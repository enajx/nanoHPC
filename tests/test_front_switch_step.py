"""The switch's steps on the front pair's machines (nanohpc-front-switch-step, run as root by `nanohpc front-switch`):
`stop` on the old front node (fence held, last copies, front services stopped) and `take` on the new one (fence
released, Slurm state, accounting and monitoring data restored from the copies). Run through the real script under a
test root folder, with fake systemctl, mariadb, mysqldump, chown and copy commands that record their arguments
(mocked: no real services or database)."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STEP = ROOT / "src/nanohpc/files/nanohpc-front-switch-step"


class StepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.calls = self.root / "calls"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake("systemctl", 'case "$1" in is-active) exit 3;; show) echo loaded;; esac')
        self.fake("slurmctld", "")
        self.fake(
            "mariadb", f'case "$*" in *SCHEMATA*) echo "${{DATABASE_COUNT:-0}}";; "") cat >> "{self.calls}";; esac'
        )
        self.fake("mysqldump", "echo 'old database'")
        self.fake("chown", "")
        self.fake("rsync", "")
        self.fake("id", "")
        self.fake("tar", "")
        self.copy_run = self.fake("copy-run", "")
        self.fence_run = self.fake("fence-run", "")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]

    def tearDown(self) -> None:
        self.listener.close()

    def fake(self, name: str, body: str) -> Path:
        program = self.bin / name
        program.write_text(f"#!/bin/sh\necho \"{name} $*\" >> '{self.calls}'\n{body}\n")
        program.chmod(0o755)
        return program

    def step(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "NANOHPC_FRONT_SWITCH_ROOT": str(self.root),
        }
        command = [sys.executable, str(STEP), *arguments, "--copy-run", str(self.copy_run), "--fence-run",
                   str(self.fence_run)]  # fmt: skip
        return subprocess.run(command, env=environment, capture_output=True, text=True, check=False)

    def recorded(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def copies(self) -> None:
        """The copies the front node sent: Slurm state and accounting, and the nightly folders and archives."""
        folder = self.root / "var/lib/nanohpc-front-copy"
        (folder / "state/slurmctld").mkdir(parents=True)
        (folder / "state/slurm_acct_db.sql").write_text("dump\n")
        (folder / "state/state.done").write_text("1700000000\n")
        (folder / "nightly/prometheus").mkdir(parents=True)
        (folder / "nightly/nightly.done").write_text("1700000000\n")

    def test_stop_holds_the_fence_copies_last_and_stops_the_front_services(self) -> None:
        result = self.step("stop", "--to", "login2")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertTrue(calls[0].startswith("fence-run --hold"), calls)
        self.assertIn("systemctl stop slurmctld.service", calls)
        # The Slurm state is copied after the controller stopped, while the database still runs.
        self.assertLess(calls.index("systemctl stop slurmctld.service"), calls.index("copy-run state"))
        self.assertLess(calls.index("copy-run state"), calls.index("systemctl stop slurmdbd.service mariadb.service"))
        self.assertLess(calls.index("copy-run state"), calls.index("copy-run nightly"))
        stopped = next(call for call in calls if call.startswith("systemctl disable --now nanohpc-prometheus"))
        self.assertIn("nginx.service", stopped)
        self.assertIn("nanohpc-agent.service", stopped)
        self.assertIn("systemctl disable slurmctld.service slurmdbd.service mariadb.service", calls)

    def test_stop_dry_run_only_tries_the_copies(self) -> None:
        result = self.step("stop", "--to", "login2", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        changes = [call for call in self.recorded() if not call.startswith("systemctl is-active")]
        self.assertEqual(changes, ["copy-run state --dry-run", "copy-run nightly --dry-run"])
        self.assertIn("Would stop", result.stdout)

    def test_stop_dry_run_fails_when_the_copies_cannot_reach_the_backup(self) -> None:
        self.fake("copy-run", "exit 1")
        result = self.step("stop", "--to", "login2", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot send its copies to login2", result.stderr)

    def test_take_restores_the_copies(self) -> None:
        self.copies()
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call for call in self.recorded() if not call.startswith("id ")]
        self.assertEqual(calls[0], "fence-run --release")
        self.assertIn("front", (self.root / "var/lib/nanohpc-front-fence/took-over").read_text())
        self.assertIn("systemctl start mariadb.service", calls)
        self.assertTrue(any(call.startswith("mariadb slurm_acct_db") for call in calls), calls)
        state = f"{self.root}/var/lib/nanohpc-front-copy/state/slurmctld/ {self.root}/var/lib/slurm/slurmctld/"
        self.assertIn(f"rsync -a --delete {state}", calls)
        self.assertIn(f"chown -R slurm:slurm {self.root}/var/lib/slurm/slurmctld", calls)
        prometheus = (
            f"{self.root}/var/lib/nanohpc-front-copy/nightly/prometheus/ {self.root}/var/lib/nanohpc/prometheus/"
        )
        # The monitoring history is added to what is there: nothing is deleted.
        self.assertIn(f"rsync -a {prometheus}", calls)
        self.assertIn(f"chown -R cluster-prometheus:cluster-prometheus {self.root}/var/lib/nanohpc/prometheus", calls)

    def test_take_keeps_an_existing_accounting_database_before_loading_the_copy(self) -> None:
        self.copies()
        os.environ["DATABASE_COUNT"] = "1"
        self.addCleanup(os.environ.pop, "DATABASE_COUNT")
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}")
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.root / "var/lib/nanohpc-front-copy/before-switch/slurm_acct_db.sql"
        self.assertEqual(before.read_text(), "old database\n")
        calls = self.recorded()
        self.assertTrue(any("DROP DATABASE slurm_acct_db" in call for call in calls), calls)

    def test_take_refuses_without_copies(self) -> None:
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no state copy", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_take_refuses_while_the_old_controller_answers(self) -> None:
        self.copies()
        self.listener.listen()
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the Slurm controller on front still answers", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_take_dry_run_changes_nothing(self) -> None:
        self.copies()
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would restore", result.stdout)
        self.assertTrue(all(call.startswith("id ") for call in self.recorded()), self.recorded())

    def test_take_dry_run_works_while_the_old_controller_still_runs(self) -> None:
        """The switch runs the take dry run before the old front node stops: its controller still answers then."""
        self.copies()
        self.listener.listen()
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still answers", result.stdout)

    def test_take_gives_the_accounting_daemon_its_database_login(self) -> None:
        """A dump of one database has no MariaDB accounts: take makes the slurm account with the restored password,
        after the secrets are unpacked."""
        self.copies()
        self.fake(
            "tar",
            f"mkdir -p '{self.root}/etc/nanohpc'; printf 'SLURM_DB_PASSWORD=%s\\n' {'a' * 64} > '{self.root}/etc/nanohpc/slurmdbd.env'",
        )
        (self.root / "var/lib/nanohpc-front-copy/nightly/slurmdbd.env.tar").write_text("archive")
        result = self.step("take", "--from", "front", "--peer", f"127.0.0.1:{self.port}")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        unpack = next(index for index, call in enumerate(calls) if call.startswith("tar "))
        login = next(index for index, call in enumerate(calls) if call.startswith("CREATE USER IF NOT EXISTS"))
        self.assertLess(unpack, login)
        self.assertIn("IDENTIFIED BY '" + "a" * 64 + "'", calls[login])

    def test_the_backup_keeps_the_same_front_services_stopped(self) -> None:
        """The standby role on the backup front node stops the same units the switch stops on the old front node."""
        defaults = yaml.safe_load((ROOT / "src/nanohpc/ansible/roles/front_backup/defaults/main.yml").read_text())
        result = subprocess.run([sys.executable, str(STEP), "units"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), defaults["front_backup_units"])


if __name__ == "__main__":
    unittest.main()
