"""`nanohpc front-status`, `front-switch` and `front-back`, run from the administrator's machine: they show what they
found, ask for the machine's name, run every dry run before changing anything, work when the old front node does
not answer, and change cluster.yml so later deploys follow the new front node.

Mocked: SSH is a fake `ssh` first on PATH that records each machine's commands and answers from files; the deploy,
the typed answer and the Slack post are passed in as stand-ins. No machine or Ansible run is involved."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from nanohpc import front_switch
from nanohpc.config import load_config
from tests.test_front_backup import example, with_backup

STATUS = "now=1700003600\nslurmctld={slurmctld}\nstate={state}\nnightly={nightly}\nfenced=\n"

FAKE_SSH = """#!/bin/sh
while [ "$#" -gt 2 ]; do
  case "$1" in -F|-o) shift 2 ;; *) shift ;; esac
done
target=$1
command=$2
echo "$target: $command" >> "$FAKE/calls"
if [ -e "$FAKE/$target.down" ]; then echo "ssh: connect to host $target: No route to host" >&2; exit 255; fi
case "$command" in
  *state.done*) cat "$FAKE/$target.status" ;;
  *--dry-run*) if [ -e "$FAKE/$target.dryfail" ]; then echo "dry run failed here" >&2; exit 1; fi; echo "dry run ok" ;;
  *) echo "done" ;;
