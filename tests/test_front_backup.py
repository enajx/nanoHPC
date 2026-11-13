"""A backup front node: a login node marked `front_backup: true` in cluster.yml holds the front node's services
installed but stopped, gets the front node's copies, and can be made the front node by `nanohpc front-switch`."""

import copy
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import jinja2
import yaml

from nanohpc import clusterfile, front_switch
from nanohpc.config import check_config
from nanohpc.deploy import inventory, variables

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "src/nanohpc/ansible/roles/front_backup"

# The machines the fence templates see: the front pair, a compute machine and the home machine.
MACHINES = {
    "front": {"address": "192.168.104.10"},
    "login2": {"address": "192.168.104.30"},
    "gpu2": {"address": "192.168.104.12"},
    "homesrv": {"address": "192.168.104.60"},
}


def render(path: Path, values: dict[str, Any]) -> str:
    """Render a role template as Ansible does (trim_blocks), failing on undefined values."""
    environment = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    return environment.from_string(path.read_text()).render(**values)


def switch_step() -> Any:
    """The switch's step script, loaded as a module."""
    path = str(ROOT / "src/nanohpc/files/nanohpc-front-switch-step")
    loader = SourceFileLoader("front_switch_step", path)
    spec = spec_from_loader("front_switch_step", loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def example() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((ROOT / "examples/cluster.yml").read_text()))


def with_backup(raw: dict[str, Any]) -> dict[str, Any]:
    """/home on a storage machine, the front node mounting it, and login2 the backup front node."""
    raw["machines"]["front"]["roles"] = ["front"]
    raw["machines"]["front"].pop("home")
    raw["machines"]["front"]["home_mount"] = "shared"
    raw["machines"]["homesrv"] = {"address": "192.168.104.60", "roles": ["home"]}
    raw["machines"]["login2"] = {
        "address": "192.168.104.30",
        "roles": ["login"],
        "home_mount": "shared",
        "front_backup": True,
    }
    return raw


class FrontBackupConfigTest(unittest.TestCase):
    def test_a_login_node_may_be_the_backup_front_node(self) -> None:
        config, errors = check_config(with_backup(example()))
        self.assertEqual(errors, [])
        self.assertTrue(config["machines"]["login2"]["front_backup"])

    def test_only_on_a_login_node(self) -> None:
        raw = with_backup(example())
        raw["machines"]["gpu2"]["front_backup"] = True
        _, errors = check_config(raw)
        self.assertIn("machines.gpu2.front_backup is only for a login node", errors)

    def test_must_be_true_or_false(self) -> None:
        raw = with_backup(example())
        raw["machines"]["login2"]["front_backup"] = "yes"
        _, errors = check_config(raw)
        self.assertIn("machines.login2.front_backup must be true or false", errors)

    def test_at_most_one(self) -> None:
        raw = with_backup(example())
        raw["machines"]["login3"] = {**raw["machines"]["login2"], "address": "192.168.104.31"}
        _, errors = check_config(raw)
        self.assertIn("machines: at most one login node can have front_backup: true, found 2", errors)

    def test_refused_when_home_is_on_the_front_node(self) -> None:
        raw = example()
        raw["machines"]["login2"] = {
            "address": "192.168.104.30",
            "roles": ["login"],
            "home_mount": "own",
            "front_backup": True,
        }
        _, errors = check_config(raw)
        self.assertIn(
            "machines.login2.front_backup needs /home on its own home machine, not on the front node front", errors
        )

    def test_refused_when_the_front_node_has_other_roles(self) -> None:
        raw = with_backup(example())
        raw["machines"]["front"]["roles"] = ["front", "backup"]
        raw["machines"]["front"]["backup"] = {"path": "/srv/backup"}
        raw["machines"].pop("store")
        raw["backup"]["to"] = "front"
        _, errors = check_config(raw)
        self.assertIn(
            "machines.login2.front_backup needs a front node with only the front role (front has front, backup)",
            errors,
        )

    def test_front_since_is_a_time_on_the_front_node(self) -> None:
        raw = with_backup(example())
        raw["machines"]["front"]["front_since"] = 1700000000
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        nanohpc = variables(config, Path("/work"), False, [], False, [])["nanohpc"]
        self.assertEqual(nanohpc["front"]["since"], 1700000000)
        raw["machines"]["login2"]["front_since"] = 5
        raw["machines"]["front"]["front_since"] = "soon"
        _, errors = check_config(raw)
        self.assertIn("machines.login2.front_since is only for the front node (nanohpc front-switch writes it)", errors)
        self.assertIn("machines.front.front_since must be a whole number of seconds", errors)

    def test_false_is_a_plain_login_node(self) -> None:
        raw = with_backup(example())
        raw["machines"]["login2"]["front_backup"] = False
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertFalse(config["machines"]["login2"]["front_backup"])


