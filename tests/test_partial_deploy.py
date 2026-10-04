"""Tests for partial deploys (`nanohpc deploy --only users|policy|partitions` or `--only node NAME`): what each part
runs and where, when a partial deploy is refused, and the command line. No machines are needed."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import load_config
from nanohpc.deploy import (
    ONLY,
    Only,
    deployed_problems,
    needed_machines,
    only_machines,
    only_refusal,
    parse_only,
    prepare,
)

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "src" / "nanohpc" / "ansible"


def example(name: str) -> dict[str, Any]:
    """Return a validated cluster.yml from the repository."""
    config, errors = load_config(ROOT / name, True, False)
    assert errors == [], errors
    return config


def plays(name: str) -> list[dict[str, Any]]:
    """Return the plays of a playbook in the package."""
    return yaml.safe_load((ANSIBLE / name).read_text())


def roles_of(play: dict[str, Any]) -> list[str]:
    """Return the roles a play runs, from `roles:` and from include_role or import_role tasks, in order."""
    found = [role if isinstance(role, str) else role["role"] for role in play.get("roles", [])]
    for task in play.get("tasks", []):
        for key in ("ansible.builtin.include_role", "ansible.builtin.import_role"):
            if key in task:
                found.append(task[key]["name"])
    return found


class OnlyArgumentsTest(unittest.TestCase):
    """`--only` takes one part name, or `node` and a machine name."""

    def test_parts(self) -> None:
        self.assertEqual(parse_only(["users"]), (Only("users", None), None))
        self.assertEqual(parse_only(["policy"]), (Only("policy", None), None))
        self.assertEqual(parse_only(["partitions"]), (Only("partitions", None), None))
        self.assertEqual(parse_only(["node", "gpu4i"]), (Only("node", "gpu4i"), None))

    def test_wrong_arguments(self) -> None:
        for words in (["node"], ["users", "gpu4"], ["machines"], ["node", "a", "b"]):
            only, error = parse_only(words)
            self.assertIsNone(only, words)
            self.assertIn("--only takes users, policy, partitions, or node NAME", str(error), words)


class OnlyMachinesTest(unittest.TestCase):
    """Each part runs on the machines that hold it, and only there."""

    def test_machines_of_each_part(self) -> None:
        config = example("examples/cluster.yml")
        everyone = ["front", "gpu4", "gpu2", "cpu1", "gpu4i", "store"]
        self.assertEqual(only_machines(config, Only("users", None)), everyone)
        slurm = ["front", "gpu4", "gpu2", "cpu1", "gpu4i"]
        self.assertEqual(only_machines(config, Only("policy", None)), slurm)
        self.assertEqual(only_machines(config, Only("partitions", None)), slurm)
        # The new machine, and the shared parts on all the others (/etc/hosts is on every machine).
        self.assertEqual(only_machines(config, Only("node", "gpu4i")), everyone)

    def test_nothing_is_deployed_when_the_new_machine_fails_its_dry_run(self) -> None:
        """The shared parts on the other machines are for the new machine: if its dry run fails, they wait too."""
        config = example("examples/cluster.yml")
        machines = only_machines(config, Only("node", "gpu4i"))
        self.assertEqual(
            needed_machines(config, machines, "gpu4i"),
            {"front": "the front node and the home machine", "gpu4i": "the machine of --only node"},
        )
        slurm = only_machines(config, Only("policy", None))
        storage = example("tests/sim/cluster-home-on-storage.yml")
        self.assertEqual(needed_machines(storage, slurm, None), {"front": "the front node"})
        self.assertEqual(needed_machines(config, slurm, None), {"front": "the front node and the home machine"})

    def test_every_part_has_its_plays(self) -> None:
        """Each part's tag is on plays in partial.yml, and partial.yml uses no other tag."""
        tags = {tag for play in plays("partial.yml") for tag in play.get("tags", [])}
        self.assertEqual(tags, {"always", *(part.tag for part in ONLY.values())})

    def test_the_new_machine_gets_what_a_full_deploy_gives_a_compute_machine(self) -> None:
        """--only node runs, on the new machine, the roles site.yml runs on a compute machine, in the same order."""
        compute_groups = {"all", "role_compute", "role_slurm", "all:!role_home:!role_backup"}
        full = [role for play in plays("site.yml") if play["hosts"] in compute_groups for role in roles_of(play)]
        node = [
            role for play in plays("partial.yml") if "only_node" in play["hosts"].split(":") for role in roles_of(play)
        ]
        # Not on a compute machine: the front node's automatic deploys, the backup, and the steps that only
        # read or check (preflight and the final cluster-health run on every machine in both playbooks).
        self.assertEqual(
            [role for role in full if role not in ("preflight", "health")],
            [role for role in node if role not in ("preflight", "health")],
        )
        self.assertIn("health", node)

    def test_inventory_names_the_new_machine(self) -> None:
        config = example("examples/cluster.yml")
        hostnames = {name: f"host-{name}" for name in config["machines"]}
        with tempfile.TemporaryDirectory() as folder:
            prepare(config, hostnames, None, True, [], False, "gpu4i", Path(folder))
            groups = yaml.safe_load((Path(folder) / "inventory.yml").read_text())["all"]["children"]
            self.assertEqual(groups["only_node"], {"hosts": {"gpu4i": None}})
            prepare(config, hostnames, None, True, [], False, None, Path(folder))
            groups = yaml.safe_load((Path(folder) / "inventory.yml").read_text())["all"]["children"]
            self.assertEqual(groups["only_node"], {"hosts": {}})


