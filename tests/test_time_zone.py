"""An optional `time_zone` in cluster.yml sets that zone on every machine; without it, machines keep their own.

Static checks of the playbooks and the base role's tasks. The real-VM check is `SimMachineSetupTest` in
tests/test_sim.py (`timedatectl` on every machine after a deploy).
"""

import unittest
from pathlib import Path
from typing import Any

import yaml

ANSIBLE = Path(__file__).resolve().parents[1] / "src" / "nanohpc" / "ansible"
BASE = ANSIBLE / "roles" / "base" / "tasks"


def command(task: dict[str, Any]) -> str:
    """Return a task's command line, or an empty string."""
    return str(task.get("ansible.builtin.command", ""))


class TimeZoneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks: list[dict[str, Any]] = yaml.safe_load((BASE / "time_zone.yml").read_text())

    def test_every_machine_runs_it_only_when_a_zone_is_set(self) -> None:
        """The base role runs it on every machine of a full deploy and on the new machine of --only node."""
        base = yaml.safe_load((BASE / "main.yml").read_text())
        step = next(task for task in base if task.get("ansible.builtin.import_tasks") == "time_zone.yml")
        self.assertEqual(step["when"], "nanohpc.time_zone is not none")
        site = yaml.safe_load((ANSIBLE / "site.yml").read_text())
        self.assertTrue(any(play["hosts"] == "all" and "base" in play.get("roles", []) for play in site))
        partial = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        self.assertTrue(any(play["hosts"] == "only_node" and "base" in play.get("roles", []) for play in partial))

    def test_unknown_zone_stops_the_machine_in_the_dry_run(self) -> None:
        listing = next(task for task in self.tasks if "list-timezones" in command(task))
        self.assertIs(listing["check_mode"], False)
        stop = next(task for task in self.tasks if "ansible.builtin.assert" in task)
        self.assertIn(listing["register"], str(stop["ansible.builtin.assert"]["that"]))
        self.assertLess(
            self.tasks.index(stop), next(i for i, t in enumerate(self.tasks) if "set-timezone" in command(t))
        )

    def test_zone_is_set_only_when_different_then_cron_restarts_and_it_is_checked(self) -> None:
        current = next(task for task in self.tasks if "show -p Timezone" in command(task))
        self.assertIs(current["check_mode"], False)
        setting = next(task for task in self.tasks if "set-timezone" in command(task))
        self.assertIn("nanohpc.time_zone", command(setting))
        self.assertIn(current["register"], str(setting["when"]))
        cron = next(task for task in self.tasks if "ansible.builtin.systemd" in task)
        self.assertEqual(cron["ansible.builtin.systemd"], {"name": "cron", "state": "restarted"})
        self.assertEqual(cron["when"], f"{setting['register']} is changed")
        check = self.tasks[-1]
        self.assertIn("show -p Timezone", command(check))
        self.assertIn("nanohpc.time_zone", check["failed_when"])
        self.assertEqual(check["when"], "not ansible_check_mode")


if __name__ == "__main__":
    unittest.main()
