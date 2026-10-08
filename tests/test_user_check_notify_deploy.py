"""The alerts role runs the channel notifier on the front node when Slack is on."""

import unittest
from pathlib import Path

import yaml
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "src/nanohpc/ansible/roles/alerts/tasks/main.yml"
PARTIAL = ROOT / "src/nanohpc/ansible/partial.yml"
AUTO_DEPLOY = ROOT / "src/nanohpc/ansible/roles/auto_deploy/tasks/main.yml"


class UserCheckNotifierDeployTest(unittest.TestCase):
    """The deployed timer can read Prometheus and the shared Slack webhook."""

    def setUp(self) -> None:
        self.tasks = yaml.safe_load(TASKS.read_text())

    def task(self, name: str) -> dict:
        """Find one named role task."""
        return next(task for task in self.tasks if task["name"] == name)

    def test_notifier_is_installed_as_executable(self) -> None:
        task = self.task("Install the user check channel notifier")
        copy = task["ansible.builtin.copy"]
        self.assertEqual(copy["src"], "{{ nanohpc.package_files }}/nanohpc-user-check-notify")
        self.assertEqual(copy["dest"], "/usr/local/bin/nanohpc-user-check-notify")
        self.assertEqual(copy["mode"], "0755")

    def test_service_uses_existing_alert_account_and_webhook(self) -> None:
        task = self.task("Install the user check channel notifier service and timer")
        self.assertEqual(task["loop"], ["service", "timer"])
        unit = Template(task["ansible.builtin.copy"]["content"]).render(
            item="service",
            nanohpc={"users": [{"name": "alice", "slack_id": "U123"}, {"name": "bob"}]},
            nanohpc_secrets={"slack_bot_token": "xoxb-test-token"},
        )
        for line in (
            "User=cluster-alertmanager",
            "Group=cluster-alertmanager",
            "--prometheus http://127.0.0.1:9090",
            "--webhook-file /etc/nanohpc/alertmanager/slack-webhook",
            "--state /var/lib/nanohpc/alertmanager/user-check.json",
            "--now now",
            "ReadWritePaths=/var/lib/nanohpc/alertmanager",
        ):
            self.assertIn(line, unit)
        self.assertIn("--bot-token-file /etc/nanohpc/alertmanager/slack-bot-token", unit)
        self.assertIn("--user alice:U123", unit)
        self.assertIn("--user bob:", unit)

    def test_bot_token_is_private_and_removed_when_unset(self) -> None:
        install = self.task("Install the optional Slack bot token")
        self.assertEqual(install["ansible.builtin.copy"]["mode"], "0640")
        self.assertEqual(install["ansible.builtin.copy"]["group"], "cluster-alertmanager")
        self.assertTrue(install["no_log"])
        remove = self.task("Remove the Slack bot token when unset")
        self.assertEqual(remove["ansible.builtin.file"]["state"], "absent")

    def test_without_token_service_keeps_channel_only(self) -> None:
        task = self.task("Install the user check channel notifier service and timer")
        unit = Template(task["ansible.builtin.copy"]["content"]).render(
            item="service", nanohpc={"users": [{"name": "alice"}]}, nanohpc_secrets={"slack_bot_token": None}
        )
        self.assertIn("--user alice:", unit)
        self.assertNotIn("--bot-token-file", unit)

    def test_monitor_mode_does_not_render_slurm_user_args(self) -> None:
        task = self.task("Install the user check channel notifier service and timer")
        unit = Template(task["ansible.builtin.copy"]["content"]).render(
            item="service", nanohpc={"mode": "monitor", "users": ["alice"]}, nanohpc_secrets={"slack_bot_token": None}
        )
        self.assertNotIn("--user", unit)

    def test_partial_users_refreshes_recipients(self) -> None:
        plays = yaml.safe_load(PARTIAL.read_text())
        play = next(
            play for play in plays if play["name"] == "Refresh Slack user mentions and private message recipients"
        )
        self.assertEqual(play["hosts"], "role_front")
        self.assertEqual(play["tags"], ["users"])
        self.assertIn("alerts", play["roles"])

    def test_automatic_deploy_keeps_optional_bot_token_out_of_git(self) -> None:
        tasks = yaml.safe_load(AUTO_DEPLOY.read_text())
        task = next(
            task
            for task in tasks
            if task["name"] == "Put the secrets next to the checkout's cluster.yml (kept out of Git)"
        )
        content = Template(task["ansible.builtin.copy"]["content"]).render(
            nanohpc_secrets={
                "slack_webhook": "https://example.org",
                "slack_bot_token": "xoxb-test",
                "deploy_webhook": None,
            }
        )
        self.assertIn("NANOHPC_SLACK_BOT_TOKEN=xoxb-test", content)
        self.assertEqual(task["ansible.builtin.copy"]["mode"], "0600")
        self.assertTrue(task["no_log"])

    def test_timer_runs_each_minute_only_with_slack_and_user_check(self) -> None:
        task = self.task("Install the user check channel notifier service and timer")
        unit = Template(task["ansible.builtin.copy"]["content"]).render(item="timer")
        self.assertIn("OnUnitActiveSec=1min", unit)
        run = self.task("Run the user check channel notifier timer")
        service = run["ansible.builtin.systemd_service"]
        self.assertEqual(service["name"], "nanohpc-user-check-notify.timer")
        for mode, slack, enabled, state in (
            ("slurm", True, "True", "started"),
            ("slurm", False, "False", "stopped"),
            ("monitor", True, "False", "stopped"),
            ("monitor", False, "False", "stopped"),
        ):
            nanohpc = {"mode": mode, "alerts": {"slack": slack}}
            self.assertEqual(Template(service["enabled"]).render(nanohpc=nanohpc), enabled)
            self.assertEqual(Template(service["state"]).render(nanohpc=nanohpc), state)

    def test_enabled_notifier_runs_once_during_deploy(self) -> None:
        """A bad Prometheus query or webhook is reported by deploy immediately."""
        task = self.task("Check the user check channel notifier now")
        self.assertEqual(task["ansible.builtin.systemd_service"]["name"], "nanohpc-user-check-notify.service")
        self.assertEqual(task["ansible.builtin.systemd_service"]["state"], "started")
        self.assertIn("nanohpc.alerts.slack", task["when"])
        self.assertIn("not ansible_check_mode", task["when"])


if __name__ == "__main__":
    unittest.main()
