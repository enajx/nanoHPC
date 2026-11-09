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
from nanohpc.deploy import Only, inventory, monitor_machines, only_machines, site_data
from nanohpc.render import home_clients, render

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "src/nanohpc/ansible/roles"


def example() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((ROOT / "examples/cluster.yml").read_text()))


def with_login(raw: dict[str, Any], name: str, address: str) -> dict[str, Any]:
    raw["machines"][name] = {"address": address, "roles": ["login"]}
    return raw


class LoginConfigTest(unittest.TestCase):
    def test_any_number_of_login_nodes(self) -> None:
        raw = with_login(with_login(example(), "login1", "10.0.0.51"), "login2", "10.0.0.52")
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
                raw = with_login(example(), "login1", "10.0.0.51")
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
        raw = with_login(example(), "login1", "10.0.0.51")
        front = next(name for name, m in raw["machines"].items() if "front" in m["roles"])
        self.assertIn("home", raw["machines"][front]["roles"])  # the example keeps /home on the front node
        if not home_on_front:
            raw["machines"][front]["roles"].remove("home")
            raw["machines"][front].pop("home", None)
            raw["machines"]["homesrv"] = {"address": "10.0.0.60", "roles": ["home"]}
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
        local = {"mounts": {"code": 0, "out": "/ /dev/sda1 ext4\n"}}
        report = Report({}, [], [], [])
        mounts_result(local, self.config(True), "login1", report)
        self.assertEqual(report.problems, [])
        report = Report({}, [], [], [])
        mounts_result(local, self.config(False), "login1", report)
        self.assertEqual(report.problems, ["/home is not mounted (cluster.yml: the NFS mount 10.0.0.60:/home)"])

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
        # Its own /home is a folder on its root disk, not a mount.
        own = monitor_machines(self.config(True))["login1"]
        self.assertEqual(own, {"role": "login", "units": ["munge.service"], "mounts": ["/"]})

    def test_the_website_lists_every_login_address(self) -> None:
        site = site_data(self.config(False))
        self.assertEqual(
            site["login_note"],
            "You can also log in to the other front nodes: 10.0.0.51. They work the same way: use one as HostName "
            "instead. They share your /home.",
        )
        site = site_data(self.config(True))
        self.assertIn(
            "Each one has its own /home: files you put on one are not on the others, nor in your jobs, which use "
            "the main front node's /home.",
            site["login_note"],
        )
        raw = example()
        config, _ = check_config(raw)
        self.assertEqual(site_data(config)["login_note"], "")


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
