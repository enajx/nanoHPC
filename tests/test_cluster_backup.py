"""Verify the nightly /home backup end to end: a real rsync mirrors one folder into another through a fake `ssh`.

The fake `ssh` (labelled as such) does what sshd does with the backup account's forced command: it records its
options, sets SSH_ORIGINAL_COMMAND to the remote command, and runs nanohpc-backup-receive, which runs the real rsync
server. So these tests cover the real rsync options, `--fake-super`, and the forced command's allowlist, on the local
machine. They need rsync 3.x for both ends: the macOS `rsync` (openrsync) has no ACL, xattr, or `--fake-super`
support. Set NANOHPC_TEST_RSYNC to an rsync 3.x program, or have one on PATH, /opt/homebrew/bin, or /usr/local/bin.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

FILES = Path(__file__).resolve().parents[1] / "src/nanohpc/files"
BACKUP = FILES / "cluster-backup"
RECEIVE = FILES / "nanohpc-backup-receive"
METRIC_NAMES = [
    "cluster_backup_last_exit_code",
    "cluster_backup_last_success_timestamp_seconds",
    "cluster_backup_last_run_timestamp_seconds",
    "cluster_backup_duration_seconds",
]


def find_rsync3() -> str | None:
    """Return an rsync 3.x program, or None when this machine has none."""
    candidates = [
        os.environ.get("NANOHPC_TEST_RSYNC", ""),
        shutil.which("rsync") or "",
        "/opt/homebrew/bin/rsync",
        "/usr/local/bin/rsync",
    ]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            version = subprocess.run([candidate, "--version"], capture_output=True, text=True, check=False).stdout
            if re.match(r"rsync\s+version 3\.", version):
                return candidate
    return None


RSYNC = find_rsync3()


def read_metrics(path: Path) -> dict[str, str]:
    """Return the metric values of a node_exporter textfile, checking each has HELP and TYPE gauge lines."""
    text = path.read_text()
    values = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            name, value = line.split(" ")
            values[name] = value
            assert f"# HELP {name} " in text, name
            assert f"# TYPE {name} gauge\n" in text, name
    return values


class Cluster:
    """A temporary home machine and backup machine on this computer, joined by the fake ssh."""

    def __init__(self, root: Path, rsync: str) -> None:
        self.root = root
        self.home = root / "home"
        self.folder = root / "backup" / "home"
        self.metrics = root / "metrics" / "cluster-backup.prom"
        self.identity = root / "id_backup"
        self.known_hosts = root / "known_hosts"
        self.ssh_log = root / "ssh-arguments"
        self.local_bin = root / "local-bin"
        remote_bin = root / "remote-bin"
        for folder in [self.home, self.folder, self.metrics.parent, self.local_bin, remote_bin]:
            folder.mkdir(parents=True)
        self.identity.write_text("not a real key\n")
        self.known_hosts.write_text("backup ssh-ed25519 AAAA\n")
        (self.local_bin / "rsync").symlink_to(rsync)
        (remote_bin / "rsync").symlink_to(rsync)
        # Fake ssh: host "unreachable" fails as ssh does; any other host runs the backup account's forced command.
        ssh = self.local_bin / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > '{self.ssh_log}'\n"
            'while [ $# -gt 0 ]; do case "$1" in -i|-o|-l) shift 2;; -*) shift;; *) break;; esac; done\n'
            'host="$1"; shift\n'
            'if [ "$host" = unreachable ]; then\n'
            '  echo "ssh: connect to host unreachable port 22: Connection refused" >&2; exit 255\n'
            "fi\n"
            'SSH_ORIGINAL_COMMAND="$*" PATH="' + str(remote_bin) + ':$PATH" '
            f"exec '{sys.executable}' '{RECEIVE}' '{self.folder}'\n"
        )
        ssh.chmod(0o755)

    def run_backup(self, destination: str, fake_super: bool) -> subprocess.CompletedProcess[str]:
        """Run cluster-backup as the systemd unit would, with the fake ssh and rsync 3.x first on PATH."""
        args = [
            sys.executable,
            str(BACKUP),
            "--source",
            str(self.home) + "/",
            "--destination",
            destination,
            "--identity",
            str(self.identity),
            "--known-hosts",
            str(self.known_hosts),
            "--exclude",
            ".cache/",
            "--exclude",
            ".venv/",
            "--exclude",
            "__pycache__/",
            "--metrics",
            str(self.metrics),
        ]
        if fake_super:
            args.append("--fake-super")
        environment = dict(os.environ)
        environment["PATH"] = str(self.local_bin) + os.pathsep + environment["PATH"]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=environment)

    def destination(self) -> str:
        """The rsync target in the backup machine's folder."""
        return f"nanohpc-backup@backup:{self.folder}/"


