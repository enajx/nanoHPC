"""Exercise the coordinator maintenance lock and its persistent SSH boundary."""

import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nanohpc import maintenance_lock


class HolderFixture(unittest.TestCase):
    """Start the real holder against one temporary lock path."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.lock_path = Path(temporary.name) / "maintenance.lock"
        self.holders: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_holders)

    def stop_holders(self) -> None:
        """Close every test holder and wait for it to release its lock."""
        for process in self.holders:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def start_holder(self) -> subprocess.Popen[bytes]:
        """Run the same Python program sent to the coordinator, using a temp path."""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", maintenance_lock.HOLDER_PROGRAM, str(self.lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.holders.append(process)
        return process


class LocalHolderTest(HolderFixture):
    """The real holder must keep one file locked until its process exits."""

    def test_contention_and_close_release(self) -> None:
        first = self.start_holder()
        assert first.stdout is not None
        self.assertEqual(first.stdout.readline(), b"READY\n")
        second = self.start_holder()
        assert second.stdout is not None
        self.assertEqual(second.stdout.readline(), b"BUSY\n")
        self.assertEqual(second.wait(timeout=5), 2)
        assert first.stdin is not None
        first.stdin.close()
        self.assertEqual(first.wait(timeout=5), 0)
        third = self.start_holder()
        assert third.stdout is not None
        self.assertEqual(third.stdout.readline(), b"READY\n")
        self.assertTrue(self.lock_path.exists())

    def test_crash_releases_lock_without_unlinking(self) -> None:
        first = self.start_holder()
        assert first.stdout is not None
        self.assertEqual(first.stdout.readline(), b"READY\n")
        inode = self.lock_path.stat().st_ino
        first.kill()
        first.wait(timeout=5)
        second = self.start_holder()
        assert second.stdout is not None
        self.assertEqual(second.stdout.readline(), b"READY\n")
        self.assertEqual(self.lock_path.stat().st_ino, inode)


class SshLockTest(unittest.TestCase):
    """A fake SSH binary checks the acquisition protocol and connection lifetime."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        fake_ssh = self.folder / "ssh"
        fake_ssh.write_text(
            f"#!{sys.executable}\n"
            "import os, shlex, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "Path(os.environ['LOCK_SSH_ARGS']).write_text('\\n'.join(args))\n"
            "command = shlex.split(args[-1])\n"
            "assert command[:3] == ['sudo', '-S', '-p']\n"
            "assert command[3] == ''\n"
            "assert command[4:7] == ['python3', '-u', '-c']\n"
            "mode = os.environ['LOCK_SSH_MODE']\n"
            "if mode == 'busy': print('BUSY', flush=True); sys.exit(2)\n"
            "if mode == 'lost': print('READY', flush=True); sys.exit(0)\n"
            "if mode == 'password':\n"
            "    supplied = sys.stdin.buffer.readline()\n"
            "    assert supplied == b'first-deploy-password\\n'\n"
            "os.execv(sys.executable, [sys.executable, '-u', '-c', command[7], os.environ['LOCK_TEST_PATH']])\n"
        )
        fake_ssh.chmod(0o755)
        self.arguments = self.folder / "ssh-args"
        self.environment = {
            **os.environ,
            "PATH": f"{self.folder}:{os.environ['PATH']}",
            "LOCK_SSH_ARGS": str(self.arguments),
            "LOCK_SSH_MODE": "ready",
            "LOCK_TEST_PATH": str(self.folder / "maintenance.lock"),
        }

    def acquire(self, mode: str, password: str | None) -> maintenance_lock.MaintenanceLock:
        """Run acquisition with the selected fake SSH response."""
        environment = {**self.environment, "LOCK_SSH_MODE": mode}
        with patch.dict(os.environ, environment):
            return maintenance_lock.acquire("front", None, password)

    def test_ready_holds_until_close_and_forwards_admin_key(self) -> None:
        lock = self.acquire("ready", None)
        self.addCleanup(lock.close)
        lock.check()
        arguments = self.arguments.read_text().splitlines()
        self.assertIn("-A", arguments)
        self.assertIn("BatchMode=yes", arguments)
        self.assertIn("front", arguments)
        self.assertIn("/run/nanohpc/maintenance.lock", arguments[-1])
        self.assertTrue((self.folder / "maintenance.lock").exists())
        with self.assertRaises(maintenance_lock.MaintenanceLockBusy):
            self.acquire("ready", None)
        lock.close()
        with self.acquire("ready", None) as next_lock:
            next_lock.check()

    def test_supplied_password_is_sent_over_stdin_only(self) -> None:
        with self.acquire("password", "first-deploy-password") as lock:
            lock.check()
        self.assertNotIn("first-deploy-password", self.arguments.read_text())

    def test_busy_handshake(self) -> None:
        with self.assertRaises(maintenance_lock.MaintenanceLockBusy):
            self.acquire("busy", None)

    def test_lost_holder_is_detected(self) -> None:
        lock = self.acquire("lost", None)
        self.addCleanup(lock.close)
        lock.process.wait(timeout=5)
        with self.assertRaises(maintenance_lock.MaintenanceLockLost):
            lock.check()