class FrontBackupDeployTest(unittest.TestCase):
    def test_the_front_node_and_its_backup_form_a_pair(self) -> None:
        config, errors = check_config(with_backup(example()))
        self.assertEqual(errors, [])
        groups = inventory(config, None)["all"]["children"]
        self.assertEqual(list(groups["front_pair"]["hosts"]), ["front", "login2"])
        self.assertEqual(list(groups["front_backup"]["hosts"]), ["login2"])
        nanohpc = variables(config, Path("/work"), False, [], False, [])["nanohpc"]
        self.assertEqual(nanohpc["front_backup"], {"name": "login2", "address": "192.168.104.30"})
        self.assertEqual(nanohpc["front"], {"name": "front", "address": "192.168.104.10", "since": 0})

    def test_no_pair_without_a_backup_front_node(self) -> None:
        config, errors = check_config(example())
        self.assertEqual(errors, [])
        groups = inventory(config, None)["all"]["children"]
        self.assertEqual(groups["front_pair"]["hosts"], {})
        self.assertIsNone(variables(config, Path("/work"), False, [], False, [])["nanohpc"]["front_backup"])


class FrontBackupRoleTest(unittest.TestCase):
    def defaults(self, https: str) -> dict[str, Any]:
        """The role's defaults, with the archive list worked out for a website using `https`."""
        values = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        nanohpc = {"website": {"https": https}}
        values["front_backup_archives"] = yaml.safe_load(
            jinja2.Environment().from_string(values["front_backup_archives"]).render(nanohpc=nanohpc)
        )
        return values

    def test_the_switch_restores_what_the_nightly_copy_sends(self) -> None:
        step = switch_step()
        defaults = self.defaults("letsencrypt")
        self.assertEqual([[name, path] for name, path, _ in step.FOLDERS], defaults["front_backup_folders"])
        self.assertEqual(
            [[name, f"{parent}/{name}"] for name, parent in step.ARCHIVES], defaults["front_backup_archives"]
        )
        self.assertEqual(sorted({owner for _, _, owner in step.FOLDERS} - {"root"}),
                         sorted(set(defaults["front_backup_accounts"]) - {"cluster-grafana"}))  # fmt: skip

    def test_the_copy_command_sends_the_folders_and_archives(self) -> None:
        defaults = self.defaults("own")
        values = {
            **defaults,
            "front_backup_peer": "login2",
            "front_backup_peer_address": "192.168.104.30",
        }
        text = render(ROLE / "templates/nanohpc-front-copy-run.j2", values)
        subprocess.run(["sh", "-n"], input=text, text=True, check=True)
        self.assertIn("--to login2=192.168.104.30", text)
        self.assertIn("--item /var/lib/nanohpc/prometheus=prometheus \\", text)
        self.assertIn("--archive /etc/nanohpc/slurmdbd.env=slurmdbd.env \\", text)
        self.assertNotIn("letsencrypt", text)  # own certificate: from the administrator's files

    def test_the_fence_watches_the_other_machine(self) -> None:
        values = {
            "front_backup_peer": "login2",
            "front_backup_units": ["slurmctld.service", "nginx.service"],
            "ansible_facts": {"hostname": "front"},
            "inventory_hostname": "front",
            "groups": {"front_pair": ["front", "login2"]},
            "front_backup_peer_address": "192.168.104.30",
            "nanohpc": {"machines": MACHINES, "front": {"name": "front"}, "front_backup": {"name": "login2"}},
        }
        text = render(ROLE / "templates/nanohpc-front-fence-run.j2", values)
        subprocess.run(["sh", "-n"], input=text, text=True, check=True)
        self.assertIn("--hostname front", text)
        self.assertIn("--peer login2=192.168.104.30:6817", text)
        self.assertIn("--stop slurmctld.service --stop nginx.service", text)

    def test_the_fence_asks_the_other_cluster_machines(self) -> None:
        values = {
            "front_backup_peer": "login2",
            "front_backup_units": ["slurmctld.service"],
            "ansible_facts": {"hostname": "front"},
            "inventory_hostname": "front",
            "groups": {"front_pair": ["front", "login2"]},
            "front_backup_peer_address": "192.168.104.30",
            "nanohpc": {"machines": MACHINES, "front": {"name": "front"}, "front_backup": {"name": "login2"}},
        }
        text = render(ROLE / "templates/nanohpc-front-fence-run.j2", values)
        subprocess.run(["sh", "-n"], input=text, text=True, check=True)
        self.assertIn("--name front", text)
        self.assertIn("--ask gpu2=192.168.104.12 --ask homesrv=192.168.104.60 ", text)
        self.assertNotIn("--ask login2", text)
        self.assertIn("--start slurmdbd.service --start slurmctld.service", text)

    def test_the_other_machines_tell_the_pair_which_front_node_they_have(self) -> None:
        """Each front pair machine's key may only read the front node this machine was deployed with."""
        template = ROOT / "src/nanohpc/ansible/roles/accounts/templates/root_authorized_keys.j2"
        values = {
            "users": [],
            "admins": [],
            "auto_deploy_key": "",
            "front_address": "192.168.104.10",
            "front_node_askers": [
                {"address": "192.168.104.10", "key": "ssh-ed25519 AAAA nanohpc-front-copy@front"},
                {"address": "192.168.104.30", "key": "ssh-ed25519 BBBB nanohpc-front-copy@login2"},
            ],
        }
        text = render(template, values)
        self.assertIn(
            'from="192.168.104.30",command="cat /etc/nanohpc/front-node",restrict ssh-ed25519 BBBB nanohpc-front-copy@login2',
            text,
        )
        self.assertIn('from="192.168.104.10",command="cat /etc/nanohpc/front-node",restrict', text)

    def test_every_machine_records_its_front_node_only_with_a_backup_front_node(self) -> None:
        tasks = yaml.safe_load((ROOT / "src/nanohpc/ansible/roles/base/tasks/main.yml").read_text())
        task = next(task for task in tasks if "front node" in task["name"] and "Record" in task["name"])
        self.assertEqual(task["ansible.builtin.copy"]["dest"], "/etc/nanohpc/front-node")
        self.assertIn("front_backup", str(task["when"]))

    def test_the_fence_leaves_the_accounting_database_running(self) -> None:
        """The switch's stop step holds the fence and then dumps the accounting database for the last copy: the
        minute watch of a fenced machine must not stop MariaDB under that dump (it runs no controller)."""
        values = {
            **self.defaults("own"),
            "front_backup_peer": "login2",
            "ansible_facts": {"hostname": "front"},
            "inventory_hostname": "front",
            "groups": {"front_pair": ["front", "login2"]},
            "front_backup_peer_address": "192.168.104.30",
            "nanohpc": {"machines": MACHINES, "front": {"name": "front"}, "front_backup": {"name": "login2"}},
        }
        text = render(ROLE / "templates/nanohpc-front-fence-run.j2", values)
        self.assertIn("--stop slurmctld.service --stop slurmdbd.service", text)
        self.assertNotIn("mariadb", text)

    def test_each_machine_of_the_pair_accepts_the_other_ones_copies(self) -> None:
        template = ROOT / "src/nanohpc/ansible/roles/accounts/templates/root_authorized_keys.j2"
        values = {
            "users": [],
            "admins": [],
            "auto_deploy_key": "",
            "front_address": "192.168.104.10",
            "front_copy_key": "ssh-ed25519 AAAA nanohpc-front-copy@login2",
            "front_copy_address": "192.168.104.30",
        }
        text = render(template, values)
        self.assertIn(
            'from="192.168.104.30",command="/usr/local/sbin/nanohpc-front-copy-receive",restrict'
            " ssh-ed25519 AAAA nanohpc-front-copy@login2",
            text,
        )
        self.assertNotIn("nanohpc-front-copy-receive", render(template, {**values, "front_copy_key": ""}))

    def test_without_a_backup_front_node_the_fence_is_removed_before_slurm_starts(self) -> None:
        """Removing front_backup: the old front node's controller starts as without the feature."""
        text = (ROLE / "tasks/off.yml").read_text()
        for path in (
            "/etc/systemd/system/slurmctld.service.d/nanohpc-front-fence.conf",
            "/etc/systemd/system/slurmdbd.service.d/nanohpc-front-fence.conf",
            "/usr/local/sbin/nanohpc-front-fence-run",
            "/var/lib/nanohpc-front-fence",
            "nanohpc-front-fence.timer",
        ):
            self.assertIn(path, text)
        plays = yaml.safe_load((ROOT / "src/nanohpc/ansible/site.yml").read_text())
        names = [play["name"] for play in plays]
        off = next(index for index, play in enumerate(plays) if "!front_pair" in play["hosts"])
        self.assertLess(off, names.index("Slurm controller and accounting on the front node"))

    def test_the_backup_front_node_gets_the_slurm_controller_packages(self) -> None:
        tasks = (ROOT / "src/nanohpc/ansible/roles/slurm_packages/tasks/main.yml").read_text()
        self.assertIn("groups['role_front'] + (groups['front_backup'] | default([]))", tasks)