esac
"""


class SwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        (bin_folder / "ssh").write_text(FAKE_SSH)
        (bin_folder / "ssh").chmod(0o755)
        self.environment = {"PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}", "FAKE": str(self.folder)}
        self.saved = {key: os.environ.get(key) for key in self.environment}
        os.environ.update(self.environment)
        self.path = self.folder / "cluster.yml"
        raw = with_backup(example())
        raw["alerts"]["slack"] = True
        self.path.write_text(yaml.safe_dump(raw, sort_keys=False))
        (self.folder / ".env").write_text("NANOHPC_SLACK_WEBHOOK=https://hooks.example.org/x\n")
        self.machine("front", "active", "1700003500", "1699990000")
        self.machine("login2", "inactive", "1700003400", "1699980000")
        self.deploys: list[tuple[list[str], bool, bool]] = []
        self.posts: list[str] = []
        self.answer = "login2"

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key)
            else:
                os.environ[key] = value

    def machine(self, name: str, slurmctld: str, state: str, nightly: str) -> None:
        (self.folder / f"{name}.status").write_text(STATUS.format(slurmctld=slurmctld, state=state, nightly=nightly))

    def calls(self) -> list[str]:
        path = self.folder / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def steps(self) -> list[str]:
        """The switch's steps on the machines, in order, as `machine: step [--dry-run]`."""
        found = []
        for call in self.calls():
            target, command = call.split(": ", 1)
            if "nanohpc-front-switch-step" in command:
                words = command.split()
                step = words[words.index("/usr/local/sbin/nanohpc-front-switch-step") + 1]
                found.append(f"{target}: {step}" + (" --dry-run" if "--dry-run" in words else ""))
        return found

    def deploy(self, config: dict[str, Any], dry_run: bool) -> int:
        roles = [f"{name}={','.join(machine['roles'])}" for name, machine in config["machines"].items()]
        self.deploys.append((roles, dry_run, config["agent"]["enabled"]))
        return 0

    def switch(self, planned: bool) -> int:
        return front_switch.switch(
            self.path,
            "login2",
            None,
            planned,
            lambda _: self.answer,
            self.deploy,
            lambda _, text: self.posts.append(text),
        )

    def test_status_shows_both_machines_and_the_age_of_the_copies(self) -> None:
        lines = front_switch.status_lines(load_config(self.path, False, False)[0], None)
        text = "\n".join(lines)
        self.assertIn("front (front node, 192.168.104.10): answers; Slurm controller active", text)
        self.assertIn("login2 (backup front node, 192.168.104.30): answers; Slurm controller inactive", text)
        self.assertIn("Slurm state copy: 3 minutes old", text)
        self.assertIn("nightly copy: 6 hours old", text)
        (self.folder / "front.down").write_text("")
        text = "\n".join(front_switch.status_lines(load_config(self.path, False, False)[0], None))
        self.assertIn("front (front node, 192.168.104.10): does not answer (SSH failed", text)
        self.assertTrue(all("nanohpc-front-switch-step" not in call for call in self.calls()))

    def test_a_wrong_name_changes_nothing(self) -> None:
        before = self.path.read_text()
        self.answer = "front"
        self.assertEqual(self.switch(False), 1)
        self.assertEqual(self.steps(), [])
        self.assertEqual(self.deploys, [])
        self.assertEqual(self.path.read_text(), before)

    def test_switch_when_the_front_node_does_not_answer(self) -> None:
        (self.folder / "front.down").write_text("")
        self.assertEqual(self.switch(False), 0)
        self.assertEqual(self.steps(), ["login2: take --dry-run", "login2: take"])
        # Both deploys leave out the old front node, and the Slack agent is not set up by the switch.
        without_front = (["gpu4=compute", "gpu2=compute", "cpu1=compute", "gpu4i=compute", "store=backup",
                          "homesrv=home", "login2=front"], True, False)  # fmt: skip
        self.assertEqual(self.deploys, [without_front, (without_front[0], False, False)])
        # cluster.yml now names the backup front node as the front node; the old one is the backup front node.
        machines = load_config(self.path, False, False)[0]["machines"]
        self.assertEqual(machines["login2"]["roles"], ["front"])
        self.assertFalse(machines["login2"]["front_backup"])
        self.assertEqual(machines["front"]["roles"], ["login"])
        self.assertTrue(machines["front"]["front_backup"])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("ssh USER@192.168.104.30", self.posts[0])

    def test_switch_when_the_front_node_answers_stops_it_first(self) -> None:
        self.assertEqual(self.switch(False), 0)
        self.assertEqual(
            self.steps(), ["front: stop --dry-run", "login2: take --dry-run", "front: stop", "login2: take"]
        )
        self.assertEqual([dry_run for _, dry_run, _ in self.deploys], [True, False])
        self.assertIn("front=login", self.deploys[0][0])

    def test_a_failed_dry_run_changes_nothing(self) -> None:
        before = self.path.read_text()
        (self.folder / "login2.dryfail").write_text("")
        self.assertEqual(self.switch(False), 1)
        self.assertEqual(self.steps(), ["front: stop --dry-run", "login2: take --dry-run"])
        self.assertEqual(self.deploys, [])
        self.assertEqual(self.path.read_text(), before)

    def test_a_failed_deploy_dry_run_changes_nothing(self) -> None:
        before = self.path.read_text()
        self.deploy = lambda config, dry_run: 3  # type: ignore[method-assign]
        self.assertEqual(self.switch(False), 1)
        self.assertEqual(self.steps(), ["front: stop --dry-run", "login2: take --dry-run"])
        self.assertEqual(self.path.read_text(), before)

    def test_moving_back_needs_the_current_front_node(self) -> None:
        (self.folder / "front.down").write_text("")
        self.assertEqual(self.switch(True), 1)
        self.assertEqual(self.steps(), [])

    def test_the_backup_front_node_must_answer(self) -> None:
        (self.folder / "login2.down").write_text("")
        self.assertEqual(self.switch(False), 1)
        self.assertEqual(self.steps(), [])

    def test_only_the_backup_front_node_can_take_over(self) -> None:
        code = front_switch.switch(self.path, "gpu2", None, False, lambda _: "gpu2", self.deploy, lambda _, t: None)
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), [])

    def test_refused_with_automatic_deploys(self) -> None:
        raw = yaml.safe_load(self.path.read_text())
        raw["auto_deploy"] = {"enabled": True, "repository": "git@github.com:lab/config.git"}
        raw["nanohpc_version"] = "0.0.1"
        self.path.write_text(yaml.safe_dump(raw, sort_keys=False))
        self.assertEqual(self.switch(False), 1)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
