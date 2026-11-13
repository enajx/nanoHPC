"""The front node's copies to its backup front node: nanohpc-front-copy sends the Slurm state and accounting (every 5
minutes) or the monitoring data, built website and secrets (every night); nanohpc-front-copy-receive, root's forced
command for that key on the other machine, accepts only rsync into its copy folder. Run through the real scripts
with a fake rsync and mysqldump that record their arguments (mocked: no real rsync or database)."""

import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEND = ROOT / "src/nanohpc/files/nanohpc-front-copy"
RECEIVE = ROOT / "src/nanohpc/files/nanohpc-front-copy-receive"
STATE_LINE = (
    "rsync --server -rtpe.iLsfxCIvu --timeout=120 --delete-during . /var/lib/nanohpc-front-copy/state/slurmctld/"
)


def fakes(folder: Path, rsync_codes: list[int]) -> dict[str, str]:
    """A fake rsync (exit codes taken in turn, then 0) and mysqldump first on PATH; each appends its arguments to
    folder/calls."""
    bin_folder = folder / "bin"
    bin_folder.mkdir()
    codes = folder / "codes"
    codes.write_text("".join(f"{code}\n" for code in rsync_codes))
    rsync = bin_folder / "rsync"
    rsync.write_text(
        f"#!/bin/sh\necho \"rsync $*\" >> '{folder / 'calls'}'\n"
        f"code=$(head -n 1 '{codes}'); sed -i.old 1d '{codes}'; exit ${{code:-0}}\n"
    )
    dump = bin_folder / "mysqldump"
    dump.write_text(f"#!/bin/sh\necho \"mysqldump $*\" >> '{folder / 'calls'}'\necho 'CREATE TABLE job;'\n")
    for program in (rsync, dump):
        program.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}"}


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.staging = self.folder / "staging"

    def send(self, codes: list[int], *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable, str(SEND), "--to", "login2=192.168.104.30", "--key", "/etc/nanohpc/front-copy/id_ed25519",
            "--known-hosts", "/etc/nanohpc/front-copy/known_hosts", "--staging", str(self.staging), *arguments,
        ]  # fmt: skip
        return subprocess.run(command, env=fakes(self.folder, codes), capture_output=True, text=True, check=False)

    def calls(self) -> list[str]:
        return (self.folder / "calls").read_text().splitlines()

    def test_state_sends_the_slurm_state_then_the_database_dump_then_the_time(self) -> None:
        result = self.send([], "--kind", "state", "--item", "/var/lib/slurm/slurmctld=slurmctld", "--database",
                           "slurm_acct_db")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(calls[0].startswith("mysqldump --single-transaction"), calls)
        self.assertIn("slurm_acct_db", calls[0])
        self.assertTrue(
            calls[1].endswith(
                "/var/lib/slurm/slurmctld/ root@192.168.104.30:/var/lib/nanohpc-front-copy/state/slurmctld/"
            ),
            calls,
        )
        self.assertIn("--delete", calls[1])
        self.assertIn("-o StrictHostKeyChecking=yes", calls[1])
        self.assertTrue(
            calls[2].endswith(
                f"{self.staging}/slurm_acct_db.sql root@192.168.104.30:/var/lib/nanohpc-front-copy/state/slurm_acct_db.sql"
            ),
            calls,
        )
        # The time of this copy goes last, so it is there only when everything before it arrived.
        self.assertTrue(
            calls[3].endswith(
                f"{self.staging}/state.done root@192.168.104.30:/var/lib/nanohpc-front-copy/state/state.done"
            ),
            calls,
        )
        self.assertEqual((self.staging / "slurm_acct_db.sql").read_text(), "CREATE TABLE job;\n")

    def test_a_failed_copy_fails_and_sends_no_time(self) -> None:
        result = self.send([12], "--kind", "nightly", "--item", "/var/lib/nanohpc/prometheus=prometheus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the nightly copy to login2 failed", result.stderr)
        self.assertNotIn("nightly.done", "\n".join(self.calls()))

    def test_files_that_vanish_during_the_copy_are_copied_again(self) -> None:
        """Prometheus removes compacted blocks while it runs: rsync's exit code 24 repeats that item's copy."""
        result = self.send([24, 24], "--kind", "nightly", "--item", "/var/lib/nanohpc/prometheus=prometheus",
                           "--exclude", "/wal/")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(sum("nanohpc/prometheus/" in call for call in calls), 3)
        self.assertIn("--exclude=/wal/", calls[0])
        self.assertTrue(calls[-1].endswith("/var/lib/nanohpc-front-copy/nightly/nightly.done"))

    def test_a_folder_with_links_is_sent_as_one_archive(self) -> None:
        source = self.folder / "letsencrypt"
        (source / "live").mkdir(parents=True)
        (source / "live/cert.pem").symlink_to("../archive/cert1.pem")
        result = self.send([], "--kind", "nightly", "--archive", f"{source}=letsencrypt")
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.staging / "letsencrypt.tar") as archive:
            self.assertIn("letsencrypt/live/cert.pem", archive.getnames())
        self.assertTrue(self.calls()[0].endswith(":/var/lib/nanohpc-front-copy/nightly/letsencrypt.tar"))

    def test_dry_run_changes_nothing_there(self) -> None:
        result = self.send([], "--kind", "nightly", "--item", "/srv/nanohpc-web=website", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all("--dry-run" in call for call in self.calls()))
        self.assertNotIn("nightly.done", "\n".join(self.calls()))


class ReceiveTest(unittest.TestCase):
    def receive(self, command: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**fakes(Path(directory), []), "SSH_ORIGINAL_COMMAND": command}
            return subprocess.run([sys.executable, str(RECEIVE)], env=environment, capture_output=True, text=True,
                                  check=False)  # fmt: skip

    def test_copies_into_the_copy_folder_are_received(self) -> None:
        for command in (
            STATE_LINE,
            STATE_LINE.replace("state/slurmctld/", "state/state.done"),
            STATE_LINE.replace("state/slurmctld/", "nightly/prometheus/"),
            STATE_LINE.replace("-rtpe", "-nrtpe"),
        ):
            with self.subTest(command):
                result = self.receive(command)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_anything_else_is_refused(self) -> None:
        for command in (
            "",
            "id",
            STATE_LINE.replace("state/slurmctld/", "state/../../../../etc/"),
            STATE_LINE.replace("/var/lib/nanohpc-front-copy/state/slurmctld/", "/var/lib/slurm/slurmctld/"),
            STATE_LINE.replace("state/slurmctld/", "other/x"),
            "rsync --server --sender -rtpe.iLsfxCIvu . /var/lib/nanohpc-front-copy/state/slurmctld/",
            STATE_LINE.replace("--timeout=120", "--temp-dir=/tmp"),
            STATE_LINE + "; id",
            STATE_LINE.replace("-rtpe", "-lrtpe"),
            STATE_LINE.replace("-rtpe", "-Drtpe"),
        ):
            with self.subTest(command):
                result = self.receive(command)
                self.assertEqual(result.returncode, 1)
                self.assertIn("refused", result.stderr)


if __name__ == "__main__":
    unittest.main()
