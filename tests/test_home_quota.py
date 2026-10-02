"""Verify the root-run quota reader publishes only the named cluster users as node_exporter metrics (fake repquota)."""

import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

READER = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-home-quotas"

HEADER = (
    "User,BlockStatus,FileStatus,BlockUsed,BlockSoftLimit,BlockHardLimit,BlockGrace,FileUsed,FileSoftLimit,"
    "FileHardLimit,FileGrace"
)


class HomeQuotaTests(unittest.TestCase):
    """Read quota CSV through the actual command and keep its last good output."""

    def test_only_named_users_and_atomic_failure(self) -> None:
        """Only the named users are written, repquota reads the given filesystem, and a failure keeps old output."""
        with tempfile.TemporaryDirectory(prefix="home-quota-") as directory:
            root = Path(directory)
            output = root / "home-quotas.prom"
            called = root / "called-with"
            command = root / "repquota"
            # Fake repquota: records its arguments, then prints CSV. Soft and hard limits may be equal.
            command.write_text(
                "#!/bin/sh\n"
                f"echo \"$@\" > '{called}'\n"
                f"printf '{HEADER}\\n'\n"
                "printf 'alice,ok,ok,307200,307200,409600,,12,0,0,\\n'\n"
                "printf 'bob,ok,ok,1024,307200,307200,,8,0,0,\\n'\n"
                "printf 'root,ok,ok,999,0,0,,1,0,0,\\n'\n"
            )
            command.chmod(0o700)
            args = [
                sys.executable,
                str(READER),
                "--output",
                str(output),
                "--repquota",
                str(command),
                "--filesystem",
                "/",
                "--users",
                "alice",
                "bob",
            ]
            completed = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(called.read_text().split(), ["-u", "-O", "csv", "/"])
            lines = output.read_text().splitlines()
            values = {line.rsplit(" ", 1)[0]: line.rsplit(" ", 1)[1] for line in lines if not line.startswith("#")}
            self.assertEqual(values['cluster_home_quota_used_bytes{user="alice"}'], str(307200 * 1024))
            self.assertEqual(values['cluster_home_quota_soft_bytes{user="alice"}'], str(307200 * 1024))
            self.assertEqual(values['cluster_home_quota_hard_bytes{user="alice"}'], str(409600 * 1024))
            self.assertEqual(values['cluster_home_quota_hard_bytes{user="bob"}'], str(307200 * 1024))
            self.assertLess(abs(float(values["cluster_home_quota_collected_timestamp_seconds"]) - time.time()), 60)
            self.assertEqual(len(values), 7)
            for name in ("used_bytes", "soft_bytes", "hard_bytes", "collected_timestamp_seconds"):
                self.assertIn(f"# HELP cluster_home_quota_{name} ", output.read_text())
                self.assertIn(f"# TYPE cluster_home_quota_{name} gauge", lines)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)
            self.assertNotIn("root", output.read_text())
            before = output.read_bytes()
            command.write_text("#!/bin/sh\nexit 1\n")
            failed = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_missing_user_fails(self) -> None:
        """A named user without a quota record is an error, not a silent gap."""
        with tempfile.TemporaryDirectory(prefix="home-quota-") as directory:
            root = Path(directory)
            command = root / "repquota"
            command.write_text(f"#!/bin/sh\nprintf '{HEADER}\\nalice,ok,ok,1,300,400,,1,0,0,\\n'\n")
            command.chmod(0o700)
            args = [
                sys.executable,
                str(READER),
                "--output",
                str(root / "home-quotas.prom"),
                "--repquota",
                str(command),
                "--filesystem",
                "/home",
                "--users",
                "alice",
                "bob",
            ]
            failed = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Missing cluster user quota record", failed.stderr)
            self.assertFalse((root / "home-quotas.prom").exists())


if __name__ == "__main__":
    unittest.main()
