"""Several front nodes: machines with the `login` role are front nodes for users (logins, Slurm commands, the
front node's tools), while the one `front` machine keeps the controller, monitoring and the website."""

import copy
import unittest
from pathlib import Path
from typing import Any

import jinja2
import yaml

from nanohpc.check import Report, mounts_result
from nanohpc.config import check_config
from nanohpc.deploy import Only, home_variables, inventory, monitor_machines, only_machines, site_data
from nanohpc.render import home_clients, own_home_machines, render

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "src/nanohpc/ansible/roles"
# The guide's sentence on the listed files in an own /home.
OWN_FILES = (
    "Every night, your listed settings files there (SSH keys, shell settings and history, git, editor and tool "
    "settings, login tokens) are replaced by their copy from the shared /home, so change them on the shared /home; "
    "your other files there stay."
)


def example() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((ROOT / "examples/cluster.yml").read_text()))


def with_login(raw: dict[str, Any], name: str, address: str, home_mount: str) -> dict[str, Any]:
    raw["machines"][name] = {"address": address, "roles": ["login"], "home_mount": home_mount}
    return raw


def home_on_storage(raw: dict[str, Any], front_mount: str) -> dict[str, Any]:
    """Move /home from the front node to a storage machine; the front node then says how it gets /home."""
    front = next(name for name, m in raw["machines"].items() if "front" in m["roles"])
    raw["machines"][front]["roles"].remove("home")
    raw["machines"][front].pop("home", None)
    raw["machines"][front]["home_mount"] = front_mount
    raw["machines"]["homesrv"] = {"address": "10.0.0.60", "roles": ["home"]}
    return raw


class LoginConfigTest(unittest.TestCase):
    def test_any_number_of_login_nodes(self) -> None:
        raw = with_login(with_login(example(), "login1", "10.0.0.51", "own"), "login2", "10.0.0.52", "own")
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["machines"]["login1"]["roles"], ["login"])

    def test_a_login_node_is_not_a_compute_machine(self) -> None:
        raw = example()
        compute = next(name for name, m in raw["machines"].items() if "compute" in m["roles"])
        raw["machines"][compute]["roles"] = ["compute", "login"]
        _, errors = check_config(raw)
        self.assertIn(f"machines.{compute}: the login role cannot be combined with compute", errors)

    def test_a_login_node_is_not_the_home_or_backup_machine(self) -> None:
        for role in ("home", "backup"):
            with self.subTest(role):
                raw = with_login(example(), "login1", "10.0.0.51", "own")
                raw["machines"]["login1"].pop("home_mount")
                raw["machines"]["login1"]["roles"].append(role)
                _, errors = check_config(raw)
                self.assertIn(f"machines.login1: the login role cannot be combined with {role}", errors)

    def test_a_login_node_is_not_the_front_node(self) -> None:
        raw = example()
        front = next(name for name, m in raw["machines"].items() if "front" in m["roles"])
        raw["machines"][front]["roles"] = [*raw["machines"][front]["roles"], "login"]
        _, errors = check_config(raw)
        self.assertIn(f"machines.{front}: the front node is already a login node; login is for the other ones", errors)


