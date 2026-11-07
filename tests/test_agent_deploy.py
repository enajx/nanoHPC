"""The Slack agent's deploy: the settings the role writes for the runner and the bot (rendered with Ansible's own
templating), the variables from cluster.yml, and where the role runs."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from nanohpc import cli
from nanohpc.config import check_config
from nanohpc.deploy import agent_cluster_yml, agent_variables

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "src/nanohpc/ansible/roles/agent"
APPROVERS = [{"name": "alice", "slack_id": "U111"}, {"name": "sam", "slack_id": "U222"}]


def config_with(agent: dict[str, Any], auto_deploy: dict[str, Any] | None) -> dict[str, Any]:
    raw = copy.deepcopy(yaml.safe_load((ROOT / "examples/cluster.yml").read_text()))
    raw["agent"] = agent
    if auto_deploy is not None:
        raw["auto_deploy"] = auto_deploy
        raw["nanohpc_version"] = "0.1.0"
    config, errors = check_config(raw)
    assert errors == [], errors
    return config


def render(template: str, config: dict[str, Any]) -> dict[str, Any]:
    """Render one of the role's JSON templates for the front node, as Ansible would."""
    compute = [name for name, machine in config["machines"].items() if "compute" in machine["roles"]]
    variables = {
        "inventory_hostname": "front",
        "groups": {"role_compute": compute},
        "nanohpc": {
            "mode": "slurm",
            "cluster_name": config["cluster"]["name"],
            "machines": config["machines"],
            "agent": agent_variables(config),
            "time_zone": config["time_zone"],
            "auto_deploy": {"simulated": True, "fake_gpus": ["gpu1"]},
        },
    }
    text = (ROLE / "templates" / template).read_text()
    rendered = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text))
    # The template module writes the text; Templar alone may hand back the value already parsed.
    return rendered if isinstance(rendered, dict) else json.loads(rendered)


class AgentSettingsTest(unittest.TestCase):
    def test_without_automatic_deploys_there_are_no_pushes_or_deploys(self) -> None:
        config = config_with({"enabled": True, "channel": "C1AB", "approvers": APPROVERS}, None)
        runner = render("runner.json.j2", config)
        self.assertEqual(runner["approvers"], ["U111", "U222"])
        self.assertEqual(runner["front"], "front")
        self.assertNotIn("front", runner["hosts"])
        self.assertTrue(all(address.count(".") == 3 for address in runner["hosts"].values()))
        self.assertIsNone(runner["repository"])
        self.assertIsNone(runner["deploy"])
        bot = render("bot.json.j2", config)
        self.assertFalse(bot["code"])
        self.assertEqual(bot["approvers"], {"U111": "alice", "U222": "sam"})
        self.assertEqual(bot["limits"], {"daily_usd": 1.0, "request_usd": 1.0, "requests_per_person": 10,
                                         "unlimited": ["U111", "U222"]})  # fmt: skip
        self.assertEqual(sorted(bot["machines"]), sorted(runner["hosts"]))
        self.assertIsNone(bot["people"])

    def test_with_automatic_deploys_it_pushes_to_their_repository_and_deploys_through_them(self) -> None:
        deploy = {"enabled": True, "repository": "git@github.com:lab/config.git", "branch": "stable"}
        config = config_with({"enabled": True, "channel": "C1AB", "approvers": APPROVERS, "daily_usd": 3}, deploy)
        runner = render("runner.json.j2", config)
        self.assertEqual((runner["repository"]["remote"], runner["repository"]["branch"]),
                         ("git@github.com:lab/config.git", "stable"))  # fmt: skip
        self.assertEqual(runner["repository"]["after_push"], "The front node deploys it within 10 minutes.")
        argv = runner["deploy"]
        self.assertEqual(argv[:3], ["systemd-run", "--unit=nanohpc-agent-deploy", "--wait"])
        self.assertIn("--setenv=NANOHPC_FAKE_GPUS=gpu1", argv)
        self.assertEqual(argv[argv.index("--branch") + 1], "stable")
        self.assertEqual(argv[-1], "--again")
        bot = render("bot.json.j2", config)
        self.assertTrue(bot["code"])
        self.assertEqual(bot["limits"]["daily_usd"], 3)

    def test_no_crash_dumps_and_the_bots_processes_are_hidden(self) -> None:
        for name in ("nanohpc-agent-runner", "nanohpc-agent"):
            unit = (ROLE / "templates" / f"{name}.service.j2").read_text()
            self.assertIn("\nLimitCORE=0\n", unit, name)
        self.assertIn("\nProtectProc=invisible\n", (ROLE / "templates/nanohpc-agent.service.j2").read_text())

    def test_the_settings_the_agent_reads_have_no_secrets(self) -> None:
        config = config_with({"enabled": True, "channel": "C1AB", "approvers": APPROVERS}, None)
        config["secrets"] = {"slack_bot_token": "xoxb-secret"}
        self.assertNotIn("xoxb-secret", agent_cluster_yml(config))
        self.assertIn("approvers", agent_cluster_yml(config))


class MonitorDeployFromTheFrontTest(unittest.TestCase):
    """deploy-monitor run by the agent's runner on the monitor host (labelled: deploy_monitor is replaced by a
    recorder, nothing is deployed): an automatic deploy, and on a simulated cluster its fake GPUs."""

    def test_environment_reaches_deploy_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            path.write_text((ROOT / "examples/monitor.yml").read_text())
            fake = {"gpu1": {"count": 2, "type": "A6000"}}
            environment = {
                "NANOHPC_AUTOMATIC": "1",
                "NANOHPC_SIMULATED": "1",
                "NANOHPC_FAKE_GPUS_JSON": json.dumps(fake),
            }
            with (
                mock.patch.dict(os.environ, environment),
                mock.patch.object(cli.deploy, "deploy_monitor", return_value=0) as recorder,
            ):
                self.assertEqual(cli.run_deploy_monitor(path, None, True, None, None), 0)
            _, _, simulated, fake_gpus, dry_run, *_ = recorder.call_args.args
            self.assertEqual((simulated, fake_gpus, dry_run), (True, fake, True))
            with (
                mock.patch.dict(os.environ, {"NANOHPC_SIMULATED": ""}),
                mock.patch.object(cli.deploy, "deploy_monitor", return_value=0) as recorder,
            ):
                cli.run_deploy_monitor(path, None, False, None, None)
            self.assertEqual(recorder.call_args.args[2:4], (False, {}))


if __name__ == "__main__":
    unittest.main()
