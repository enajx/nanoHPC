"""Check the Prometheus daily recording rules: YAML shape always, promtool unit tests when promtool is installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "src/nanohpc/files/prometheus-daily-rules.yml"
RULES_TEST = REPO / "tests/prometheus_daily_rules_test.yml"
EXPECTED = [
    "cluster_daily_cpu_busy_ratio",
    "cluster_daily_memory_used_ratio",
    "cluster_daily_filesystem_available_bytes_min",
    "cluster_daily_gpu_utilization_percent",
    "cluster_daily_gpu_temperature_celsius_max",
    "cluster_daily_gpu_power_watts",
    "cluster_daily_allocated_gpu_hours_total",
    "cluster_daily_decayed_gpu_hours",
    "cluster_daily_fairshare_factor",
    "cluster_daily_pending_jobs_max",
    "cluster_daily_running_jobs_max",
    "cluster_daily_scrape_coverage_ratio",
]


class DailyRulesTests(unittest.TestCase):
    """The daily summaries are recording rules with the expected names."""

    def test_rules_file_shape(self) -> None:
        """Minute values first, then the daily summaries with exactly the expected names."""
        document = yaml.safe_load(RULES.read_text())
        groups = document["groups"]
        self.assertEqual([group["name"] for group in groups], ["cluster-minute-values", "cluster-daily-summaries"])
        rules = groups[1]["rules"]
        self.assertEqual([rule["record"] for rule in rules], EXPECTED)
        for rule in rules:
            self.assertIsInstance(rule["expr"], str)
            self.assertTrue(rule["expr"].strip())

    def test_no_subqueries(self) -> None:
        """No rule re-evaluates a 24-hour subquery every minute: that runs out of query samples on large clusters."""
        for group in yaml.safe_load(RULES.read_text())["groups"]:
            for rule in group["rules"]:
                self.assertNotRegex(rule["expr"], r"\[\d+[smhd]:", rule["record"])

    @unittest.skipIf(shutil.which("promtool") is None, "promtool is not installed, so the rule unit tests cannot run")
    def test_promtool_rule_tests(self) -> None:
        """promtool evaluates the rules against fixed input series and expected results."""
        result = subprocess.run(
            ["promtool", "test", "rules", str(RULES_TEST)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
