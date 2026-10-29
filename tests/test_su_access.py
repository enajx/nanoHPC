"""`su` works only for members of the sudo group on every machine; others see `su: Permission denied`.

Static checks of the playbooks and the accounts role's tasks, and of the PAM line edit on Ubuntu's own
/etc/pam.d/su lines. The real-VM check is `SimMachineSetupTest` in tests/test_sim.py (`su` as a user outside
the sudo group is refused, an administrator is asked for root's password).
"""

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ANSIBLE = Path(__file__).resolve().parents[1] / "src" / "nanohpc" / "ansible"
ACCOUNTS = ANSIBLE / "roles" / "accounts" / "tasks"
PAM_LINE = "auth       requisite  pam_wheel.so group=sudo"


class SuAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks: list[dict[str, Any]] = yaml.safe_load((ACCOUNTS / "su.yml").read_text())

    def test_every_machine_gets_it_after_the_administrators_join_sudo(self) -> None:
        """The accounts role runs it on every machine of a full deploy, of --only users, and on the new machine
        of --only node. Monitor mode changes no accounts or logins, so it does not run there."""
        accounts = yaml.safe_load((ACCOUNTS / "main.yml").read_text())
        names = [task["name"] for task in accounts]
        step = next(task for task in accounts if task.get("ansible.builtin.import_tasks") == "su.yml")
        self.assertGreater(accounts.index(step), names.index("Put administrators in the sudo group"))
        site = yaml.safe_load((ANSIBLE / "site.yml").read_text())
        self.assertTrue(any(play["hosts"] == "all" and "accounts" in play.get("roles", []) for play in site))
        partial = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        self.assertTrue(any(play["hosts"] == "all" and "accounts" in play.get("roles", []) for play in partial))
        self.assertTrue(any(play["hosts"] == "only_node" and "accounts" in play.get("roles", []) for play in partial))
        monitor = yaml.safe_load((ANSIBLE / "monitor.yml").read_text())
        self.assertFalse(any("accounts" in play.get("roles", []) for play in monitor))

    def test_the_line_replaces_ubuntus_commented_one(self) -> None:
        edit = next(task for task in self.tasks if "ansible.builtin.lineinfile" in task)["ansible.builtin.lineinfile"]
        self.assertEqual(edit["path"], "/etc/pam.d/su")
        self.assertEqual(edit["line"], PAM_LINE)
        pattern = re.compile(edit["regexp"])
        self.assertTrue(pattern.search("# auth       required   pam_wheel.so"))
        self.assertTrue(pattern.search(PAM_LINE))
        self.assertFalse(pattern.search("# auth       required   pam_wheel.so deny group=nosu"))
        self.assertFalse(pattern.search("# auth       sufficient pam_wheel.so trust"))

    def test_it_stops_unless_the_file_is_as_expected_and_saves_a_copy_first(self) -> None:
        stop = next(task for task in self.tasks if "ansible.builtin.assert" in task)
        self.assertIn("pam_rootok", str(stop))
        copy = next(task for task in self.tasks if "ansible.builtin.copy" in task)["ansible.builtin.copy"]
        self.assertEqual((copy["src"], copy["remote_src"], copy["force"]), ("/etc/pam.d/su", True, False))
        edit = next(i for i, task in enumerate(self.tasks) if "ansible.builtin.lineinfile" in task)
        self.assertLess(self.tasks.index(stop), edit)
        self.assertLess(next(i for i, task in enumerate(self.tasks) if "ansible.builtin.copy" in task), edit)

    def test_live_checks_after_the_change(self) -> None:
        """Without input, su would wait for a password until the timeout, so the checks give it none."""
        checks = [task for task in self.tasks if "script -qec" in str(task.get("ansible.builtin.shell", ""))]
        self.assertEqual(len(checks), 2)
        for check in checks:
            self.assertTrue(check["ansible.builtin.shell"].endswith("</dev/null"))
            self.assertEqual(check["when"], "not ansible_check_mode")
        refused, allowed = checks
        self.assertIn("-u nobody", refused["ansible.builtin.shell"])
        self.assertIn("'Permission denied' not in", refused["failed_when"])
        self.assertIn("nanohpc.admins[0]", allowed["ansible.builtin.shell"])
        self.assertIn("'Password:' not in", allowed["failed_when"])


if __name__ == "__main__":
    unittest.main()
