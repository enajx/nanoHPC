"""The front pair's fence (nanohpc-front-fence): a front node that comes back while the other machine is the active
front node keeps its Slurm controller and accounting stopped. Run through the real script, with a real listening
port standing in for the other machine's Slurm controller and a fake systemctl that records its arguments."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FENCE = ROOT / "src/nanohpc/files/nanohpc-front-fence"


class FenceBase(unittest.TestCase):
    """A fake machine for the fence: its slurm.conf, a fake systemctl, and a port standing in for the other one."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.slurm_conf = self.folder / "slurm.conf"
        self.slurm_conf.write_text("ClusterName=lab\nSlurmctldHost=front(192.168.104.10)\n")
        self.calls = self.folder / "calls"
        systemctl = self.folder / "systemctl"
        # Every unit runs (is-active succeeds).
        systemctl.write_text(f"#!/bin/sh\necho \"$*\" >> '{self.calls}'\n")
        systemctl.chmod(0o755)
        self.marker = self.folder / "fenced"
        self.took_over = self.folder / "took-over"
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]

    def tearDown(self) -> None:
        self.listener.close()

    def peer_answers(self) -> None:
        self.listener.listen()

    def fence(self, *mode: str) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable, str(FENCE), "--hostname", "front", "--slurm-conf", str(self.slurm_conf),
            "--peer", f"login2=127.0.0.1:{self.port}", "--marker", str(self.marker),
            "--took-over", str(self.took_over), "--systemctl", str(self.folder / "systemctl"),
            "--stop", "slurmctld.service", "--stop", "slurmdbd.service", "--name", "front", "--ask-key", "/key",
            "--ask-known-hosts", "/hosts", "--waiting", str(self.folder / "waiting"),
            "--record", str(self.folder / "no-record"), *mode,
        ]  # fmt: skip
        return subprocess.run(command, capture_output=True, text=True, check=False)