class GuardedRunnerTest(HolderFixture):
    """The running child must stop when the active lock holder exits."""

    def test_captured_text_input_and_output(self) -> None:
        holder = self.start_holder()
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline(), b"READY\n")
        lock = maintenance_lock.MaintenanceLock("local", holder)
        with maintenance_lock.active(lock):
            result = maintenance_lock.guarded_run(
                [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                input="hello",
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
                env=None,
                cwd=None,
            )
        self.assertEqual(result.stdout, "HELLO\n")
        self.assertEqual(result.returncode, 0)

    def test_inherited_output_environment_and_working_directory(self) -> None:
        holder = self.start_holder()
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline(), b"READY\n")
        lock = maintenance_lock.MaintenanceLock("local", holder)
        with maintenance_lock.active(lock):
            result = maintenance_lock.guarded_run(
                [
                    sys.executable,
                    "-c",
                    "import os, pathlib; pathlib.Path('result').write_text(os.environ['LOCK_VALUE'])",
                ],
                input=None,
                capture_output=False,
                text=True,
                timeout=5,
                check=True,
                env={**os.environ, "LOCK_VALUE": "from-env"},
                cwd=self.lock_path.parent,
            )
        self.assertIsNone(result.stdout)
        self.assertEqual((self.lock_path.parent / "result").read_text(), "from-env")

    def test_timeout_stops_child(self) -> None:
        holder = self.start_holder()
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline(), b"READY\n")
        lock = maintenance_lock.MaintenanceLock("local", holder)
        pid_path = self.lock_path.with_name("timed-out.pid")
        child = "import os, pathlib, time, sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        with maintenance_lock.active(lock), self.assertRaises(subprocess.TimeoutExpired):
            maintenance_lock.guarded_run(
                [sys.executable, "-c", child, str(pid_path)],
                input=None,
                capture_output=True,
                text=True,
                timeout=0.3,
                check=False,
                env=None,
                cwd=None,
            )
        self.assertTrue(pid_path.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)

    def test_lost_holder_stops_long_child(self) -> None:
        holder = self.start_holder()
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline(), b"READY\n")
        lock = maintenance_lock.MaintenanceLock("local", holder)
        pid_path = self.lock_path.with_name("child.pid")
        child = "import os, pathlib, time, sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"

        def crash_holder() -> None:
            """Wait until the child is running before killing its lock holder."""
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            holder.kill()

        killer = threading.Thread(target=crash_holder)
        killer.start()
        try:
            with maintenance_lock.active(lock), self.assertRaises(maintenance_lock.MaintenanceLockLost):
                maintenance_lock.guarded_run(
                    [sys.executable, "-c", child, str(pid_path)],
                    input=None,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env=None,
                    cwd=None,
                )
        finally:
            killer.join(timeout=5)
        self.assertTrue(pid_path.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)


class InheritedDescriptorTest(unittest.TestCase):
    """An auto runner accepts only its actual locked, private file description."""

    def test_inherited_description_succeeds_and_wrong_fd_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "maintenance.lock"
            wrong_path = Path(folder) / "wrong.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            wrong = os.open(wrong_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                inherited = os.dup(descriptor)
                try:
                    maintenance_lock.validate_inherited(inherited, path)
                finally:
                    os.close(inherited)
                with self.assertRaises(maintenance_lock.MaintenanceLockError):
                    maintenance_lock.validate_inherited(wrong, path)
                separate = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(maintenance_lock.MaintenanceLockError):
                        maintenance_lock.validate_inherited(separate, path)
                finally:
                    os.close(separate)
            finally:
                os.close(descriptor)
                os.close(wrong)

    def test_unlocked_descriptor_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "maintenance.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with self.assertRaises(maintenance_lock.MaintenanceLockError):
                    maintenance_lock.validate_inherited(descriptor, path)
            finally:
                os.close(descriptor)

    def test_insecure_file_and_symlink_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "maintenance.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            self.addCleanup(os.close, descriptor)
            os.chmod(path, 0o644)
            with self.assertRaises(maintenance_lock.MaintenanceLockError):
                maintenance_lock.validate_inherited(descriptor, path)
            os.chmod(path, 0o600)
            link = Path(folder) / "linked.lock"
            link.symlink_to(path)
            with self.assertRaises(maintenance_lock.MaintenanceLockError):
                maintenance_lock.validate_inherited(descriptor, link)


if __name__ == "__main__":
    unittest.main()
