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
SNAPSHOT_LINE = "rsync --server -ogtpre.iLsfxCIvu --timeout=60 --numeric-ids . /var/lib/nanohpc-login-copy/status.json"
NOTICES_LINE = (
    "rsync --server -ogtpre.iLsfxCIvu --timeout=60 --delete-during --numeric-ids"
    " . /var/lib/nanohpc-login-copy/home-notices/"
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

    def test_received_files_are_plain_and_never_setuid(self) -> None:
        """After rsync, anything in the folder that is not a plain file or folder is removed, and setuid and setgid
        bits are dropped (whatever the sender sent)."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "copy"
            (folder / "home-notices").mkdir(parents=True)
            box = folder / "home-notices/alice"
            box.write_text("box\n")
            box.chmod(0o4755)
            (folder / "home-notices/escape").symlink_to("/etc/shadow")
            environment = {
                **fake_rsync(Path(directory), 0),
                "SSH_ORIGINAL_COMMAND": NOTICES_LINE.replace("/var/lib/nanohpc-login-copy", str(folder)),
                "NANOHPC_LOGIN_COPY_FOLDER": str(folder),
            }
            result = subprocess.run([sys.executable, str(RECEIVE)], env=environment, capture_output=True, text=True,
                                    check=False)  # fmt: skip
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(box.stat().st_mode & 0o7777, 0o755)
            self.assertFalse((folder / "home-notices/escape").is_symlink())

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
            # Links and device files are never received.
            SNAPSHOT_LINE.replace("-ogtpre", "-logtpre"),
            SNAPSHOT_LINE.replace("-ogtpre", "-ogDtpre"),
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
            self.assertNotIn("--archive", calls[0])
            self.assertIn("--recursive --times --perms --owner --group", calls[1])
            self.assertTrue(calls[1].endswith("notices/ root@10.0.0.51:/var/lib/nanohpc-login-copy/home-notices/"))
            self.assertIn("StrictHostKeyChecking=yes", calls[0])

    def test_a_login_node_with_its_own_home_gets_only_the_snapshot(self) -> None:
        """Its usage boxes are measured on it (its own /home), so the front node's boxes are not sent there."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            notices = folder / "notices"
            notices.mkdir()
            (folder / "status.json").write_text("{}")
            result = subprocess.run(
                [sys.executable, str(SEND), "--login", "login1=10.0.0.51", "--login", "login2=10.0.0.52",
                 "--own-home", "login2", "--key", "k", "--known-hosts", "h", "--snapshot", str(folder / "status.json"),
                 "--notices", str(notices)],
                env=fake_rsync(folder, 0), capture_output=True, text=True, check=False,
            )  # fmt: skip
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (folder / "calls").read_text().splitlines()
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[2].endswith("root@10.0.0.52:/var/lib/nanohpc-login-copy/status.json"))

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
