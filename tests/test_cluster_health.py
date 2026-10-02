"""Run the real cluster-health script with fake commands and a fake root folder, and check its report and metrics.

These tests use fakes: systemctl, df, mountpoint, and openssl are small scripts put first on PATH, and the script reads
its nanoHPC files (roles, backup state) under NANOHPC_HEALTH_TEST_ROOT, an override used only by tests.
"""

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-health"

# The report of a home machine whose NFS server is down, whose /home is 95% full, and whose backup is old and failed.
BROKEN_HOME_REPORT = [
    "ok    nanoHPC roles are recorded",
    "ok    disk /: 12% space, 12% inodes used (warning from 90%)",
    "ok    service nanohpc-node-exporter (metrics)",
    "ok    metrics certificate valid for at least 30 more days",
    "ok    /home is mounted",
    "FAIL  service nfs-server",
    "ok    home quota reader timer",
    "ok    the home quota reader's last run worked",
    "WARN  disk /home: 95% space, 95% inodes used (warning from 90%)",
    "WARN  the last successful /home backup is less than 26 hours old",
    "WARN  the last /home backup run worked",
]

BROKEN_HOME_STATES = {
    "nanoHPC roles are recorded": "0",
    "disk /": "0",
    "service nanohpc-node-exporter": "0",
    "metrics certificate valid for at least 30 more days": "0",
    "/home is mounted": "0",
    "service nfs-server": "2",
    "home quota reader timer": "0",
    "the home quota reader's last run worked": "0",
    "disk /home": "1",
    "the last successful /home backup is less than 26 hours old": "1",
    "the last /home backup run worked": "1",
}


