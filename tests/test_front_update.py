"""Front-node updates through the public nanoHPC command."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_compute_update import FAKE_PREVIEW_SSH, ROOT  # pyrefly: ignore[missing-import]
from tests.test_probe import write_command  # pyrefly: ignore[missing-import]
from tests.test_restart_check import facts  # pyrefly: ignore[missing-import]

FAKE_FRONT_SSH = (
    FAKE_PREVIEW_SSH.replace(
        "elif 'scontrol show node' in command:\n    print('NodeName=cpu1 State=' + state.get('node_state', 'IDLE'))",
        """elif 'scontrol show node' in command:
    node = command.split('scontrol show node ', 1)[1].split()[0]
    status = state.get('nodes', {}).get(node, 'IDLE')
    reason = ' Reason=nanohpc-front-update' if status == 'DRAIN' and node in state.get('owned', []) else ''
    print('NodeName=' + node + ' State=' + status + reason)""",
    )
    .replace(
        "elif 'state=drain' in command:\n    state['node_state'] = 'DRAIN'\n    save()",
        """elif 'state=drain' in command:
    node = command.split('nodename=', 1)[1].split()[0]
    state.setdefault('nodes', {})[node] = 'DRAIN'
    state.setdefault('owned', []).append(node)
    save()
    if os.environ.get('FRONT_FIRST_DRAIN_LOSES_REPLY') and len(state['owned']) == 1:
        sys.exit(255)""",
    )
    .replace(
        "elif 'state=resume' in command:\n    state['node_state'] = 'IDLE'\n    save()",
        """elif 'state=resume' in command:
    node = command.split('nodename=', 1)[1].split()[0]
    state['resume_count'] = state.get('resume_count', 0) + 1
    if os.environ.get('FRONT_FAIL_SECOND_RESUME') and state['resume_count'] == 2:
        sys.exit(3)
    state.setdefault('nodes', {})[node] = (
        'ALLOCATED' if os.environ.get('FRONT_ALLOCATED_AFTER_RESUME') and state['resume_count'] == 1 else 'IDLE'
    )
    save()""",
    )
    .replace(
        "elif 'squeue' in command:\n    pass",
        """elif 'squeue' in command:
    if os.environ.get('FRONT_RESUME_BEFORE_INSTALL') and state.get('owned'):
        state['nodes']['cpu1'] = 'IDLE'
        save()
    if os.environ.get('FRONT_JOB_STARTS_DURING_DRAIN') and state.get('owned'):
        print('123 RUNNING')
    elif state.get('pending_after_drain') and state.get('owned'):
        print('124 PENDING')
    elif not state.get('owned') and state.get('queue_busy'):
        print('123 RUNNING')""",
    )
    .replace(
        "elif 'apt-get install' in command:\n    state['installed'] = True\n    save()",
        """elif 'apt-get install' in command:
    state['installed'] = True
    if os.environ.get('FRONT_NODE_DOWN_ON_INSTALL'):
        state['nodes']['cpu1'] = 'DOWN'
    save()""",
    )
    .replace(
        "elif 'sshd -t' in command or 'systemctl is-active slurmd' in command:",
        """elif 'systemctl is-active' in command:
    if os.environ.get('FRONT_BAD_SERVICE') and os.environ['FRONT_BAD_SERVICE'] in command and state.get('installed'):
        print('failed')
        sys.exit(3)
    print('active')
elif 'scontrol ping' in command:
    print('UP')
elif 'sacctmgr -n -P list cluster' in command:
    print('labcluster|')
elif 'curl --silent --fail --insecure --resolve' in command:
    print('{{}}')
elif 'findmnt -n -o SOURCE --mountpoint /home' in command:
    print('/dev/vdb' if target == 'front' else '192.168.104.20:/home')
elif 'blkid -s UUID -o value' in command:
    print('expected-home-uuid')
elif 'findmnt -n -o UUID --mountpoint /home' in command:
    print('wrong-home-uuid' if os.environ.get('FRONT_WRONG_HOME_UUID') else 'expected-home-uuid')
elif 'exportfs -v' in command:
    print('/home')
