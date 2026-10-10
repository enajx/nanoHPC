"""Check the wall-power collector through a fake ipmitool command."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-power-metrics"
SUPPLIES = """PSU1 Power In  | DFh | ok | 10.0 | 816 Watts
PSU1 Power Out | 5Ah | ok | 10.0 | 768 Watts
PSU2 Power In  | E4h | ok | 10.0 | 816 Watts
PSU2 Power Out | 5Dh | ok | 10.0 | 768 Watts
CPU_Power      | F3h | ok | 19.0 | 80 Watts"""


class PowerMetricsTest(unittest.TestCase):
    """Only complete power-supply input readings become a wall-power metric."""

    def test_complete_reading_and_failed_reading(self) -> None:
        """Sum inputs, then preserve the last file when a supply cannot be read."""
        self.assertTrue(SCRIPT.exists(), SCRIPT)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / "ipmitool"
            fake.write_text(
                f"#!{sys.executable}\nimport os, sys\n"
                "assert sys.argv[1:] == ['sdr', 'type', 'Power Supply']\n"
                "print(os.environ['IPMI_FIXTURE'])\n"
                "sys.exit(int(os.environ.get('IPMI_EXIT', '0')))\n"
            )
            fake.chmod(0o700)
            output = root / "power.prom"
            environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"])

            def run(reading: str, exit_code: str) -> subprocess.CompletedProcess[str]:
                """Run the shipped collector with one hardware response."""
                return subprocess.run(
                    [sys.executable, str(SCRIPT), "--output", str(output)],
                    env=dict(environment, IPMI_FIXTURE=reading, IPMI_EXIT=exit_code),
                    capture_output=True,
                    text=True,
                    check=False,
                )

            result = run(SUPPLIES, "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = output.read_text().splitlines()
            self.assertIn('cluster_wall_power_watts{source="power_supplies"} 1632', lines)
            timestamp = float(
                next(
                    line.split()[1] for line in lines if line.startswith("cluster_power_collection_timestamp_seconds ")
                )
            )
            self.assertLess(abs(timestamp - time.time()), 10)
            saved = output.read_text()
            for reading, exit_code in (
                (SUPPLIES.replace("816 Watts", "No Reading", 1), "0"),
                ("PSU1 AC Lost | D1h | ns | 10.0 | No Reading", "0"),
                (SUPPLIES, "1"),
            ):
                with self.subTest(reading=reading, exit_code=exit_code):
                    self.assertNotEqual(run(reading, exit_code).returncode, 0)
                    self.assertEqual(output.read_text(), saved)


if __name__ == "__main__":
    unittest.main()