def write_command(folder: Path, name: str, body: str) -> None:
    """Write an executable fake command."""
    path = folder / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def make_machine(folder: Path, roles: str, failing_units: str) -> dict[str, str]:
    """Create fake commands and a fake root for one machine; return the environment to run cluster-health with."""
    bin_folder = folder / "bin"
    bin_folder.mkdir()
    # Fake systemctl: every unit is active and not failed, except the units named in failing_units.
    write_command(
        bin_folder,
        "systemctl",
        f'case " {failing_units} " in *" $3 "*) bad=1 ;; *) bad=0 ;; esac\n'
        'case "$1" in\n'
        '  is-active) exit "$bad" ;;\n'
        '  is-failed) test "$bad" = 1 ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    # Fake df --output=pcent|ipcent PATH: /home is 95% used, everything else 12%.
    write_command(
        bin_folder,
        "df",
        "echo 'Use%'\ncase \"$2\" in /home) echo ' 95%' ;; *) echo ' 12%' ;; esac\n",
    )
    write_command(bin_folder, "mountpoint", "exit 0\n")
    write_command(bin_folder, "openssl", "exit 0\n")
    root = folder / "root"
    (root / "etc/nanohpc").mkdir(parents=True)
    (root / "var/lib/nanohpc/metrics-textfile").mkdir(parents=True)
    (root / "etc/nanohpc/roles").write_text(roles + "\n")
    return {
        **os.environ,
        "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}",
        "NANOHPC_HEALTH_TEST_ROOT": str(root),
    }


def set_backup(root: Path, started_hours_ago: float, success_hours_ago: float | None, exit_code: int | None) -> None:
    """Configure the backup and write the backup's metrics file as the backup script would (None: not written)."""
    backup = root / "etc/nanohpc/backup"
    backup.mkdir(parents=True)
    (backup / "enabled").write_text("")
    started = backup / "started"
    started.write_text("")
    started_time = time.time() - started_hours_ago * 3600
    os.utime(started, (started_time, started_time))
    if success_hours_ago is None or exit_code is None:
        return
    success = int(time.time() - success_hours_ago * 3600)
    (root / "var/lib/nanohpc/metrics-textfile/backup.prom").write_text(
        f"cluster_backup_last_success_timestamp_seconds {success}\ncluster_backup_last_exit_code {exit_code}\n"
    )


def run(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run cluster-health with the given arguments."""
    return subprocess.run(
        [str(SCRIPT), *arguments], capture_output=True, text=True, env=environment, check=False, timeout=60
    )


def read_states(path: Path) -> tuple[dict[str, str], list[str]]:
    """Return the check states by id and the other sample lines of a metrics file."""
    states: dict[str, str] = {}
    others: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            continue
        if line.startswith('cluster_health_check_state{check="'):
            labels, value = line.rsplit(" ", 1)
            check = labels.removeprefix('cluster_health_check_state{check="').removesuffix('"}')
            if check in states:
                raise AssertionError(f"duplicate series for {check!r}")
            states[check] = value
        else:
            others.append(line)
    return states, others


class ClusterHealthMetricsTests(unittest.TestCase):
    """cluster-health --metrics writes each check's state; without it, the report is unchanged (fakes)."""

    def test_metrics_file_has_one_state_per_check(self) -> None:
        """Stable ids, 0/1/2 states, a finish time, mode 0644, no temporary file left, exit code 1 on a FAIL."""
        with tempfile.TemporaryDirectory(prefix="cluster-health-") as directory:
            folder = Path(directory)
            environment = make_machine(folder, "home", "nfs-server")
            set_backup(folder / "root", started_hours_ago=27, success_hours_ago=30, exit_code=1)
            output = folder / "out"
            output.mkdir()
            metrics = output / "cluster-health.prom"
            before = time.time()
            result = run(["--metrics", str(metrics)], environment)
            after = time.time()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout.splitlines(), BROKEN_HOME_REPORT)
            states, others = read_states(metrics)
            self.assertEqual(states, BROKEN_HOME_STATES)
            self.assertEqual(len(others), 1, others)
            name, value = others[0].split(" ")
            self.assertEqual(name, "cluster_health_timestamp_seconds")
            self.assertLessEqual(int(before), int(value))
            self.assertLessEqual(int(value), after)
            self.assertEqual(stat.S_IMODE(metrics.stat().st_mode), 0o644)
            self.assertEqual([path.name for path in output.iterdir()], ["cluster-health.prom"])

    def test_without_metrics_the_report_is_unchanged(self) -> None:
        """Without --metrics: the same lines and exit code as with it, and no metrics file."""
        with tempfile.TemporaryDirectory(prefix="cluster-health-") as directory:
            folder = Path(directory)
            environment = make_machine(folder, "home", "nfs-server")
            set_backup(folder / "root", started_hours_ago=27, success_hours_ago=30, exit_code=1)
            result = run([], environment)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout.splitlines(), BROKEN_HOME_REPORT)
            self.assertEqual(result.stderr, "")
            written = [path.name for path in (folder / "root/var/lib/nanohpc/metrics-textfile").iterdir()]
            self.assertEqual(written, ["backup.prom"])

    def test_backup_checks(self) -> None:
        """A recent good backup is ok; before the first night there is no age check; no backup configured: no check."""
        cases = [
            # (started hours ago, last success hours ago, last exit code, expected backup lines)
            (
                27,
                2,
                0,
                [
                    "ok    the last successful /home backup is less than 26 hours old",
                    "ok    the last /home backup run worked",
                ],
            ),
            (3, None, None, ["ok    the last /home backup run worked"]),
            (3, 2, 23, ["WARN  the last /home backup run worked"]),
            (None, None, None, []),
        ]
        for started, success, exit_code, expected in cases:
            with (
                self.subTest(started=started, success=success, exit_code=exit_code),
                tempfile.TemporaryDirectory(prefix="cluster-health-") as directory,
            ):
                folder = Path(directory)
                environment = make_machine(folder, "home", "")
                if started is not None:
                    set_backup(folder / "root", started, success, exit_code)
                result = run([], environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                lines = [line for line in result.stdout.splitlines() if "backup" in line]
                self.assertEqual(lines, expected)

    def test_same_id_keeps_worst_state_and_values_are_escaped(self) -> None:
        """Checks that give the same id make one series with the worst state; quotes, backslashes, newlines escaped."""
        with tempfile.TemporaryDirectory(prefix="cluster-health-") as directory:
            metrics = Path(directory) / "health.prom"
            # The script stops after defining its functions when sourced, so the test can call them directly.
            script = (
                f"source '{SCRIPT}'\n"
                'report ok "the website answers over HTTPS (/)"\n'
                'report FAIL "the website answers over HTTPS (/site.json)"\n'
                'report WARN "the website answers over HTTPS (/grafana)"\n'
                'report WARN "disk /: 95% space"\n'
                'report ok "disk /: 12% space"\n'
                "report ok 'say \"hi\" \\ there (x)'\n"
                "report ok $'two\\nlines'\n"
                "report ok '  spaced out  : 3'\n"
                f"write_metrics '{metrics}'\n"
            )
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            states, _ = read_states(metrics)
            self.assertEqual(
                states,
                {
                    "the website answers over HTTPS": "2",
                    "disk /": "1",
                    'say \\"hi\\" \\\\ there': "0",
                    "two\\nlines": "0",
                    "spaced out": "0",
                },
            )

    def test_unknown_arguments_are_refused(self) -> None:
        """A missing metrics path or an unknown argument stops with exit code 2 before any check runs."""
        for arguments in (["--metrics"], ["--other"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(SCRIPT), *arguments], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage: cluster-health", result.stderr)


if __name__ == "__main__":
    unittest.main()
