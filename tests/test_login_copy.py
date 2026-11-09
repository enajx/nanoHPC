"""The login banner's data on login nodes: the front node's nanohpc-login-copy sends the monitoring snapshot and the
home usage boxes; the login node's nanohpc-login-copy-receive (root's forced command for that key) accepts only
those two destinations. Run through the real scripts with a fake rsync that records its arguments."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND = ROOT / "src/nanohpc/files/nanohpc-login-copy"
RECEIVE = ROOT / "src/nanohpc/files/nanohpc-login-copy-receive"
SNAPSHOT_LINE = (
    "rsync --server -logDtpre.iLsfxCIvu --timeout=60 --numeric-ids . /var/lib/nanohpc-login-copy/status.json"
)
NOTICES_LINE = (
    "rsync --server -logDtpre.iLsfxCIvu --timeout=60 --delete --numeric-ids . /var/lib/nanohpc-login-copy/home-notices/"
)


def fake_rsync(folder: Path, exit_code: int) -> dict[str, str]:
    """A fake rsync first on PATH that appends its arguments, one call per line, to folder/calls."""
    bin_folder = folder / "bin"
    bin_folder.mkdir()
    program = bin_folder / "rsync"
    program.write_text(f"#!/bin/sh\necho \"$*\" >> '{folder / 'calls'}'\nexit {exit_code}\n")
    program.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}"}


class ReceiveTest(unittest.TestCase):
    def receive(self, command: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**fake_rsync(Path(directory), 0), "SSH_ORIGINAL_COMMAND": command}
            return subprocess.run([sys.executable, str(RECEIVE)], env=environment, capture_output=True, text=True,
                                  check=False)  # fmt: skip

    def test_the_two_copies_are_received(self) -> None:
        for command in (SNAPSHOT_LINE, NOTICES_LINE):
            with self.subTest(command):
                result = self.receive(command)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_anything_else_is_refused(self) -> None:
        for command in (
            "",
            "id",
            SNAPSHOT_LINE.replace("status.json", "../../../etc/shadow"),
            SNAPSHOT_LINE.replace("/var/lib/nanohpc-login-copy/status.json", "/root/.ssh/authorized_keys"),
            "rsync --server --sender -logDtpre.iLsfxCIvu . /var/lib/nanohpc-login-copy/status.json",
            SNAPSHOT_LINE.replace("--timeout=60", "--temp-dir=/tmp"),
            SNAPSHOT_LINE + "; id",
            "rsync --server . /var/lib/nanohpc-login-copy/status.json",
        ):
            with self.subTest(command):
                result = self.receive(command)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("refused", result.stderr)


class SendTest(unittest.TestCase):
    def send(self, folder: Path, exit_code: int, snapshot: bool) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        notices = folder / "notices"
        notices.mkdir()
        (notices / "alice").write_text("box\n")
        if snapshot:
            (folder / "status.json").write_text("{}")
        result = subprocess.run(
            [sys.executable, str(SEND), "--login", "login1=10.0.0.51", "--login", "login2=10.0.0.52", "--key", "k",
             "--known-hosts", "h", "--snapshot", str(folder / "status.json"), "--notices", str(notices)],
            env=fake_rsync(folder, exit_code), capture_output=True, text=True, check=False,
        )  # fmt: skip
        calls = (folder / "calls").read_text().splitlines() if (folder / "calls").exists() else []
        return result, calls

    def test_each_login_node_gets_the_snapshot_and_the_usage_boxes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.send(Path(directory), 0, True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 4)
            self.assertTrue(calls[0].endswith("root@10.0.0.51:/var/lib/nanohpc-login-copy/status.json"))
            self.assertIn("--delete", calls[1])
            self.assertTrue(calls[1].endswith("notices/ root@10.0.0.51:/var/lib/nanohpc-login-copy/home-notices/"))
            self.assertIn("StrictHostKeyChecking=yes", calls[0])

    def test_no_snapshot_yet_sends_only_the_usage_boxes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.send(Path(directory), 0, False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 2)

    def test_a_failed_copy_fails_the_run_after_trying_every_login_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.send(Path(directory), 12, True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("login1, login2", result.stderr)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
