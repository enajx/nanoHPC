"""Test kept scratch job copies through the cleanup command, with fake Slurm accounting."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

COMMAND = Path(__file__).resolve().parents[1] / "src/nanohpc/files/scratch-job-cleanup"


class ScratchJobCleanupTest(unittest.TestCase):
    """Remove only copies whose Slurm job ended before the retention cutoff."""

    def run_cleanup(self, records: dict[str, str], sacct_exit: int) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Make job copies and a fake sacct, then invoke the installed command's source."""
        directory = Path(tempfile.mkdtemp(prefix="scratch-job-cleanup-")).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(directory)], check=True)
        root = directory / "cluster-jobs"
        root.mkdir()
        for name in ("job-100-old", "job-101-recent", "job-102-running", "job-103-unknown", "notes"):
            folder = root / name
            folder.mkdir()
            (folder / "file").write_text("data")
        outside = directory / "outside"
        outside.mkdir()
        (outside / "file").write_text("keep")
        (root / "job-104-link").symlink_to(outside, target_is_directory=True)
        fake = directory / "sacct"
        fake.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"records = json.loads({json.dumps(records)!r})\n"
            "jobs = sys.argv[sys.argv.index('-j') + 1].split(',')\n"
            "for job in jobs:\n"
            "    for row in records.get(job, '').splitlines():\n"
            "        print(f'{job}|{row}')\n"
            f"sys.exit({sacct_exit})\n"
        )
        fake.chmod(0o700)
        environment = {
            **os.environ,
            "PATH": str(directory) + os.pathsep + os.environ["PATH"],
            "SCRATCH_JOB_ROOT": str(root),
            "SCRATCH_JOB_RETENTION_DAYS": "7",
        }
        result = subprocess.run(
            [sys.executable, str(COMMAND)], env=environment, text=True, capture_output=True, check=False
        )
        return result, root

    def test_old_completed_copy_is_removed_and_other_folders_stay(self) -> None:
        """Use Slurm's end time, and leave unknown jobs and unrelated paths alone."""
        stamp = lambda days: (datetime.now(UTC).astimezone().replace(tzinfo=None) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        records = {
            "100": f"FAILED|{stamp(8)}\n",
            "101": f"TIMEOUT|{stamp(2)}\n",
            "102": "RUNNING|Unknown\n",
        }
        result, root = self.run_cleanup(records, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sorted(path.name for path in root.iterdir()),
            ["job-101-recent", "job-102-running", "job-103-unknown", "job-104-link", "notes"],
        )
        self.assertTrue((root.parent / "outside/file").exists())

    def test_failed_accounting_stops_cleanup(self) -> None:
        """Even if sacct prints a completed job, a failed query leaves all copies intact."""
        ended = (datetime.now(UTC).astimezone().replace(tzinfo=None) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%S")
        result, root = self.run_cleanup({"100": f"FAILED|{ended}\n"}, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((root / "job-100-old/file").exists())


if __name__ == "__main__":
    unittest.main()