elif 'sshd -t' in command or 'systemctl is-active slurmd' in command:""",
    )
)


class FrontUpdateCommandTest(unittest.TestCase):
    """Exercise dry run and confirmed front-node updates with fake SSH targets."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        write_command(bin_folder, "ssh", FAKE_FRONT_SSH)
        self.log = self.folder / "ssh.log"
        self.state = self.folder / "machine.json"
        self.state.write_text(json.dumps({"nodes": {"cpu1": "IDLE", "gpu4": "IDLE", "gpu2": "DRAIN", "gpu4i": "IDLE"}}))
        machine_facts = self.folder / "facts.json"
        machine_facts.write_text(json.dumps(facts()))
        self.environment = {
            **os.environ,
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "UPDATE_SSH_LOG": str(self.log),
            "UPDATE_FAKE_STATE": str(self.state),
            "UPDATE_FAKE_FACTS": str(machine_facts),
            "XDG_STATE_HOME": str(self.folder / "state"),
        }

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Invoke the installed entry point with the example Slurm cluster."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "front",
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def calls(self) -> list[str]:
        """Return the SSH commands in execution order."""
        return [json.loads(line)[1] for line in self.log.read_text().splitlines()]

    def test_ordinary_front_update_runs_while_jobs_exist(self) -> None:
        state = json.loads(self.state.read_text())
        state["queue_busy"] = True
        self.state.write_text(json.dumps(state))
        preview = self.command("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        applied = self.command("--confirm", "front")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertTrue(json.loads(self.state.read_text())["installed"])
        self.assertFalse(any("state=drain" in call for call in self.calls()))
        self.assertFalse(any("squeue" in call for call in self.calls()))

    def test_care_group_requires_empty_queue_before_draining(self) -> None:
        state = json.loads(self.state.read_text())
        state["queue_busy"] = True
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("queue", applied.stderr)
        calls = self.calls()
        self.assertFalse(any("state=drain" in call for call in calls))
        self.assertFalse(any("apt-get install" in call for call in calls))

    def test_care_group_drains_compute_and_restores_only_owned_nodes(self) -> None:
        state = json.loads(self.state.read_text())
        state["pending_after_drain"] = True
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        calls = self.calls()
        drained = [call for call in calls if "state=drain" in call]
        resumed = [call for call in calls if "state=resume" in call]
        self.assertEqual(len(drained), 3)
        self.assertEqual(len(resumed), 3)
        self.assertFalse(any("nodename=gpu2" in call for call in [*drained, *resumed]))
        install = next(i for i, call in enumerate(calls) if "apt-get install" in call)
        self.assertTrue(all(calls.index(call) < install for call in drained))
        self.assertTrue(all(calls.index(call) > install for call in resumed))
        self.assertTrue(all(call != "maintenance lock released" for call in calls[: calls.index(resumed[-1])]))
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["gpu2"], "DRAIN")

    def test_job_start_during_drain_stops_before_install_and_leaves_nodes_drained(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_JOB_STARTS_DURING_DRAIN"] = "1"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("started", applied.stderr)
        self.assertFalse(any("apt-get install" in call for call in self.calls()))
        self.assertFalse(any("state=resume" in call for call in self.calls()))
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["cpu1"], "DRAIN")

    def test_drain_that_loses_ssh_reply_is_reported_and_reconciled(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_FIRST_DRAIN_LOSES_REPLY"] = "1"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("gpu4", applied.stderr)
        self.assertIn("drained", applied.stderr)
        self.assertTrue(any("scontrol show node gpu4" in call for call in self.calls()))
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["gpu4"], "DRAIN")

    def test_uncertain_node_state_is_not_resumed(self) -> None:
        state = json.loads(self.state.read_text())
        state["nodes"]["cpu1"] = "IDLE+NOT_RESPONDING"
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("IDLE+NOT_RESPONDING", applied.stderr)
        self.assertFalse(any("state=resume" in call for call in self.calls()))
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["cpu1"], "IDLE+NOT_RESPONDING")

    def test_owned_drain_is_checked_again_before_install(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_RESUME_BEFORE_INSTALL"] = "1"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("state changed", applied.stderr)
        self.assertFalse(any("apt-get install" in call for call in self.calls()))
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["cpu1"], "DRAIN")

    def test_failure_does_not_overwrite_node_that_went_down(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_NODE_DOWN_ON_INSTALL"] = "1"
        self.environment["FRONT_BAD_SERVICE"] = "slurmctld"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["cpu1"], "DOWN")
        self.assertEqual(sum("nodename=cpu1 state=drain" in call for call in self.calls()), 1)

    def test_wrong_home_disk_keeps_compute_nodes_drained(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_WRONG_HOME_UUID"] = "1"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("/home", applied.stderr)
        self.assertFalse(any("state=resume" in call for call in self.calls()))

    def test_failed_front_service_keeps_nodes_drained(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_BAD_SERVICE"] = "slurmctld"
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("slurmctld", applied.stderr)
        self.assertFalse(any("state=resume" in call for call in self.calls()))

    def test_inactive_node_exporter_keeps_nodes_drained(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_BAD_SERVICE"] = "nanohpc-node-exporter"
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("nanohpc-node-exporter", applied.stderr)
        self.assertEqual(json.loads(self.state.read_text())["nodes"]["cpu1"], "DRAIN")

    def test_failed_partial_resume_redrains_node_with_new_allocation(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["FRONT_ALLOCATED_AFTER_RESUME"] = "1"
        self.environment["FRONT_FAIL_SECOND_RESUME"] = "1"
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        nodes = json.loads(self.state.read_text())["nodes"]
        self.assertEqual(nodes["gpu4"], "DRAIN")
        self.assertEqual(nodes["cpu1"], "DRAIN")
        self.assertEqual(nodes["gpu2"], "DRAIN")

    def test_lost_lock_does_not_change_slurm_without_reacquiring_it(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["UPDATE_FAKE_LOCK_LOSS_ON_INSTALL"] = "1"
        self.environment["UPDATE_FAKE_LOCK_BUSY_AFTER_INSTALL"] = "1"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("review Slurm state manually", applied.stderr)
        calls = self.calls()
        released = next(index for index, call in enumerate(calls) if call == "maintenance lock released")
        self.assertFalse(any("state=drain" in call or "state=resume" in call for call in calls[released + 1 :]))

    def test_front_restart_is_reported_without_rebooting(self) -> None:
        state = json.loads(self.state.read_text())
        state["restart_needed"] = True
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.command("--dry-run").returncode, 0)
        self.log.write_text("")
        applied = self.command("--confirm", "front")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("restart required", applied.stdout)
        self.assertFalse(any("systemctl reboot" in call for call in self.calls()))

    def test_failed_fresh_root_login_leaves_ssh_undo_and_nodes_drained(self) -> None:
        self.assertEqual(self.command("--include", "ssh", "--dry-run").returncode, 0)
        self.environment["UPDATE_FAKE_FAIL_LOGIN"] = "root"
        self.log.write_text("")
        applied = self.command("--include", "ssh", "--confirm", "front")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("root login", applied.stderr)
        state = json.loads(self.state.read_text())
        self.assertTrue(state["undo_armed"])
        self.assertEqual(state["nodes"]["cpu1"], "DRAIN")
        self.assertFalse(any("state=resume" in call for call in self.calls()))


class StorageUpdateCommandTest(unittest.TestCase):
    """A backup machine uses the confirmed update path and pauses new jobs during installation."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        fake = FAKE_FRONT_SSH.replace(
            "'192.168.104.13' if target == 'cpu1' else '192.168.104.10'",
            "'192.168.104.13' if target == 'cpu1' else '192.168.104.20' if target == 'store' else '192.168.104.10'",
        )
        write_command(bin_folder, "ssh", fake)
        self.log = self.folder / "ssh.log"
        self.state = self.folder / "machine.json"
        self.state.write_text(json.dumps({"nodes": {"cpu1": "IDLE", "gpu4": "IDLE", "gpu2": "DRAIN", "gpu4i": "IDLE"}}))
        machine_facts = self.folder / "facts.json"
        machine_facts.write_text(json.dumps(facts()))
        self.environment = {
            **os.environ,
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "UPDATE_SSH_LOG": str(self.log),
            "UPDATE_FAKE_STATE": str(self.state),
            "UPDATE_FAKE_FACTS": str(machine_facts),
            "XDG_STATE_HOME": str(self.folder / "state"),
        }

    def calls(self) -> list[str]:
        """Read the fake SSH commands in order."""
        return [json.loads(line)[1] for line in self.log.read_text().splitlines()]

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the public command against the configured backup machine."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "store",
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def test_backup_update_pauses_and_restores_scheduling(self) -> None:
        """Only available nodes are drained, then resumed after the backup machine passes checks."""
        preview = self.command("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        applied = self.command("--confirm", "store")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        calls = self.calls()
        self.assertTrue(any("state=drain" in call for call in calls))
        self.assertTrue(any("apt-get install" in call for call in calls))
        self.assertTrue(any("state=resume" in call for call in calls))

    def test_bad_backup_access_leaves_nodes_drained(self) -> None:
        """The backup machine stays isolated when its fresh root login fails after an update."""
        self.assertEqual(self.command("--dry-run").returncode, 0)
        self.environment["UPDATE_FAKE_FAIL_LOGIN"] = "root"
        applied = self.command("--confirm", "store")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("root login", applied.stderr)
        nodes = json.loads(self.state.read_text())["nodes"]
        self.assertEqual(nodes["cpu1"], "DRAIN")
        self.assertFalse(any("state=resume" in call for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
