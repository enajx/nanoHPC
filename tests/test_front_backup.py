"""A backup front node: a login node marked `front_backup: true` in cluster.yml holds the front node's services
installed but stopped, gets the front node's copies, and can be made the front node by `nanohpc front-switch`."""

import copy
import unittest
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import check_config
from nanohpc.deploy import inventory, variables

ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
