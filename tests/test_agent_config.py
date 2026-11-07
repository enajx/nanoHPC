"""The optional Slack agent in cluster.yml: defaults, checks, its files, and its secrets in .env."""

import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import check_config, load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/cluster.yml"
MONITOR = ROOT / "examples/monitor.yml"
APPROVERS = [{"name": "alice", "slack_id": "U111"}, {"name": "sam", "slack_id": "U222"}]
ON = {"enabled": True, "channel": "C0123ABC", "approvers": APPROVERS}
SECRETS = "NANOHPC_SLACK_BOT_TOKEN=xoxb-1\nNANOHPC_SLACK_APP_TOKEN=xapp-1\nNANOHPC_ANTHROPIC_API_KEY=sk-ant-1\n"


def errors_with(source: Path, agent: Any) -> tuple[dict[str, Any], list[str]]:
    raw = yaml.safe_load(source.read_text())
    if agent is not None:
        raw["agent"] = agent
    return check_config(raw)


class AgentConfigTest(unittest.TestCase):
    def test_off_by_default_with_the_agreed_limits(self) -> None:
        for source in (EXAMPLE, MONITOR):
            with self.subTest(source.name):
                config, errors = errors_with(source, None)
                self.assertEqual(errors, [])
                self.assertEqual(
                    config["agent"],
                    {
                        "enabled": False,
                        "channel": None,
                        "approvers": [],
                        "daily_usd": 1.0,
                        "request_usd": 1.0,
                        "requests_per_person": 10,
                        "personality": None,
                        "people": None,
                        "repository": None,
                    },
                )

    def test_turned_on_in_both_modes(self) -> None:
        config, errors = errors_with(EXAMPLE, {**ON, "daily_usd": 5, "requests_per_person": 3})
        self.assertEqual(errors, [])
        self.assertEqual(config["agent"]["approvers"], APPROVERS)
        self.assertEqual((config["agent"]["daily_usd"], config["agent"]["requests_per_person"]), (5, 3))
        repository = "git@github.com:lab/monitor-config.git"
        config, errors = errors_with(MONITOR, {**ON, "enabled": False, "repository": repository})
        self.assertEqual(errors, [])
        self.assertEqual(config["agent"]["repository"], repository)

    def test_monitor_mode_pins_a_version_for_deploys_from_slack(self) -> None:
        repository = {**ON, "repository": "git@github.com:lab/monitor-config.git"}
        raw = yaml.safe_load(MONITOR.read_text())
        raw["agent"] = {**repository, "enabled": True}
        raw["nanohpc_version"] = "0.1.0"
        config, errors = check_config(raw)
        self.assertEqual((errors, config["nanohpc_version"]), ([], "0.1.0"))
        del raw["nanohpc_version"]
        self.assertIn(
            "nanohpc_version is required when agent.repository is set (the version the monitor host installs)",
            check_config(raw)[1],
        )
        raw["nanohpc_version"] = "1.0"
        self.assertIn('nanohpc_version must be a version such as "0.1.0", in quotes', check_config(raw)[1])
        raw["nanohpc_version"] = "0.1.0"
        raw["agent"]["enabled"] = False
        self.assertEqual(check_config(raw)[1], [])

    def test_bad_settings_name_their_field(self) -> None:
        cases = [
            ({"enabled": True, "approvers": APPROVERS}, "agent.channel is required when agent.enabled is true"),
            ({**ON, "channel": "#hpc"}, "agent.channel must be a Slack channel ID such as C0123ABC"),
            ({**ON, "approvers": []}, "agent.approvers must list at least one Slack user when agent.enabled is true"),
            ({**ON, "approvers": [{"name": "alice"}]}, "agent.approvers[0].slack_id is required"),
            (
                {**ON, "approvers": [{"name": "alice", "slack_id": "alice"}]},
                "agent.approvers[0].slack_id must be a Slack member ID such as U123ABC",
            ),
            (
                {**ON, "approvers": [*APPROVERS, {"name": "bo", "slack_id": "U111"}]},
                "agent.approvers[2].slack_id U111 is listed twice",
            ),
            ({**ON, "daily_usd": 0}, "agent.daily_usd must be a positive number of US dollars"),
            ({**ON, "request_usd": "1"}, "agent.request_usd must be a positive number of US dollars"),
            ({**ON, "requests_per_person": 0}, "agent.requests_per_person must be a positive integer"),
            ({**ON, "colour": "red"}, "agent.colour is not a known field"),
            ({**ON, "repository": "git@github.com:lab/c.git"}, "agent.repository is for monitor mode"),
        ]
        for agent, expected in cases:
            with self.subTest(expected):
                _, errors = errors_with(EXAMPLE, agent)
                self.assertTrue(any(error.startswith(expected) for error in errors), errors)
        _, errors = errors_with(MONITOR, {**ON, "repository": "https://github.com/lab/c.git"})
        self.assertIn("agent.repository must be a Git repository over SSH", "\n".join(errors))


class AgentFilesAndSecretsTest(unittest.TestCase):
    def load(self, folder: Path, agent: dict[str, Any], env: str | None) -> tuple[dict[str, Any], list[str]]:
        raw = yaml.safe_load(EXAMPLE.read_text())
        raw["agent"] = agent
        path = folder / "cluster.yml"
        path.write_text(yaml.safe_dump(raw))
        if env is not None:
            (folder / ".env").write_text(env)
        return load_config(path, True, False)

    def test_secrets_and_files_are_read_next_to_cluster_yml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "agent").mkdir()
            (folder / "agent/personality.md").write_text("Dry humour.\n")
            (folder / "agent/people.md").write_text("alice: likes tea\n")
            agent = {**ON, "personality": "agent/personality.md", "people": "agent/people.md"}
            config, errors = self.load(folder, agent, SECRETS)
            self.assertEqual(errors, [])
            self.assertEqual(config["agent"]["personality"], str((folder / "agent/personality.md").resolve()))
            self.assertEqual(config["agent"]["people"], str((folder / "agent/people.md").resolve()))
            self.assertEqual(
                (config["secrets"]["slack_app_token"], config["secrets"]["anthropic_api_key"]), ("xapp-1", "sk-ant-1")
            )

    def test_missing_secrets_and_files_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            env = folder.resolve() / ".env"
            _, errors = self.load(folder, {**ON, "people": "people.md"}, None)
            self.assertIn(f"agent.enabled is true but {env} has no NANOHPC_SLACK_BOT_TOKEN", errors)
            self.assertIn(f"agent.enabled is true but {env} has no NANOHPC_SLACK_APP_TOKEN", errors)
            self.assertIn(f"agent.enabled is true but {env} has no NANOHPC_ANTHROPIC_API_KEY", errors)
            self.assertIn(f"agent.people: {(folder / 'people.md').resolve()} not found", errors)
            bad = "NANOHPC_SLACK_BOT_TOKEN=xoxb-1\nNANOHPC_SLACK_APP_TOKEN=xoxb-2\nNANOHPC_ANTHROPIC_API_KEY=key\n"
            _, errors = self.load(folder, ON, bad)
            self.assertIn("NANOHPC_SLACK_APP_TOKEN in .env must be a Slack app-level token starting xapp-", errors)
            self.assertIn("NANOHPC_ANTHROPIC_API_KEY in .env must be an Anthropic API key starting sk-ant-", errors)

    def test_no_agent_secrets_needed_while_it_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, errors = self.load(Path(directory), {"enabled": False}, None)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