class SwitchDeployTest(unittest.TestCase):
    """The deploy a switch runs, with the old front node away: its variables, groups and the fence they render."""

    def setUp(self) -> None:
        folder = Path(tempfile.mkdtemp())
        path = folder / "cluster.yml"
        path.write_text(yaml.safe_dump(with_backup(example()), sort_keys=False))
        file = clusterfile.load(path)
        front_switch.swapped(file, "front", "login2")
        self.config, errors = front_switch.deploy_config(file, folder, "front")
        self.assertEqual(errors, [])
        self.nanohpc = variables(self.config, Path("/work"), False, [], False, [])["nanohpc"]
        children = inventory(self.config, None)["all"]["children"]
        self.groups = {name: list(group.get("hosts", {})) for name, group in children.items()}

    def role_facts(self, host: str) -> dict[str, Any]:
        """The values the front_backup role's first task sets on `host`, worked out as Ansible would."""
        task = yaml.safe_load((ROLE / "tasks/main.yml").read_text())[0]["ansible.builtin.set_fact"]
        values = {"nanohpc": self.nanohpc, "groups": self.groups, "inventory_hostname": host}
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        return {key: environment.from_string(value).render(**values) for key, value in task.items()}

    def test_the_old_front_node_stays_the_backup_front_node_while_away(self) -> None:
        self.assertEqual(self.nanohpc["front_backup"], {"name": "front", "address": "192.168.104.10"})
        self.assertEqual(self.nanohpc["front"]["name"], "login2")
        self.assertGreater(self.nanohpc["front"]["since"], 1700000000)
        self.assertEqual(self.groups["front_pair"], ["login2"])

    def test_every_machine_records_the_new_front_node(self) -> None:
        tasks = yaml.safe_load((ROOT / "src/nanohpc/ansible/roles/base/tasks/main.yml").read_text())
        task = next(task for task in tasks if task["name"].startswith("Record the front node"))
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.assertTrue(environment.from_string("{{ " + task["when"] + " }}").render(nanohpc=self.nanohpc) == "True")
        content = environment.from_string(task["ansible.builtin.copy"]["content"]).render(nanohpc=self.nanohpc)
        self.assertEqual(content.strip(), f"login2 {self.nanohpc['front']['since']}")

    def test_the_new_front_nodes_fence_knows_its_peer(self) -> None:
        facts = self.role_facts("login2")
        self.assertEqual(facts["front_backup_peer"], "front")
        values = {
            **facts,
            "front_backup_units": ["slurmctld.service"],
            "ansible_facts": {"hostname": "login2"},
            "inventory_hostname": "login2",
            "groups": self.groups,
            "nanohpc": self.nanohpc,
        }
        text = render(ROLE / "templates/nanohpc-front-fence-run.j2", values)
        self.assertIn("--peer front=192.168.104.10:6817", text)
        self.assertIn("--ask gpu4=192.168.104.11", text)
        self.assertNotIn("--ask front=", text)
        self.assertNotIn("--ask login2=", text)

    def test_the_new_front_node_starts_while_a_machine_that_missed_the_switch_names_the_old_one(self) -> None:
        """The rendered fence command, run as the new front node: gpu4 was off during the switch and still names the
        old front node; the other machines do not answer. Mocked: a fake ssh and systemctl, a closed local port for
        the old front node's Slurm controller."""
        folder = Path(tempfile.mkdtemp())
        values = {
            **self.role_facts("login2"),
            "front_backup_units": ["slurmctld.service"],
            "ansible_facts": {"hostname": "login2"},
            "inventory_hostname": "login2",
            "groups": self.groups,
            "nanohpc": self.nanohpc,
        }
        tasks = yaml.safe_load((ROOT / "src/nanohpc/ansible/roles/base/tasks/main.yml").read_text())
        record = next(task for task in tasks if task["name"].startswith("Record the front node"))
        (folder / "front-node").write_text(
            jinja2.Environment().from_string(record["ansible.builtin.copy"]["content"]).render(nanohpc=self.nanohpc)
        )
        (folder / "took-over").write_text(
            f"took over from front at 2026-11-13 18:00 since {self.nanohpc['front']['since']}\n"
        )
        (folder / "slurm.conf").write_text("SlurmctldHost=login2(192.168.104.30)\n")
        bin_folder = folder / "bin"
        bin_folder.mkdir()
        (bin_folder / "ssh").write_text(
            '#!/bin/sh\nfor word in "$@"; do case "$word" in root@192.168.104.11) echo "front 0"; exit 0 ;; esac; done\nexit 255\n'
        )
        (bin_folder / "systemctl").write_text(f"#!/bin/sh\necho \"$*\" >> '{folder / 'calls'}'\n")
        for program in ("ssh", "systemctl"):
            (bin_folder / program).chmod(0o755)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.addCleanup(closed.close)
        text = render(ROLE / "templates/nanohpc-front-fence-run.j2", values)
        line = text.split("exec ", 1)[1].replace("\\\n", " ").replace('"$@"', "")
        line = line.replace("192.168.104.10:6817", f"127.0.0.1:{closed.getsockname()[1]}")
        for path, local in (
            ("/usr/local/sbin/nanohpc-front-fence", str(ROOT / "src/nanohpc/files/nanohpc-front-fence")),
            ("/etc/slurm/slurm.conf", str(folder / "slurm.conf")),
            ("/etc/nanohpc/front-node", str(folder / "front-node")),
            ("/var/lib/nanohpc-front-fence/", f"{folder}/"),
            ("/usr/bin/systemctl", str(bin_folder / "systemctl")),
        ):
            line = line.replace(path, local)
        environment = {**os.environ, "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}"}
        result = subprocess.run([sys.executable, *shlex.split(line), "--check"], env=environment, capture_output=True,
                                text=True, check=False)  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpu4 names front (since 0)", result.stderr)
        self.assertFalse((folder / "fenced").exists())


if __name__ == "__main__":
    unittest.main()