@unittest.skipIf(
    RSYNC is None, "needs rsync 3.x (macOS openrsync has no ACLs, xattrs, or --fake-super); set NANOHPC_TEST_RSYNC"
)
class ClusterBackupTests(unittest.TestCase):
    """Mirror, metrics, failures, and restore, through the real scripts and a real rsync."""

    def test_mirror_excludes_deletes_and_restores(self) -> None:
        """/home is mirrored with owners kept as xattrs, excludes and deleted files leave the copy, restore works."""
        assert RSYNC is not None
        with tempfile.TemporaryDirectory(prefix="cluster-backup-") as directory:
            cluster = Cluster(Path(directory), RSYNC)
            alice = cluster.home / "alice"
            for folder in [alice / ".cache", alice / ".venv/bin", alice / "project/__pycache__", cluster.home / "bob"]:
                folder.mkdir(parents=True)
            (alice / "notes.txt").write_text("notes\n")
            (alice / ".cache/blob").write_text("cache\n")
            (alice / ".venv/bin/python").write_text("venv\n")
            (alice / "project/run.py").write_text("print(1)\n")
            (alice / "project/__pycache__/run.pyc").write_text("pyc\n")
            (alice / "linked").hardlink_to(alice / "notes.txt")
            (alice / "read-only").write_text("keep this mode\n")
            (alice / "read-only").chmod(0o400)
            (cluster.home / "bob/data").write_text("bob\n")
            # Left from earlier runs: a file deleted from /home since, and a folder excluded since.
            (cluster.folder / "carol").mkdir()
            (cluster.folder / "carol/old").write_text("old\n")
            (cluster.folder / "alice/.cache").mkdir(parents=True)
            (cluster.folder / "alice/.cache/old").write_text("old\n")

            before = time.time()
            completed = cluster.run_backup(cluster.destination(), True)
            self.assertEqual(completed.returncode, 0, completed.stderr)

            copy = cluster.folder / "alice"
            self.assertEqual((copy / "notes.txt").read_text(), "notes\n")
            self.assertEqual((copy / "project/run.py").read_text(), "print(1)\n")
            self.assertEqual((cluster.folder / "bob/data").read_text(), "bob\n")
            self.assertEqual((copy / "linked").stat().st_ino, (copy / "notes.txt").stat().st_ino)
            for excluded in [".cache", ".venv", "project/__pycache__"]:
                self.assertFalse((copy / excluded).exists(), excluded)
            self.assertFalse((cluster.folder / "carol").exists())
            # --fake-super: the unprivileged copy is kept writable by its owner; the real mode is in an xattr.
            self.assertEqual(stat.S_IMODE((copy / "read-only").stat().st_mode), 0o600)

            ssh_arguments = cluster.ssh_log.read_text().splitlines()
            self.assertEqual(ssh_arguments[ssh_arguments.index("-i") + 1], str(cluster.identity))
            for option in [
                "BatchMode=yes",
                "IdentitiesOnly=yes",
                "StrictHostKeyChecking=yes",
                f"UserKnownHostsFile={cluster.known_hosts}",
            ]:
                self.assertEqual(ssh_arguments[ssh_arguments.index(option) - 1], "-o")

            values = read_metrics(cluster.metrics)
            self.assertEqual(sorted(values), sorted(METRIC_NAMES))
            self.assertEqual(values["cluster_backup_last_exit_code"], "0")
            run = float(values["cluster_backup_last_run_timestamp_seconds"])
            self.assertTrue(before - 1 <= run <= time.time())
            # The success time is when the copy finished, so a long copy does not look old the next morning.
            success = float(values["cluster_backup_last_success_timestamp_seconds"])
            duration = float(values["cluster_backup_duration_seconds"])
            self.assertAlmostEqual(success, run + duration, delta=1)
            self.assertLessEqual(success, time.time())
            self.assertGreaterEqual(float(values["cluster_backup_duration_seconds"]), 0)
            self.assertEqual(stat.S_IMODE(cluster.metrics.stat().st_mode), 0o644)
            self.assertEqual(list(cluster.metrics.parent.iterdir()), [cluster.metrics])

            # A file deleted from /home is deleted from the copy on the next run.
            (alice / "project/run.py").unlink()
            completed = cluster.run_backup(cluster.destination(), True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((copy / "project/run.py").exists())
            self.assertTrue((copy / "notes.txt").exists())

            # Restore one user's folder the documented way: rsync from the backup account with the same key.
            restored = cluster.root / "restored-alice"
            environment = dict(os.environ)
            environment["PATH"] = str(cluster.local_bin) + os.pathsep + environment["PATH"]
            restore = subprocess.run(
                [
                    "rsync",
                    "--archive",
                    "--hard-links",
                    "--acls",
                    "--xattrs",
                    "--numeric-ids",
                    "--rsh",
                    (
                        f"ssh -i {cluster.identity} -o BatchMode=yes -o IdentitiesOnly=yes"
                        f" -o StrictHostKeyChecking=yes -o UserKnownHostsFile={cluster.known_hosts}"
                    ),
                    "--rsync-path",
                    "rsync --fake-super",
                    f"nanohpc-backup@backup:{cluster.folder}/alice/",
                    str(restored) + "/",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual((restored / "notes.txt").read_text(), "notes\n")
            self.assertEqual(stat.S_IMODE((restored / "read-only").stat().st_mode), 0o400)
            self.assertEqual((restored / "linked").stat().st_ino, (restored / "notes.txt").stat().st_ino)

    def test_failure_keeps_last_success_and_shows_the_error(self) -> None:
        """A failed run records its exit code, keeps the previous success time, and prints rsync's error."""
        assert RSYNC is not None
        with tempfile.TemporaryDirectory(prefix="cluster-backup-") as directory:
            cluster = Cluster(Path(directory), RSYNC)
            (cluster.home / "alice").mkdir()

            # A first run that fails writes no success time.
            failed = cluster.run_backup(f"nanohpc-backup@unreachable:{cluster.folder}/", True)
            self.assertNotIn(failed.returncode, [0, 24])
            self.assertIn("Connection refused", failed.stderr)
            values = read_metrics(cluster.metrics)
            self.assertEqual(values["cluster_backup_last_exit_code"], str(failed.returncode))
            self.assertNotIn("cluster_backup_last_success_timestamp_seconds", values)

            completed = cluster.run_backup(cluster.destination(), True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            success = read_metrics(cluster.metrics)["cluster_backup_last_success_timestamp_seconds"]

            failed = cluster.run_backup(f"nanohpc-backup@unreachable:{cluster.folder}/", True)
            self.assertNotIn(failed.returncode, [0, 24])
            values = read_metrics(cluster.metrics)
            self.assertEqual(values["cluster_backup_last_exit_code"], str(failed.returncode))
            self.assertEqual(values["cluster_backup_last_success_timestamp_seconds"], success)
            self.assertGreater(
                float(values["cluster_backup_last_run_timestamp_seconds"]),
                float(success),
            )

            # The forced command refuses a folder other than its own; the run fails with the refusal shown.
            refused = cluster.run_backup(f"nanohpc-backup@backup:{cluster.folder}/alice/", True)
            self.assertNotIn(refused.returncode, [0, 24])
            self.assertIn("nanohpc-backup-receive: refused", refused.stderr)
            self.assertEqual(read_metrics(cluster.metrics)["cluster_backup_last_success_timestamp_seconds"], success)

            # Without --fake-super the backup account's forced command refuses the run.
            refused = cluster.run_backup(cluster.destination(), False)
            self.assertNotIn(refused.returncode, [0, 24])
            self.assertIn("nanohpc-backup-receive: refused", refused.stderr)

    def test_vanished_files_count_as_success(self) -> None:
        """rsync's exit code 24 (files vanished during the copy, normal on a live /home) is a success."""
        assert RSYNC is not None
        with tempfile.TemporaryDirectory(prefix="cluster-backup-") as directory:
            cluster = Cluster(Path(directory), RSYNC)
            (cluster.home / "alice").mkdir()
            # Fake local rsync: runs the real copy, then exits 24 as rsync does when a file vanished.
            (cluster.local_bin / "rsync").unlink()
            (cluster.local_bin / "rsync").write_text(f"#!/bin/sh\n'{RSYNC}' \"$@\" || exit $?\nexit 24\n")
            (cluster.local_bin / "rsync").chmod(0o755)
            completed = cluster.run_backup(cluster.destination(), True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((cluster.folder / "alice").is_dir())
            values = read_metrics(cluster.metrics)
            self.assertEqual(values["cluster_backup_last_exit_code"], "0")
            self.assertIn("cluster_backup_last_success_timestamp_seconds", values)


class ClusterBackupArgumentTests(unittest.TestCase):
    """Arguments that would copy into the wrong place are refused before rsync runs."""

    def test_source_and_destination_need_a_final_slash(self) -> None:
        """Without a final slash rsync would make /home/home inside the copy, so both must end with '/'."""
        with tempfile.TemporaryDirectory(prefix="cluster-backup-") as directory:
            root = Path(directory)
            for source, destination in [("/home", "u@h:/srv/backup/home/"), ("/home/", "u@h:/srv/backup/home")]:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(BACKUP),
                        "--source",
                        source,
                        "--destination",
                        destination,
                        "--identity",
                        str(root / "key"),
                        "--known-hosts",
                        str(root / "known_hosts"),
                        "--metrics",
                        str(root / "backup.prom"),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("must end with '/'", completed.stderr)
                self.assertFalse((root / "backup.prom").exists())


if __name__ == "__main__":
    unittest.main()
