"""Check the Prometheus daily recording rules and alert rules: YAML shape always, promtool unit tests when promtool
is installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "src/nanohpc/files/prometheus-daily-rules.yml"
RULES_TEST = REPO / "tests/prometheus_daily_rules_test.yml"
ALERT_RULES = REPO / "src/nanohpc/files/prometheus-alert-rules.yml"
ALERT_RULES_TEST = REPO / "tests/prometheus_alert_rules_test.yml"
MONITOR_DAILY_RULES = REPO / "src/nanohpc/files/prometheus-monitor-daily-rules.yml"
MONITOR_ALERT_RULES = REPO / "src/nanohpc/files/prometheus-monitor-alert-rules.yml"
EXPECTED_ALERTS = {
    "HealthCheckFailing": "critical",
    "HealthCheckWarning": "warning",
    "HealthChecksNotRunning": "warning",
    "HealthChecksMissing": "warning",
    "MachineMetricsMissing": "critical",
    "BackupFailed": "critical",
    "BackupOld": "warning",
    "AutoDeployFailed": "critical",
}
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


class AlertRulesTests(unittest.TestCase):
    """The alert rules have the expected names, severities, and a summary naming the machine."""

    def test_alert_rules_shape(self) -> None:
        """One group of alerts, each with its severity and a summary naming the machine (and the check if any)."""
        groups = yaml.safe_load(ALERT_RULES.read_text())["groups"]
        self.assertEqual([group["name"] for group in groups], ["cluster-alerts"])
        rules = groups[0]["rules"]
        self.assertEqual({rule["alert"]: rule["labels"]["severity"] for rule in rules}, EXPECTED_ALERTS)
        self.assertEqual(len(rules), len(EXPECTED_ALERTS))
        for rule in rules:
            self.assertTrue(rule["expr"].strip(), rule["alert"])
            self.assertIn("$labels.machine", rule["annotations"]["summary"], rule["alert"])
            if rule["alert"] in ("HealthCheckFailing", "HealthCheckWarning"):
                self.assertIn("$labels.check", rule["annotations"]["summary"], rule["alert"])

    @unittest.skipIf(shutil.which("promtool") is None, "promtool is not installed, so the alert rule tests cannot run")
    def test_promtool_alert_tests(self) -> None:
        """promtool checks which alerts fire, and when, for fixed input series, including recoveries."""
        result = subprocess.run(
            ["promtool", "test", "rules", str(ALERT_RULES_TEST)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class MonitorRulesTests(unittest.TestCase):
    """Monitor-only rules summarize machine readings and alert on failed checks or missing metrics."""

    def test_monitor_daily_rules_have_no_scheduler_metrics(self) -> None:
        """Daily history keeps measured machine and GPU series without scheduler figures."""
        groups = yaml.safe_load(MONITOR_DAILY_RULES.read_text())["groups"]
        self.assertEqual([group["name"] for group in groups], ["monitor-minute-values", "monitor-daily-summaries"])
        records = {rule["record"] for group in groups for rule in group["rules"]}
        self.assertIn("cluster_daily_gpu_utilization_percent", records)
        self.assertIn("cluster_daily_scrape_coverage_ratio", records)
        self.assertFalse(any("allocat" in name or "fairshare" in name or "jobs" in name for name in records))
        self.assertFalse(any("cluster_allocated" in rule["expr"] or "cluster_pending_jobs" in rule["expr"]
                             for group in groups for rule in group["rules"]))

    def test_monitor_alerts_cover_health_inventory_and_freshness(self) -> None:
        """Alerts use collector health and inventory mismatch, without storage or scheduler alerts."""
        groups = yaml.safe_load(MONITOR_ALERT_RULES.read_text())["groups"]
        self.assertEqual([group["name"] for group in groups], ["monitor-alerts"])
        rules = {rule["alert"]: rule for rule in groups[0]["rules"]}
        self.assertEqual(set(rules), {
            "HealthCheckFailing", "HealthCheckWarning", "HealthChecksNotRunning", "HealthChecksMissing",
            "MachineMetricsMissing", "MonitorMachineOffline", "MonitorMachineUnhealthy",
            "SnapshotStale", "GpuMetricsStale", "GPUInventoryChanged",
        })
        self.assertIn("cluster_monitor_machine_health", rules["MonitorMachineUnhealthy"]["expr"])
        self.assertIn("cluster_monitor_gpu_inventory_mismatch", rules["GPUInventoryChanged"]["expr"])
        for name, rule in rules.items():
            self.assertTrue(rule["expr"].strip(), name)
            self.assertIn(rule["labels"]["severity"], ("warning", "critical"))
            self.assertNotIn("Slurm", str(rule))
            self.assertNotIn("backup", str(rule).lower())


if __name__ == "__main__":
    unittest.main()
