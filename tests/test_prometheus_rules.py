"""Check the Prometheus daily recording rules and alert rules: YAML shape always, promtool unit tests when promtool
is installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

import jinja2
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
    "MachineCheckWarning": "warning",
    "BackupFailed": "critical",
    "BackupOld": "warning",
    "AutoDeployFailed": "critical",
}
EXPECTED = [
    "cluster_daily_cpu_busy_ratio",
    "cluster_daily_memory_used_ratio",
    "cluster_daily_filesystem_available_bytes_min",
    "cluster_daily_gpu_utilization_percent",
    "cluster_daily_gpu_memory_used_percent",
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

    def test_test_accounts_are_excluded_from_daily_user_records(self) -> None:
        """Render the deploy task and check all three per-user rules, including no test accounts."""
        source = RULES.read_text()
        token = "__NANOHPC_TEST_ACCOUNT_REGEX__"
        records = {
            "cluster_daily_allocated_gpu_hours_total",
            "cluster_daily_decayed_gpu_hours",
            "cluster_daily_fairshare_factor",
        }
        self.assertEqual(source.count(token), 3)
        tasks = yaml.safe_load((REPO / "src/nanohpc/ansible/roles/prometheus/tasks/main.yml").read_text())
        prepared = next(task for task in tasks if task["name"].startswith("Prepare daily rules"))
        installed = next(task for task in tasks if task["name"].startswith("Install the daily summary rules"))
        self.assertEqual(installed["ansible.builtin.copy"]["content"], "{{ daily_rules_text }}")
        environment = jinja2.Environment()
        environment.globals["lookup"] = lambda _kind, path, rstrip: (
            Path(path).read_text().rstrip() if rstrip else Path(path).read_text()
        )
        for users, matcher in (
            ([{"name": "alice"}, {"name": "test-user_1", "test_account": True}], 'user!~"test-user_1"'),
            ([{"name": "alice"}], 'user!~"a^"'),
        ):
            with self.subTest(users=users):
                variables = {"nanohpc": {"package_files": str(RULES.parent), "users": users, "mode": "slurm"}}
                for key, template in prepared["vars"].items():
                    variables[key] = environment.from_string(template).render(**variables)
                rendered_text = environment.from_string(
                    prepared["ansible.builtin.set_fact"]["daily_rules_text"]
                ).render(**variables)
                self.assertNotIn(token, rendered_text)
                rendered = yaml.safe_load(rendered_text)
                rules = {rule["record"]: rule["expr"] for group in rendered["groups"] for rule in group["rules"]}
                for record in records:
                    self.assertIn(matcher, rules[record])
                    self.assertNotIn("\\-", rules[record])
                for record in set(rules) - records:
                    self.assertNotIn("user!~", rules[record])
        variables = {"nanohpc": {"package_files": str(RULES.parent), "users": ["alice"], "mode": "monitor"}}
        for key, template in prepared["vars"].items():
            variables[key] = environment.from_string(template).render(**variables)
        monitor_text = environment.from_string(prepared["ansible.builtin.set_fact"]["daily_rules_text"]).render(
            **variables
        )
        self.assertEqual(monitor_text, MONITOR_DAILY_RULES.read_text())

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
        records = [rule for rule in groups[0]["rules"] if "record" in rule]
        self.assertEqual([rule["record"] for rule in records], ["cluster_machine_maintenance"])
        rules = [rule for rule in groups[0]["rules"] if "alert" in rule]
        self.assertEqual({rule["alert"]: rule["labels"]["severity"] for rule in rules}, EXPECTED_ALERTS)
        self.assertEqual(len(rules), len(EXPECTED_ALERTS))
        for rule in rules:
            self.assertTrue(rule["expr"].strip(), rule["alert"])
            self.assertIn("$labels.machine", rule["annotations"]["summary"], rule["alert"])
            if rule["alert"] in ("HealthCheckFailing", "HealthCheckWarning"):
                self.assertIn("$labels.check", rule["annotations"]["summary"], rule["alert"])

    def test_no_alert_for_a_machine_under_maintenance(self) -> None:
        """Every alert leaves out machines under maintenance, also when they are off, so Slack gets nothing."""
        for rule in yaml.safe_load(ALERT_RULES.read_text())["groups"][0]["rules"]:
            if "alert" in rule:
                self.assertIn("unless on(machine) cluster_machine_maintenance", rule["expr"], rule["alert"])

    def test_health_checks_not_running_waits_for_the_first_run_after_boot(self) -> None:
        """A machine back from a long outage shows its old health file until its checks run, 2 minutes after boot."""
        rules = {rule.get("alert"): rule for rule in yaml.safe_load(ALERT_RULES.read_text())["groups"][0]["rules"]}
        self.assertEqual(rules["HealthChecksNotRunning"].get("for"), "10m")

    def test_every_alert_keeps_firing_for_7_days(self) -> None:
        """A problem that comes back within 7 days of ending is the same alert, so Slack gets nothing new."""
        for path in (ALERT_RULES, MONITOR_ALERT_RULES):
            for group in yaml.safe_load(path.read_text())["groups"]:
                for rule in group["rules"]:
                    if "alert" in rule:
                        self.assertEqual(rule.get("keep_firing_for"), "7d", f"{path.name}: {rule['alert']}")

    def test_slack_posts_only_the_start_of_a_problem(self) -> None:
        """Alertmanager posts a problem once, when it starts: no post when it ends and no repeat while it lasts."""
        tasks = yaml.safe_load((REPO / "src/nanohpc/ansible/roles/alerts/tasks/main.yml").read_text())
        task = next(task for task in tasks if task["name"] == "Install the Alertmanager configuration")
        text = (
            jinja2.Environment()
            .from_string(task["vars"]["alertmanager_config_text"])
            .render(nanohpc={"alerts": {"slack": True}, "cluster_name": "lab"})
        )
        config = yaml.safe_load(text)
        self.assertEqual(config["route"]["repeat_interval"], "8760h")
        slack = next(receiver for receiver in config["receivers"] if receiver["name"] == "slack")
        self.assertIs(slack["slack_configs"][0]["send_resolved"], False)
        self.assertNotIn("Resolved", text)
        self.assertNotIn("green", text)

    def test_prometheus_keeps_alert_state_across_deploys(self) -> None:
        """A deploy that changes the configuration or rules reloads Prometheus, which keeps its alerts. A restart
        loses them; Alertmanager then holds each alert 20 minutes (4 resend delays), long enough for a problem
        still there to fire again before Alertmanager would count it as ended and post it again."""
        tasks = yaml.safe_load((REPO / "src/nanohpc/ansible/roles/prometheus/tasks/main.yml").read_text())
        units = next(task for task in tasks if task["name"] == "Install the Prometheus services")
        content = units["ansible.builtin.copy"]["content"]
        self.assertIn("--rules.alert.resend-delay=5m", content)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", content)
        run = next(task for task in tasks if task["name"] == "Run the detail Prometheus")
        state = run["ansible.builtin.systemd_service"]["state"]
        for unit, rules, expected in ((False, True, "reloaded"), (True, False, "restarted"), (False, False, "started")):
            rendered = jinja2.Environment().from_string(state).render(
                prometheus_units={"changed": unit},
                detail_config={"changed": False},
                daily_rules={"changed": False},
                alert_rules={"changed": rules},
            )
            self.assertEqual(rendered.strip(), expected)

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
        self.assertIn("cluster_daily_gpu_memory_used_percent", records)
        self.assertIn("cluster_daily_scrape_coverage_ratio", records)
        self.assertFalse(any("allocat" in name or "fairshare" in name or "jobs" in name for name in records))
        self.assertFalse(
            any(
                "cluster_allocated" in rule["expr"] or "cluster_pending_jobs" in rule["expr"]
                for group in groups
                for rule in group["rules"]
            )
        )

    def test_monitor_alerts_cover_health_inventory_and_freshness(self) -> None:
        """Alerts use collector health and inventory mismatch, without storage or scheduler alerts."""
        groups = yaml.safe_load(MONITOR_ALERT_RULES.read_text())["groups"]
        self.assertEqual([group["name"] for group in groups], ["monitor-alerts"])
        rules = {rule["alert"]: rule for rule in groups[0]["rules"]}
        self.assertEqual(
            set(rules),
            {
                "HealthCheckFailing",
                "HealthCheckWarning",
                "HealthChecksNotRunning",
                "HealthChecksMissing",
                "MachineMetricsMissing",
                "MonitorMachineUnhealthy",
                "SnapshotStale",
                "GpuMetricsStale",
                "GPUInventoryChanged",
            },
        )
        # An offline machine gets only MachineMetricsMissing, not a second post from the snapshot.
        self.assertNotIn("MonitorMachineOffline", rules)
        self.assertIn("cluster_monitor_machine_health", rules["MonitorMachineUnhealthy"]["expr"])
        self.assertIn("cluster_monitor_gpu_inventory_mismatch", rules["GPUInventoryChanged"]["expr"])
        for name, rule in rules.items():
            self.assertTrue(rule["expr"].strip(), name)
            self.assertIn(rule["labels"]["severity"], ("warning", "critical"))
            self.assertNotIn("Slurm", str(rule))
            self.assertNotIn("backup", str(rule).lower())


if __name__ == "__main__":
    unittest.main()