class FenceTest(FenceBase):
    def test_a_front_node_that_comes_back_stays_quiet(self) -> None:
        """The other machine's controller answers: the watch fences this machine and stops its services, and from
        then on they do not start (the check fails) until the fence is released."""
        self.peer_answers()
        result = self.fence("--watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("login2", self.marker.read_text())
        self.assertIn("stop slurmctld.service slurmdbd.service", self.calls.read_text().splitlines())
        self.listener.close()
        # While fenced, the watch keeps the front-node services stopped (also after a restart of the machine).
        self.calls.unlink()
        self.assertEqual(self.fence("--watch").returncode, 0)
        self.assertIn("stop slurmctld.service slurmdbd.service", self.calls.read_text().splitlines())
        self.assertEqual(self.fence("--check").returncode, 1)
        self.assertEqual(self.fence("--is-front").returncode, 1)
        self.assertEqual(self.fence("--release").returncode, 0)
        self.assertEqual(self.fence("--check").returncode, 0)

    def test_the_services_do_not_start_while_the_other_controller_answers(self) -> None:
        self.peer_answers()
        self.assertEqual(self.fence("--check").returncode, 1)
        self.assertTrue(self.marker.exists())

    def test_nothing_happens_while_the_other_machine_is_quiet(self) -> None:
        self.assertEqual(self.fence("--watch").returncode, 0)
        self.assertEqual(self.fence("--check").returncode, 0)
        self.assertEqual(self.fence("--is-front").returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.calls.exists())

    def test_a_machine_that_slurm_conf_does_not_name_does_nothing(self) -> None:
        """A backup front node (its slurm.conf names the front node) is never fenced and never sends copies."""
        self.slurm_conf.write_text("SlurmctldHost=login2(192.168.104.30)\n")
        self.peer_answers()
        self.assertEqual(self.fence("--watch").returncode, 0)
        self.assertEqual(self.fence("--check").returncode, 0)
        self.assertEqual(self.fence("--is-front").returncode, 1)
        self.assertFalse(self.marker.exists())

    def test_the_machine_that_took_over_is_not_fenced_by_the_watch(self) -> None:
        self.took_over.write_text("took over from login2\n")
        self.peer_answers()
        self.assertEqual(self.fence("--watch").returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_the_machine_that_took_over_starts_its_controller_whatever_the_other_one_does(self) -> None:
        """Both machines restart after a switch: a stale old front node that came up first must not fence the
        machine the switch made the front node."""
        self.took_over.write_text("took over from login2\n")
        self.peer_answers()
        self.assertEqual(self.fence("--check").returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_hold_fences_without_stopping(self) -> None:
        self.assertEqual(self.fence("--hold", "switched to login2").returncode, 0)
        self.assertEqual(self.marker.read_text(), "switched to login2\n")
        self.assertEqual(self.fence("--check").returncode, 1)
        self.assertFalse(self.calls.exists())


FAKE_SSH = """#!/bin/sh
for word in "$@"; do case "$word" in root@*) target=${word#root@} ;; esac; done
echo "ssh $target" >> "$FAKE/asked"
if [ -e "$FAKE/$target.answer" ]; then cat "$FAKE/$target.answer"; else echo "no route" >&2; exit 255; fi
"""


class AskTest(FenceBase):
    """Before the controller starts, the fence asks the other cluster machines which front node they were deployed
    with (mocked: a fake ssh first on PATH answers for each address from a file)."""

    def setUp(self) -> None:
        super().setUp()
        (self.folder / "bin").mkdir()
        (self.folder / "bin/ssh").write_text(FAKE_SSH)
        (self.folder / "bin/ssh").chmod(0o755)
        self.waiting = self.folder / "waiting"
        # This machine's own record: the front node it was deployed with, and when that front node took over.
        self.record = self.folder / "front-node"
        self.record.write_text("front 0\n")

    def answer(self, address: str, text: str) -> None:
        (self.folder / f"{address}.answer").write_text(text + "\n")

    def ask(self, *mode: str) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable, str(FENCE), "--hostname", "front", "--name", "front", "--slurm-conf", str(self.slurm_conf),
            "--peer", f"login2=127.0.0.1:{self.port}", "--marker", str(self.marker),
            "--took-over", str(self.took_over), "--systemctl", str(self.folder / "systemctl"),
            "--stop", "slurmctld.service", "--start", "slurmdbd.service", "--start", "slurmctld.service",
            "--ask", "gpu1=10.0.0.21", "--ask", "gpu2=10.0.0.22", "--ask-key", "/key", "--ask-known-hosts", "/hosts",
            "--waiting", str(self.waiting), "--record", str(self.record), *mode,
        ]  # fmt: skip
        environment = {**os.environ, "PATH": f"{self.folder / 'bin'}{os.pathsep}{os.environ['PATH']}",
                       "FAKE": str(self.folder)}  # fmt: skip
        return subprocess.run(command, env=environment, capture_output=True, text=True, check=False)

    def test_starts_when_the_machines_that_answer_name_it(self) -> None:
        self.answer("10.0.0.21", "front")
        result = self.ask("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpu1 names front", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_fences_itself_when_they_name_the_other_machine(self) -> None:
        """The old front node boots while the new one is off: the compute machines name the new one."""
        self.answer("10.0.0.21", "login2")
        self.answer("10.0.0.22", "login2")
        result = self.ask("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("login2", self.marker.read_text())

    def test_waits_while_no_machine_answers_and_starts_once_one_does(self) -> None:
        """A cold boot of the whole cluster: the controller waits, and the minute watch starts it later."""
        self.answer("10.0.0.21", "none")
        result = self.ask("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("waiting", result.stderr)
        self.assertTrue(self.waiting.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.ask("--watch").returncode, 0)
        self.assertFalse(self.calls.exists())
        self.answer("10.0.0.22", "front")
        self.assertEqual(self.ask("--watch").returncode, 0)
        self.assertIn("start slurmdbd.service slurmctld.service", self.calls.read_text().splitlines())
        self.assertFalse(self.waiting.exists())

    def test_a_machine_that_missed_the_switch_does_not_fence_the_new_front_node(self) -> None:
        """gpu1 was off during the switch that made this machine the front node: it still names the old one, with
        an older switch time. The newest record counts."""
        self.record.write_text("front 1700000000\n")
        self.answer("10.0.0.21", "login2 0")
        self.answer("10.0.0.22", "front 1700000000")
        result = self.ask("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        # Also when only the stale machine answers: this machine's own record is newer.
        (self.folder / "10.0.0.22.answer").unlink()
        self.assertEqual(self.ask("--check").returncode, 0)

    def test_the_old_front_node_follows_the_newest_record(self) -> None:
        """The old front node comes back: its own record and a stale machine name it, but a machine deployed by
        the later switch names the other one with a newer time."""
        self.answer("10.0.0.21", "front 0")
        self.answer("10.0.0.22", "login2 1700000000")
        self.assertEqual(self.ask("--check").returncode, 1)
        self.assertIn("login2", self.marker.read_text())

    def test_a_waiting_machine_is_fenced_when_the_other_controller_answers(self) -> None:
        self.peer_answers()
        self.assertEqual(self.ask("--check").returncode, 1)
        self.assertTrue(self.marker.exists())

    def test_the_machine_the_last_switch_made_front_node_does_not_wait(self) -> None:
        """No other machine answers, but this machine took over in the switch its record names."""
        self.record.write_text("front 1700000000\n")
        self.took_over.write_text("took over from login2 at 2026-11-13 18:00 since 1700000000\n")
        result = self.ask("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.waiting.exists())
        # A record from an older switch does not count.
        self.took_over.write_text("took over from login2 at 2026-11-01 18:00 since 1690000000\n")
        self.assertEqual(self.ask("--check").returncode, 1)

    def test_the_watch_does_not_start_a_controller_stopped_by_hand(self) -> None:
        self.answer("10.0.0.21", "front")
        self.assertEqual(self.ask("--watch").returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_the_watch_fences_a_waiting_machine_the_others_do_not_name(self) -> None:
        self.waiting.write_text("")
        self.answer("10.0.0.21", "login2")
        self.assertEqual(self.ask("--watch").returncode, 0)
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.waiting.exists())


if __name__ == "__main__":
    unittest.main()
