"""Keep SSH recovery available while a confirmed package update changes OpenSSH."""

import select
import subprocess
import time
from pathlib import Path

from nanohpc import restart
from nanohpc.probe import ssh_args

BACKUP = "/root/nanohpc-ssh-update-undo"
TIMER = "nanohpc-ssh-update-undo"

# Runs only if the systemd timer reaches 15 minutes without being canceled. The backup is root-only and
# contains package-owned files selected by dpkg plus /etc/ssh. Wait for apt/dpkg locks to clear so restoring
# a package never races an install that is still in progress.
UNDO_PROGRAM = r"""
import fcntl, os, pathlib, shutil, subprocess, tarfile, tempfile, time

backup = pathlib.Path('/root/nanohpc-ssh-update-undo')
if not backup.is_dir():
    raise SystemExit(0)
coordination = (backup / 'coordination.lock').open('a')
fcntl.flock(coordination, fcntl.LOCK_EX)
if not backup.is_dir():
    raise SystemExit(0)
locks = [pathlib.Path('/var/lib/dpkg/lock-frontend'), pathlib.Path('/var/lib/dpkg/lock')]
while True:
    opened = []
    busy = False
    for path in locks:
        descriptor = path.open('a')
        opened.append(descriptor)
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            busy = True
            break
    if not busy:
        break
    for descriptor in opened:
        descriptor.close()
    time.sleep(5)
# Keep both dpkg locks until SSH files have been restored and the listener checked.

def listener():
    service = subprocess.run(['systemctl', 'is-active', '--quiet', 'ssh.service']).returncode == 0
    socket = subprocess.run(['systemctl', 'is-active', '--quiet', 'ssh.socket']).returncode == 0
    port = subprocess.run(['ss', '-H', '-ltn', 'sport = :22'], capture_output=True, text=True)
    return (service or socket) and port.returncode == 0 and bool(port.stdout.strip())

def restore(files):
    shutil.rmtree('/etc/ssh')
    shutil.copytree(backup / 'etc-ssh', '/etc/ssh', symlinks=True)
    if files:
        with tarfile.open(backup / 'openssh-files.tar') as archive:
            with tempfile.TemporaryDirectory(dir=backup) as folder:
                archive.extractall(folder)
                for member in archive.getmembers():
                    if not (member.isfile() or member.issym() or member.islnk()):
                        continue
                    source = pathlib.Path(folder) / member.name
                    target = pathlib.Path('/') / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name('.' + target.name + '.nanohpc-ssh-undo')
                    temporary.unlink(missing_ok=True)
                    if source.is_symlink():
                        temporary.symlink_to(os.readlink(source))
                    else:
                        shutil.copy2(source, temporary)
                    os.chown(temporary, member.uid, member.gid, follow_symlinks=False)
                    os.replace(temporary, target)
    # A damaged sshd binary can exhaust systemd's start limit before undo fires.
    # Reset the service after restoring the binary; revive the socket only if it failed.
    socket_failed = subprocess.run(['systemctl', 'is-failed', '--quiet', 'ssh.socket']).returncode == 0
    subprocess.run(['systemctl', 'reset-failed', 'ssh.service'], check=True)
    if socket_failed:
        subprocess.run(['systemctl', 'reset-failed', 'ssh.socket'], check=True)
        if subprocess.run(['systemctl', 'is-enabled', '--quiet', 'ssh.socket']).returncode == 0:
            subprocess.run(['systemctl', 'restart', 'ssh.socket'], check=True)
    subprocess.run(['systemctl', 'restart', 'ssh.service'], check=True)
    return subprocess.run(['sshd', '-t']).returncode == 0 and listener()

# Fresh root and administrator logins can only be checked from the administrator's machine.
# Package metadata can still record the failed upgrade; keep the backup until an administrator repairs it.
(backup / 'needs-review').write_text('SSH undo ran; verify fresh logins and repair OpenSSH package state before clearing this backup.\n')
if not restore(True):
    raise RuntimeError('SSH restore did not leave a valid listener on port 22')
"""

# The backup and timer are created before apt installs anything. An existing timer or backup belongs to an
# unfinished earlier run, so a new SSH update must stop rather than overwrite its recovery files.
PREPARE_PROGRAM = r"""
# nanohpc-ssh-prepare
import pathlib, shutil, subprocess, tarfile

backup = pathlib.Path('/root/nanohpc-ssh-update-undo')
unit = 'nanohpc-ssh-update-undo'
timer = subprocess.run(['systemctl', 'is-active', '--quiet', unit + '.timer'])
service = subprocess.run(['systemctl', 'is-active', '--quiet', unit + '.service'])
if timer.returncode == 0 or service.returncode == 0 or backup.exists():
    raise RuntimeError('an earlier SSH undo is still present; check the machine by hand')
backup.mkdir(mode=0o700)
shutil.copytree('/etc/ssh', backup / 'etc-ssh', symlinks=True)
packages = ['openssh-server', 'openssh-client', 'openssh-sftp-server']
files = set()
for package in packages:
    result = subprocess.run(['dpkg-query', '-L', package], capture_output=True, text=True)
    if result.returncode:
        continue
    for line in result.stdout.splitlines():
        path = pathlib.Path(line)
        if path.is_absolute() and not str(path).startswith('/etc/ssh/') and (path.is_file() or path.is_symlink()):
            files.add(path)
with tarfile.open(backup / 'openssh-files.tar', 'w') as archive:
    for path in sorted(files):
        archive.add(path, arcname=str(path).lstrip('/'), recursive=False)
script = backup / 'undo.py'
script.write_text(UNDO_SOURCE)
script.chmod(0o700)
subprocess.run(['systemd-run', '--unit=' + unit, '--on-active=15min', '/usr/bin/python3', str(script)], check=True)
if subprocess.run(['systemctl', 'is-active', '--quiet', unit + '.timer']).returncode != 0:
    raise RuntimeError('SSH undo timer did not become active; check the machine by hand')
"""

