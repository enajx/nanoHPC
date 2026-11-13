"""A backup front node: a login node marked `front_backup: true` in cluster.yml holds the front node's services
installed but stopped, gets the front node's copies, and can be made the front node by `nanohpc front-switch`."""

import copy
import subprocess
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import jinja2
import yaml

from nanohpc.config import check_config
from nanohpc.deploy import inventory, variables

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "src/nanohpc/ansible/roles/front_backup"


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
        self.assertEqual(nanohpc["front"], {"name": "front", "address": "192.168.104.10"})

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
            "nanohpc": {"machines": {"login2": {"address": "192.168.104.30"}}},
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
            "nanohpc": {"machines": {"login2": {"address": "192.168.104.30"}}},
        }
        text = render(ROLE / "templates/nanohpc-front-fence-run.j2", values)
        subprocess.run(["sh", "-n"], input=text, text=True, check=True)
        self.assertIn("--hostname front", text)
        self.assertIn("--peer login2=192.168.104.30:6817", text)
        self.assertIn("--stop slurmctld.service --stop nginx.service", text)

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

    def test_the_backup_front_node_gets_the_slurm_controller_packages(self) -> None:
        tasks = (ROOT / "src/nanohpc/ansible/roles/slurm_packages/tasks/main.yml").read_text()
        self.assertIn("groups['role_front'] + (groups['front_backup'] | default([]))", tasks)


if __name__ == "__main__":
    unittest.main()