class LoginDeployTest(unittest.TestCase):
    def config(self, home_on_front: bool) -> dict[str, Any]:
        """login1 mounts the shared /home when /home is on a storage machine, and keeps its own otherwise."""
        raw = with_login(example(), "login1", "10.0.0.51", "own" if home_on_front else "shared")
        front = next(name for name, m in raw["machines"].items() if "front" in m["roles"])
        self.assertIn("home", raw["machines"][front]["roles"])  # the example keeps /home on the front node
        if not home_on_front:
            home_on_storage(raw, "shared")
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        return config

    def test_login_nodes_are_slurm_machines_without_slurmd(self) -> None:
        groups = inventory(self.config(False), None)["all"]["children"]
        self.assertEqual(list(groups["role_login"]["hosts"]), ["login1"])
        self.assertIn("role_login", groups["role_slurm"]["children"])
        self.assertNotIn("login1", groups["role_compute"]["hosts"])
        config = self.config(False)
        conf = render(config, {name: name for name in config["machines"]}, False).slurm_conf
        self.assertNotIn("NodeName=login1", conf)

    def test_home_on_its_own_machine_is_mounted_on_login_nodes(self) -> None:
        config = self.config(False)
        self.assertIn("login1", home_clients(config))
        self.assertIn("login1", inventory(config, None)["all"]["children"]["home_client"]["hosts"])

    def test_home_on_the_front_node_leaves_login_nodes_their_own_home(self) -> None:
        config = self.config(True)
        self.assertNotIn("login1", home_clients(config))
        self.assertNotIn("login1", inventory(config, None)["all"]["children"]["home_client"]["hosts"])

    def test_nanohpc_check_expects_the_right_home_on_login_nodes(self) -> None:
        local = {"mounts": {"code": 0, "out": "/ /dev/sda1 ext4\n/home /dev/sda1[/home] ext4\n"}}
        report = Report({}, [], [], [])
        mounts_result(local, self.config(True), "login1", report)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.cells["mounts"], "ok")
        report = Report({}, [], [], [])
        mounts_result({"mounts": {"code": 0, "out": "/ /dev/sda1 ext4\n"}}, self.config(True), "login1", report)
        self.assertEqual(report.problems, ["/home is not mounted (cluster.yml: home_mount: own, its own /home)"])
        report = Report({}, [], [], [])
        nfs = {"mounts": {"code": 0, "out": "/ /dev/sda1 ext4\n/home 10.0.0.10:/home nfs4\n"}}
        mounts_result(nfs, self.config(True), "login1", report)
        self.assertEqual(
            report.problems, ["/home is the NFS mount 10.0.0.10:/home, but cluster.yml says home_mount: own"]
        )
        report = Report({}, [], [], [])
        mounts_result(local, self.config(False), "login1", report)
        self.assertEqual(
            report.problems, ["/home is /dev/sda1[/home] (ext4), but cluster.yml says the NFS mount 10.0.0.60:/home"]
        )

    def test_partial_deploys_reach_login_nodes(self) -> None:
        """--only users adds accounts and home folders there; policy and partitions update slurm.conf; a new node
        reaches their /etc/hosts and slurm.conf."""
        config = self.config(True)
        compute = next(name for name, m in config["machines"].items() if "compute" in m["roles"])
        for only in (Only("users", None), Only("policy", None), Only("partitions", None), Only("node", compute)):
            with self.subTest(only.part):
                self.assertIn("login1", only_machines(config, only))

    def test_the_status_page_checks_login_nodes(self) -> None:
        shared = monitor_machines(self.config(False))["login1"]
        self.assertEqual(shared, {"role": "login", "units": ["munge.service"], "mounts": ["/", "/home"]})
        # Its own /home is a mount too: its own disk, or the root disk's /home bind-mounted onto itself.
        own = monitor_machines(self.config(True))["login1"]
        self.assertEqual(own, {"role": "login", "units": ["munge.service"], "mounts": ["/", "/home"]})

    def test_the_website_lists_every_login_address(self) -> None:
        site = site_data(self.config(False))
        self.assertEqual(
            site["login_note"],
            "You can also log in to the other front nodes: 10.0.0.51. They work the same way: use one as HostName "
            "instead. They share your /home.",
        )
        site = site_data(self.config(True))
        self.assertEqual(
            site["login_note"],
            "You can also log in to the other front nodes: 10.0.0.51. They work the same way: use one as HostName "
            "instead. 10.0.0.51 has its own /home: files you put there are not on the other front nodes. Jobs you "
            "submit there see only the shared /home, so keep your projects and data in the shared /home. " + OWN_FILES,
        )
        raw = example()
        config, _ = check_config(raw)
        self.assertEqual(site_data(config)["login_note"], "")