CANCEL_PROGRAM = r"""
# nanohpc-ssh-cancel
import fcntl, pathlib, shutil, subprocess
backup = pathlib.Path('/root/nanohpc-ssh-update-undo')
coordination = (backup / 'coordination.lock').open('a')
try:
    fcntl.flock(coordination, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise RuntimeError('SSH undo has started; keep the backup and check the machine by hand')
unit = 'nanohpc-ssh-update-undo.timer'
if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode != 0:
    raise RuntimeError('SSH undo timer already fired or is inactive; check the machine by hand')
subprocess.run(['systemctl', 'stop', unit], check=True)
service_unit = 'nanohpc-ssh-update-undo.service'
loaded = subprocess.run(['systemctl', 'show', '-p', 'LoadState', '--value', service_unit],
                        capture_output=True, text=True, check=True).stdout.strip()
if loaded == 'loaded':
    subprocess.run(['systemctl', 'stop', service_unit], check=True)
elif loaded != 'not-found':
    raise RuntimeError('cannot verify SSH undo service state; keep the backup')
if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
    raise RuntimeError('SSH undo timer is still active')
service = subprocess.run(['systemctl', 'show', '-p', 'ActiveState', '--value', service_unit],
                         capture_output=True, text=True, check=True)
if service.stdout.strip() != 'inactive' or (backup / 'needs-review').exists():
    raise RuntimeError('SSH undo started while canceling; keep the backup and check the machine by hand')
shutil.rmtree(backup)
"""

LISTENER_PROGRAM = r"""
# nanohpc-ssh-listener-check
import subprocess
service = subprocess.run(['systemctl', 'is-active', '--quiet', 'ssh.service']).returncode == 0
socket = subprocess.run(['systemctl', 'is-active', '--quiet', 'ssh.socket']).returncode == 0
port = subprocess.run(['ss', '-H', '-ltn', 'sport = :22'], capture_output=True, text=True)
if not (service or socket) or port.returncode or not port.stdout.strip():
    raise RuntimeError('SSH is not running and listening on port 22')
"""

CHECK_PROGRAM = r"""
# nanohpc-ssh-repair-check
from pathlib import Path
if Path('/root/nanohpc-ssh-update-undo').exists():
    raise RuntimeError('SSH undo backup is present; verify fresh root/admin logins and OpenSSH package state before another update')
"""


def hold_root(machine: str, ssh_config: Path | None) -> subprocess.Popen[str]:
    """Open one independent root connection and require its READY response."""
    command = "printf 'READY\\n'; sleep 14400 # nanohpc-root-hold"
    arguments = ssh_args(ssh_config, machine, command, False)
    arguments = [
        *arguments[:-2],
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-l",
        "root",
        *arguments[-2:],
    ]
    process = subprocess.Popen(
        arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
        if ready and process.stdout.readline().strip() == "READY" and process.poll() is None:
            return process
        if process.poll() is not None:
            break
    process.terminate()
    _, error = process.communicate(timeout=5)
    detail = error.strip()
    raise restart.RestartError(f"{machine}: held root SSH connection failed: {detail or 'no READY response'}")


def require_holds(machine: str, sessions: list[subprocess.Popen[str]]) -> None:
    """Stop if either independent recovery connection has closed."""
    if len(sessions) != 2 or any(session.poll() is not None for session in sessions):
        raise restart.RestartError(f"{machine}: a held root SSH connection closed during the update")


def prepare(machine: str, ssh_config: Path | None) -> None:
    """Back up SSH files and arm the 15-minute undo on the target machine."""
    source = PREPARE_PROGRAM.replace("UNDO_SOURCE", repr(UNDO_PROGRAM))
    restart.root(machine, ssh_config, ["/usr/bin/python3", "-c", source], 120)


def require_clear(machine: str, ssh_config: Path | None) -> None:
    """Refuse another update while an earlier SSH undo still needs review."""
    restart.root(machine, ssh_config, ["/usr/bin/python3", "-c", CHECK_PROGRAM], 30)


def listener(machine: str, ssh_config: Path | None) -> None:
    """Require either ssh.service or ssh.socket and a live port 22 listener."""
    restart.root(machine, ssh_config, ["/usr/bin/python3", "-c", LISTENER_PROGRAM], 30)


def cancel(machine: str, ssh_config: Path | None) -> None:
    """Cancel the undo only after both fresh logins passed."""
    restart.root(machine, ssh_config, ["/usr/bin/python3", "-c", CANCEL_PROGRAM], 30)


def close_holds(sessions: list[subprocess.Popen[str]]) -> None:
    """Close held root connections after the SSH update and login checks succeed."""
    for session in sessions:
        session.terminate()
        session.wait(timeout=5)
        if session.stdout is not None:
            session.stdout.close()
        if session.stderr is not None:
            session.stderr.close()
