"""Verify the backup account's SSH forced command allows only rsync into or out of its one folder.

The allowed command lines below were captured from rsync 3.2.7 and 3.4.1 clients (they send the same lines) through
an `-e` program that printed its arguments. The rsync run by the forced command here is a fake that records its
arguments, so these tests check what is allowed and what is executed, not a real transfer (test_cluster_backup.py
runs a real one).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RECEIVE = Path(__file__).resolve().parents[1] / "src/nanohpc/files/nanohpc-backup-receive"
FOLDER = "/srv/backup/home"

# Captured from rsync clients run with --rsync-path="rsync --fake-super".
CAPTURED_BACKUP = (
    "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --timeout=600 --delete-excluded --partial --numeric-ids"
    " . /srv/backup/home/"
)
CAPTURED_BACKUP_DELETE = (
    "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --timeout=600 --delete --partial --numeric-ids"
    " . /srv/backup/home/"
)
CAPTURED_RESTORE_USER = (
    "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --timeout=600 --numeric-ids . /srv/backup/home/alice/"
)
CAPTURED_RESTORE_DRY_RUN = (
    "rsync --fake-super --server --sender -vnlHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/alice/"
)
CAPTURED_RESTORE_FILE = (
    "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/alice/f"
)
CAPTURED_RESTORE_NO_SLASH = (
    "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/alice"
)
CAPTURED_LIST_ALL = "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/"
# Captured lines that must be refused.
CAPTURED_DEPLOY_CHECK = "rsync --fake-super --server --sender -de.LsfxCIvu --list-only --timeout=30 . /srv/backup/home/"
CAPTURED_WITHOUT_FAKE_SUPER = "rsync --server -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/"
CAPTURED_SECLUDED_ARGS = "rsync --fake-super --server -slHogDtpAXre.iLsfxCIvu"
CAPTURED_PARTIAL_DIR = (
    "rsync --fake-super --server -lHogDtpAXrze.iLsfxCIvu --bwlimit=1000 --delete-after --partial-dir .p"
    " . /srv/backup/home/"
)
CAPTURED_ESCAPED_SPACE = (
    "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/my\\ dir/"
)
CAPTURED_ESCAPED_QUOTE = (
    "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/al\\'ice/"
)


def run_receive(root: Path, command: str | None, folder: str) -> subprocess.CompletedProcess[str]:
    """Run the forced command as sshd would, with a fake rsync first on PATH that records its arguments."""
    bin_folder = root / "bin"
    bin_folder.mkdir(exist_ok=True)
    fake_rsync = bin_folder / "rsync"
    fake_rsync.write_text(f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{root / 'argv'}'\n")
    fake_rsync.chmod(0o755)
    environment = {key: value for key, value in os.environ.items() if key != "SSH_ORIGINAL_COMMAND"}
    environment["PATH"] = str(bin_folder) + os.pathsep + environment["PATH"]
    if command is not None:
        environment["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(
        [sys.executable, str(RECEIVE), folder], capture_output=True, text=True, check=False, env=environment
    )


class BackupReceiveTests(unittest.TestCase):
    """Allowed and refused command lines, run through the real script."""

    def test_captured_command_lines_are_run_without_a_shell(self) -> None:
        """Each captured backup or restore line runs rsync with exactly its words, and nothing else."""
        for command in [
            CAPTURED_BACKUP,
            CAPTURED_BACKUP_DELETE,
            CAPTURED_RESTORE_USER,
            CAPTURED_RESTORE_DRY_RUN,
            CAPTURED_RESTORE_FILE,
            CAPTURED_RESTORE_NO_SLASH,
            CAPTURED_LIST_ALL,
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/alice/.ssh/",
            # The deploy's read-only check (rsync --list-only), as rsync 3.2.7 sends it.
            CAPTURED_DEPLOY_CHECK,
        ]:
            with self.subTest(command=command), tempfile.TemporaryDirectory(prefix="backup-receive-") as directory:
                root = Path(directory)
                completed = run_receive(root, command, FOLDER)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual((root / "argv").read_text().splitlines(), command.split(" ")[1:])

    def test_other_commands_are_refused(self) -> None:
        """Anything other than an rsync server into or out of the folder is refused with a message and exit 1."""
        refused = [
            None,
            "",
            "ls -la /srv/backup/home/",
            "sh -c id",
            "/usr/bin/rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/",
            CAPTURED_WITHOUT_FAKE_SUPER,
            CAPTURED_SECLUDED_ARGS,
            CAPTURED_SECLUDED_ARGS + " . /srv/backup/home/",
            CAPTURED_PARTIAL_DIR,
            CAPTURED_ESCAPED_SPACE,
            CAPTURED_ESCAPED_QUOTE,
            # Other folders, and ways out of the folder.
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home2/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/alice/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --numeric-ids . /srv/backup/home/../../etc/",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/../../etc/shadow",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/alice/../..",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/./alice/",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . /srv/backup/home//alice/",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . /etc/passwd",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . alice/",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/a /srv/backup/home/b",
            # Shell characters.
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/; id",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/ && id",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/$(id)",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/`id`",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/ | id",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/ > /tmp/x",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/\nid",
            "rsync  --fake-super --server -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/",
            # Options that name other paths, follow links out of the folder, or delete the backup.
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --log-file=/tmp/x . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --temp-dir=/tmp . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --link-dest=/etc . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --files-from=/etc/passwd . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --write-batch=/tmp/b . /srv/backup/home/",
            "rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --remove-source-files . /srv/backup/home/",
            "rsync --fake-super --server --sender -LlHogDtpAXre.iLsfxCIvu . /srv/backup/home/alice/",
            "rsync --fake-super --server -KlHogDtpAXre.iLsfxCIvu . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --protect-args . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --secluded-args . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --daemon . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu -e.x . /srv/backup/home/",
            # Word order.
            "rsync --server --fake-super -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu --sender . /srv/backup/home/",
            "rsync --fake-super --server -lHogDtpAXre.iLsfxCIvu /srv/backup/home/",
            "rsync --fake-super -lHogDtpAXre.iLsfxCIvu . /srv/backup/home/",
            # Listing is only for reading (sending).
            "rsync --fake-super --server -de.LsfxCIvu --list-only . /srv/backup/home/",
        ]
        for command in refused:
            with self.subTest(command=command), tempfile.TemporaryDirectory(prefix="backup-receive-") as directory:
                root = Path(directory)
                completed = run_receive(root, command, FOLDER)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn("nanohpc-backup-receive: refused", completed.stderr)
                self.assertFalse((root / "argv").exists())

    def test_restores_cannot_follow_links_out_of_the_folder(self) -> None:
        """A user can back up a symbolic link (for example alice/escape -> /etc); a restore through it is refused,
        so the key reads nothing outside the backup folder."""
        with tempfile.TemporaryDirectory(prefix="backup-receive-") as directory:
            root = Path(directory)
            folder = root / "backup"
            (folder / "alice").mkdir(parents=True)
            (folder / "alice" / "escape").symlink_to("/etc")
            (folder / "alice" / "notes").mkdir()
            inside = (
                f"rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . {folder}/alice/notes/"
            )
            self.assertEqual(run_receive(root, inside, str(folder)).returncode, 0)
            (root / "argv").unlink()
            for target in ("alice/escape/hosts", "alice/escape/"):
                command = (
                    f"rsync --fake-super --server --sender -lHogDtpAXre.iLsfxCIvu --numeric-ids . {folder}/{target}"
                )
                completed = run_receive(root, command, str(folder))
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn("refused", completed.stderr)
                self.assertFalse((root / "argv").exists())

    def test_invalid_folder_argument_is_refused(self) -> None:
        """The fixed folder must be an absolute, plain path without a final slash, so the checks above hold."""
        for folder in ["srv/backup/home", "/srv/backup/home/", "/srv/backup/../home", "/", "/srv/back up"]:
            with self.subTest(folder=folder), tempfile.TemporaryDirectory(prefix="backup-receive-") as directory:
                root = Path(directory)
                completed = run_receive(root, CAPTURED_BACKUP, folder)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn("nanohpc-backup-receive:", completed.stderr)
                self.assertFalse((root / "argv").exists())


if __name__ == "__main__":
    unittest.main()
