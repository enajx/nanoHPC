"""The read-only update report through the administrator's CLI."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_probe import write_command  # pyrefly: ignore[missing-import]

ROOT = Path(__file__).resolve().parents[1]
MACHINES = ("front", "gpu4", "gpu2", "cpu1", "gpu4i", "store")


class UpdateReportCommandTest(unittest.TestCase):
    """One command covers all configured machines and reports a machine failure."""

    def run_report(self, folder: Path) -> subprocess.CompletedProcess[str]:
        """Run the public command with SSH responses kept in temporary files."""
        bin_folder = folder / "bin"
        bin_folder.mkdir()
        write_command(
            bin_folder,
            "ssh",
            'while [ "${1#-}" != "$1" ]; do case "$1" in -F|-o) shift 2;; *) shift;; esac; done\n'
            'cat "$FAKE_MACHINES/$1.json"\n',
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update-report",
                str(ROOT / "examples/cluster.yml"),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{bin_folder}:{os.environ['PATH']}", "FAKE_MACHINES": str(folder / "machines")},
        )

    def test_every_machine_groups_each_package_once_and_shows_list_age(self) -> None:
        """A care group takes priority over a package's source or security pocket."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            machines = folder / "machines"
            machines.mkdir()
            packages = [
                {"name": "linux-image-generic", "ubuntu": True, "security": True},
                {"name": "linux-tools-6.8.0-146", "ubuntu": True, "security": False},
                {"name": "openssh-server", "ubuntu": True, "security": True},
                {"name": "libnm0", "ubuntu": True, "security": False},
                {"name": "mariadb-server", "ubuntu": True, "security": False},
                {"name": "bash", "ubuntu": True, "security": True},
                {"name": "vim", "ubuntu": True, "security": False},
                {"name": "vendor-tool", "ubuntu": False, "security": False},
            ]
            for name in MACHINES:
                (machines / f"{name}.json").write_text(json.dumps({"list_time": 1735689600, "packages": packages}))
            result = self.run_report(folder)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in MACHINES:
            self.assertIn(f"{name}: 8 updates waiting, 3 security", result.stdout)
        for group, package in (
            ("ssh", "openssh-server"),
            ("network", "libnm0"),
            ("mariadb", "mariadb-server"),
            ("security", "bash"),
            ("rest", "vim"),
            ("extra", "vendor-tool"),
        ):
            self.assertIn(f"{group} (1): {package}", result.stdout)
            self.assertEqual(result.stdout.count(package), len(MACHINES))
        self.assertIn("kernel (2): linux-image-generic linux-tools-6.8.0-146", result.stdout)
        self.assertEqual(result.stdout.count("linux-image-generic"), len(MACHINES))
        self.assertEqual(result.stdout.count("linux-tools-6.8.0-146"), len(MACHINES))
        self.assertIn("package lists from 2025-01-01", result.stdout)

    def test_unreachable_machine_is_visible_and_fails_run(self) -> None:
        """A missing response fails the run after reachable machines are shown."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            machines = folder / "machines"
            machines.mkdir()
            for name in MACHINES[:-1]:
                (machines / f"{name}.json").write_text(json.dumps({"list_time": 1735689600, "packages": []}))
            result = self.run_report(folder)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("front: 0 updates waiting", result.stdout)
        self.assertIn("store", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