class RefusalTest(unittest.TestCase):
    """A partial deploy that cannot be done safely is refused before any change, with what to do instead."""

    def setUp(self) -> None:
        self.config = example("examples/cluster.yml")
        self.deployed: dict[str, list[str] | None] = {
            name: list(machine["roles"]) for name, machine in self.config["machines"].items()
        }

    def test_only_a_compute_machine_can_be_deployed_alone(self) -> None:
        self.assertIsNone(only_refusal(self.config, Only("node", "gpu4i")))
        self.assertIsNone(only_refusal(self.config, Only("users", None)))
        for name in ("front", "store"):
            refused = str(only_refusal(self.config, Only("node", name)))
            self.assertIn(f"{name} is not a compute machine", refused)
            self.assertIn("run a full deploy (nanohpc deploy without --only)", refused)
        self.assertIn("gpu9 is not a machine in cluster.yml", str(only_refusal(self.config, Only("node", "gpu9"))))
        storage = example("tests/sim/cluster-home-on-storage.yml")
        self.assertIn("store is not a compute machine", str(only_refusal(storage, Only("node", "store"))))

    def test_every_other_machine_must_be_deployed_with_its_roles(self) -> None:
        self.assertEqual(deployed_problems(self.config, Only("users", None), self.deployed), [])
        self.deployed["gpu4i"] = None
        # The new machine itself may be new.
        self.assertEqual(deployed_problems(self.config, Only("node", "gpu4i"), self.deployed), [])
        self.assertEqual(
            deployed_problems(self.config, Only("users", None), self.deployed),
            [
                (
                    "gpu4i has not been deployed yet: deploy it first with --only node gpu4i, or run a full deploy"
                    " (nanohpc deploy without --only)"
                )
            ],
        )
        self.deployed["store"] = ["home"]
        self.assertEqual(
            deployed_problems(self.config, Only("node", "gpu4i"), self.deployed),
            [
                (
                    "store was deployed with the roles home, and cluster.yml gives it backup: a change of roles affects"
                    " the other machines, run a full deploy (nanohpc deploy without --only)"
                )
            ],
        )

    def test_the_new_machine_cannot_change_roles(self) -> None:
        self.deployed["gpu4i"] = ["backup"]
        self.assertEqual(
            deployed_problems(self.config, Only("node", "gpu4i"), self.deployed),
            [
                (
                    "gpu4i was deployed with the roles backup, and cluster.yml gives it compute: a change of roles"
                    " affects the other machines, run a full deploy (nanohpc deploy without --only)"
                )
            ],
        )


class OnlyCommandTest(unittest.TestCase):
    """The command line refuses a wrong --only before connecting to any machine."""

    def nanohpc(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", "from nanohpc.cli import main; main()", *arguments],
            capture_output=True, text=True, check=False, cwd=ROOT,
        )  # fmt: skip

    def test_refused_before_any_connection(self) -> None:
        result = self.nanohpc("deploy", "examples/cluster.yml", "--only", "node", "front")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Nothing was changed: front is not a compute machine", result.stderr)
        self.assertNotIn("cannot connect", result.stderr)
        self.assertEqual(result.stdout, "")
        result = self.nanohpc("deploy", "examples/cluster.yml", "--only", "node")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--only takes users, policy, partitions, or node NAME", result.stderr)

    def test_sim_deploy_takes_only_too(self) -> None:
        result = self.nanohpc("sim", "up", "tests/sim/everyday.yml", "--only", "users")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--only works only with nanohpc sim deploy", result.stderr)


if __name__ == "__main__":
    unittest.main()
