"""Static checks of the Ansible playbook and roles for the dry run (check mode)."""

import unittest
from pathlib import Path
from typing import Any

import yaml

ANSIBLE = Path(__file__).resolve().parents[1] / "src" / "nanohpc" / "ansible"
COMMANDS = {"ansible.builtin.command", "ansible.builtin.shell", "command", "shell"}


def conditions(task: dict[str, Any]) -> list[str]:
    """Return a task's or block's `when` conditions as strings."""
    when = task.get("when", [])
    return [str(condition) for condition in (when if isinstance(when, list) else [when])]


def tasks_in(tasks: list[dict[str, Any]], inherited: list[str]) -> list[tuple[dict[str, Any], list[str]]]:
    """Return every task, inside blocks too, with the `when` conditions it inherits and its own."""
    found = []
    for task in tasks or []:
        own = inherited + conditions(task)
        for key in ("block", "rescue", "always"):
            if key in task:
                found += tasks_in(task[key], own)
        found.append((task, own))
    return found


def all_tasks() -> list[tuple[str, dict[str, Any], list[str]]]:
    """Return every task of the roles and site.yml, with its file and its `when` conditions."""
    found = []
    for path in sorted(ANSIBLE.glob("roles/*/*/*.yml")):
        if path.parent.name in ("tasks", "handlers"):
            found += [
                (str(path.relative_to(ANSIBLE)), task, when)
                for task, when in tasks_in(yaml.safe_load(path.read_text()), [])
            ]
    for play in yaml.safe_load((ANSIBLE / "site.yml").read_text()):
        for key in ("pre_tasks", "tasks"):
            found += [("site.yml", task, when) for task, when in tasks_in(play.get(key, []), [])]
    return found


class CheckModeTest(unittest.TestCase):
    def test_read_only_commands_say_what_they_do_in_the_dry_run(self) -> None:
        """A command or shell task marked as never changing anything (changed_when: false) must either run in the
        dry run (check_mode: false) or say when it is skipped there (a condition on ansible_check_mode). Otherwise
        check mode skips it silently, and the dry run misses a check the real run makes."""
        missing = [
            f"{path}: {task['name']}"
            for path, task, when in all_tasks()
            if COMMANDS & task.keys()
            and task.get("changed_when") is False
            and task.get("check_mode") is not False
            and not any("ansible_check_mode" in condition for condition in when)
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
