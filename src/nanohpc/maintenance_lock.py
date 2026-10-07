"""Hold the coordinator's maintenance lock through one persistent root SSH process."""

import fcntl
import os
import select
import shlex
import signal
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import TracebackType
from typing import Self

LOCK_PATH = Path("/run/nanohpc/maintenance.lock")
_ACTIVE_LOCK: ContextVar["MaintenanceLock | None"] = ContextVar("nanohpc_maintenance_lock", default=None)

# This source runs as root on the coordinator. The path argument also lets the
# focused test run the exact holder against a temporary file without sudo.
HOLDER_PROGRAM = r"""
import fcntl
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
stable_path = pathlib.Path('/run/nanohpc/maintenance.lock')
if path == stable_path and os.geteuid() != 0:
    raise RuntimeError('maintenance lock holder must run as root')
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
directory = path.parent.lstat()
if not stat.S_ISDIR(directory.st_mode):
    raise RuntimeError('maintenance lock directory is not a directory')
if path == stable_path and (directory.st_uid != 0 or directory.st_mode & 0o077):
    raise RuntimeError('maintenance lock directory is not root-owned and private')
descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
information = os.fstat(descriptor)
if not stat.S_ISREG(information.st_mode):
    raise RuntimeError('maintenance lock path is not a regular file')
if path == stable_path and information.st_uid != 0:
    raise RuntimeError('maintenance lock file is not root-owned')
os.fchmod(descriptor, 0o600)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('BUSY', flush=True)
    raise SystemExit(2)
print('READY', flush=True)
sys.stdin.buffer.read()
"""


class MaintenanceLockError(RuntimeError):
    """The coordinator maintenance lock could not be held."""


class MaintenanceLockBusy(MaintenanceLockError):
    """Another maintenance operation already holds the coordinator lock."""


class MaintenanceLockLost(MaintenanceLockError):
    """The remote holder exited after the lock was acquired."""


class MaintenanceLock:
    """One acquired lock, released by closing its SSH process."""

    def __init__(self, machine: str, process: subprocess.Popen[bytes]) -> None:
        self.machine = machine
        self.process = process
        self._closed = False

    def check(self) -> None:
        """Raise if the holder has exited or this connection was closed."""
        if self._closed or self.process.poll() is not None:
            raise MaintenanceLockLost(f"{self.machine}: maintenance lock holder closed")

    def close(self) -> None:
        """Close SSH stdin so the remote holder releases flock, then reap SSH."""
        if self._closed:
            return
        self._closed = True
        _close_stdin(self.process)
        _reap(self.process)

    def __enter__(self) -> Self:
        self.check()
        return self

    def __exit__(
        self, _type: type[BaseException] | None, _value: BaseException | None, _traceback: TracebackType | None
    ) -> None:
        self.close()


def _close_stdin(process: subprocess.Popen[bytes]) -> None:
    """Close input even if the remote SSH command has already exited."""
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Wait briefly for SSH to close, then stop a stuck process."""
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()


def _response(process: subprocess.Popen[bytes], seconds: int) -> str:
    """Read one short handshake line without waiting forever for SSH or sudo."""
    assert process.stdout is not None
    deadline = time.monotonic() + seconds
    answer = bytearray()
    while len(answer) < 32:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            return ""
        letter = os.read(process.stdout.fileno(), 1)
        if not letter:
            return ""
        if letter == b"\n":
            return answer.decode("ascii", errors="replace")
        answer.extend(letter)
    return ""


def acquire(machine: str, ssh_config: Path | None, sudo_password: str | None) -> MaintenanceLock:
    """Acquire the coordinator lock via forwarded-key or password-authenticated sudo."""
    remote = shlex.join(["sudo", "-S", "-p", "", "python3", "-u", "-c", HOLDER_PROGRAM, str(LOCK_PATH)])
    config = ["-F", str(ssh_config)] if ssh_config is not None else []
    arguments = [
        "ssh",
        *config,
        "-A",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        machine,
        remote,
    ]
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None
    if sudo_password is not None:
        try:
            process.stdin.write(sudo_password.encode() + b"\n")
            process.stdin.flush()
        except BrokenPipeError:
            _close_stdin(process)
            _reap(process)
            raise MaintenanceLockError(
                f"{machine}: maintenance lock holder closed during sudo authentication"
            ) from None
    answer = _response(process, 30)
    if answer == "READY":
        return MaintenanceLock(machine, process)
    _close_stdin(process)
    _reap(process)
    if answer == "BUSY":
        raise MaintenanceLockBusy(f"{machine}: another maintenance operation is running")
    raise MaintenanceLockError(f"{machine}: maintenance lock holder did not confirm READY")


@contextmanager
def active(lock: MaintenanceLock) -> Iterator[None]:
    """Scope the lock to subprocess calls in the current execution context."""
    lock.check()
    token = _ACTIVE_LOCK.set(lock)
    try:
        yield
    finally:
        _ACTIVE_LOCK.reset(token)


def _stop_group(process: subprocess.Popen[str]) -> None:
    """Stop the child session and all processes it started."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate(timeout=5)


def guarded_run(
    arguments: Sequence[str],
    *,
    input: str | None,
    capture_output: bool,
    text: bool,
    timeout: float | None,
    check: bool,
    env: Mapping[str, str] | None,
    cwd: Path | None,
) -> subprocess.CompletedProcess[str]:
    """Run a child, stopping its process group if the active holder is lost."""
    lock = _ACTIVE_LOCK.get()
    if lock is None:
        return subprocess.run(
            arguments,
            input=input,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
            env=env,
            cwd=cwd,
        )
    lock.check()
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    pending_input = input
    while True:
        try:
            lock.check()
        except MaintenanceLockLost:
            _stop_group(process)
            raise
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            _stop_group(process)
            raise subprocess.TimeoutExpired(arguments, timeout)
        interval = 0.1 if remaining is None else min(0.1, remaining)
        try:
            stdout, stderr = process.communicate(input=pending_input, timeout=interval)
        except subprocess.TimeoutExpired:
            pending_input = None
            continue
        try:
            lock.check()
        except MaintenanceLockLost:
            _stop_group(process)
            raise
        result = subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def validate_inherited(fd: int, path: Path) -> None:
    """Require a private regular lock file and a descriptor holding its flock."""
    try:
        file_info = os.fstat(fd)
        path_info = path.lstat()
        directory_info = path.parent.lstat()
    except OSError as error:
        raise MaintenanceLockError(f"invalid inherited maintenance lock: {error.strerror}") from None
    if not stat.S_ISREG(file_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise MaintenanceLockError("inherited maintenance lock is not a regular file")
    if (file_info.st_dev, file_info.st_ino) != (path_info.st_dev, path_info.st_ino):
        raise MaintenanceLockError("inherited maintenance lock does not match its path")
    expected_owner = 0 if path == LOCK_PATH else os.geteuid()
    if file_info.st_uid != expected_owner or file_info.st_mode & 0o077 or file_info.st_nlink != 1:
        raise MaintenanceLockError("inherited maintenance lock file is not private")
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != expected_owner
        or directory_info.st_mode & 0o077
    ):
        raise MaintenanceLockError("inherited maintenance lock directory is not private")
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        probe_info = os.fstat(probe)
        if (probe_info.st_dev, probe_info.st_ino) != (file_info.st_dev, file_info.st_ino):
            raise MaintenanceLockError("inherited maintenance lock changed on disk")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise MaintenanceLockError("inherited maintenance lock is not held")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MaintenanceLockError("inherited maintenance lock belongs to another process") from None
    finally:
        os.close(probe)