class HomeMountTest(unittest.TestCase):
    """Each front and login node that is not the home machine says in cluster.yml whether it mounts the shared /home
    or keeps its own (home_mount: shared|own)."""

    def test_required_on_front_and_login_nodes_that_are_not_the_home_machine(self) -> None:
        raw = with_login(example(), "login1", "10.0.0.51", "own")
        raw["machines"]["login1"].pop("home_mount")
        _, errors = check_config(raw)
        self.assertIn(
            "machines.login1.home_mount is required on a front or login node that is not the home machine: shared "
            "(it mounts the cluster's /home) or own (it keeps its own /home). Before home_mount, a login node kept "
            "its own /home when /home was on the front node (own), and mounted the shared /home when /home was on a "
            "storage machine (shared), as a front node without the home role did (shared)",
            errors,
        )
        _, errors = check_config(home_on_storage(example(), "shared"))
        self.assertEqual(errors, [])
        raw = home_on_storage(example(), "shared")
        raw["machines"]["front"].pop("home_mount")
        _, errors = check_config(raw)
        self.assertTrue(any(error.startswith("machines.front.home_mount is required") for error in errors), errors)
        raw = with_login(example(), "login1", "10.0.0.51", "nfs")
        _, errors = check_config(raw)
        self.assertIn("machines.login1.home_mount must be shared or own", errors)

    def test_refused_on_other_machines(self) -> None:
        for name in ("front", "gpu4", "store"):
            with self.subTest(name):
                raw = example()
                raw["machines"][name]["home_mount"] = "shared"
                _, errors = check_config(raw)
                self.assertIn(
                    f"machines.{name}.home_mount is only for a front or login node that is not the home machine",
                    errors,
                )
        raw = home_on_storage(example(), "shared")
        raw["machines"]["homesrv"]["home_mount"] = "own"
        _, errors = check_config(raw)
        self.assertIn(
            "machines.homesrv.home_mount is only for a front or login node that is not the home machine", errors
        )

    def test_own_home_may_name_a_disk(self) -> None:
        raw = with_login(example(), "login1", "10.0.0.51", "own")
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["machines"]["login1"]["home"], {"device": None})
        raw["machines"]["login1"]["home"] = {"device": "/dev/vdb"}
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["machines"]["login1"]["home"], {"device": "/dev/vdb"})
        raw["machines"]["login1"]["home"] = {"device": "/data"}
        _, errors = check_config(raw)
        self.assertIn("machines.login1.home.device must be a device path under /dev/", errors)
        raw = with_login(example(), "login1", "10.0.0.51", "shared")
        raw["machines"]["login1"]["home"] = {"device": "/dev/vdb"}
        _, errors = check_config(raw)
        self.assertIn(
            "machines.login1.home is only for the home machine, or a front or login node with home_mount: own", errors
        )

    def test_the_field_decides_who_mounts_the_shared_home(self) -> None:
        """A shared login node mounts /home even when it is on the front node (and is in the exports); a front node
        may keep its own /home when /home is on a storage machine. Compute machines always mount it."""
        raw = with_login(with_login(example(), "login1", "10.0.0.51", "shared"), "login2", "10.0.0.52", "own")
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertIn("login1", home_clients(config))
        self.assertNotIn("login2", home_clients(config))
        self.assertEqual(own_home_machines(config), ["login2"])
        exports = render(config, {name: name for name in config["machines"]}, False).home_exports
        self.assertIn("/home 10.0.0.51(rw", exports)
        self.assertNotIn("10.0.0.52", exports)
        config, errors = check_config(home_on_storage(example(), "own"))
        self.assertEqual(errors, [])
        self.assertNotIn("front", home_clients(config))
        self.assertEqual(own_home_machines(config), ["front"])
        self.assertIn("gpu4", home_clients(config))

    def test_deploy_groups_and_variables(self) -> None:
        raw = with_login(with_login(example(), "login1", "10.0.0.51", "shared"), "login2", "10.0.0.52", "own")
        raw["machines"]["login2"]["home"] = {"device": "/dev/vdc"}
        config, _ = check_config(raw)
        groups = inventory(config, None)["all"]["children"]
        self.assertEqual(list(groups["home_own"]["hosts"]), ["login2"])
        self.assertIn("login1", groups["home_client"]["hosts"])
        home = home_variables(config)
        self.assertEqual(home["own"], ["login2"])
        # The disk that holds each machine's /home with quotas (None: the root disk).
        self.assertEqual(home["disks"], {"front": "/dev/vdb", "login2": "/dev/vdc"})

    def test_guide_names_shared_and_own_front_nodes(self) -> None:
        raw = with_login(with_login(example(), "login1", "10.0.0.51", "shared"), "login2", "10.0.0.52", "own")
        config, _ = check_config(raw)
        self.assertEqual(
            site_data(config)["login_note"],
            "You can also log in to the other front nodes: 10.0.0.51, 10.0.0.52. They work the same way: use one as "
            "HostName instead. 10.0.0.51 shares your /home. 10.0.0.52 has its own /home: files you put there are not "
            "on the other front nodes. Jobs you submit there see only the shared /home, so keep your projects and "
            "data in the shared /home. " + OWN_FILES,
        )
        config, _ = check_config(home_on_storage(example(), "own"))
        self.assertEqual(
            site_data(config)["login_note"],
            "The front node has its own /home: files you put there are not on the other front nodes. Jobs you submit "
            "there see only the shared /home, so keep your projects and data in the shared /home. " + OWN_FILES,
        )


class LoginNodeSshTest(unittest.TestCase):
    """A login node's SSH settings are the front node's: users log in, and tunnel only to compute machines."""

    def render(self, host: str) -> str:
        environment = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
        template = (ROLES / "accounts/templates/sshd_nanohpc.conf.j2").read_text()
        return environment.from_string(template).render(
            ssh_allowed=["alice", "bob"],
            inventory_hostname=host,
            groups={"role_front": ["front"], "role_login": ["login1"]},
            nanohpc={"admins": ["alice"]},
            ssh_compute_permitopen="10.0.0.11:* cpu1:*",
        )

    def test_login_node_has_the_front_nodes_rules(self) -> None:
        self.assertEqual(self.render("login1"), self.render("front"))
        self.assertIn("PermitOpen 10.0.0.11:* cpu1:*", self.render("login1"))
        self.assertNotIn("PermitOpen", self.render("cpu1"))


if __name__ == "__main__":
    unittest.main()
