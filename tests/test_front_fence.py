"""The front pair's fence (nanohpc-front-fence): a front node that comes back while the other machine is the active
front node keeps its Slurm controller and accounting stopped. Run through the real script, with a real listening
port standing in for the other machine's Slurm controller and a fake systemctl that records its arguments."""

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FENCE = ROOT / "src/nanohpc/files/nanohpc-front-fence"


class FenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.slurm_conf = self.folder / "slurm.conf"
        self.slurm_conf.write_text("ClusterName=lab\nSlurmctldHost=front(192.168.104.10)\n")
        self.calls = self.folder / "calls"
        systemctl = self.folder / "systemctl"
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
            "--stop", "slurmctld.service", "--stop", "slurmdbd.service", *mode,
        ]  # fmt: skip
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def test_a_front_node_that_comes_back_stays_quiet(self) -> None:
        """The other machine's controller answers: the watch fences this machine and stops its services, and from
        then on they do not start (the check fails) until the fence is released."""
        self.peer_answers()
        result = self.fence("--watch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("login2", self.marker.read_text())
        self.assertEqual(self.calls.read_text(), "stop slurmctld.service slurmdbd.service\n")
        self.listener.close()
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

    def test_hold_fences_without_stopping(self) -> None:
        self.assertEqual(self.fence("--hold", "switched to login2").returncode, 0)
        self.assertEqual(self.marker.read_text(), "switched to login2\n")
        self.assertEqual(self.fence("--check").returncode, 1)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
