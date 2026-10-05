"""The monitor-only deploy prepares only monitoring inputs for Ansible."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from nanohpc import deploy

ROOT = Path(__file__).resolve().parents[1]


class MonitorDeployTest(unittest.TestCase):
    """Check the public command and the files passed to the monitoring playbook."""

    def test_command_is_available(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from nanohpc.cli import main; main()", "deploy-monitor", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--only", result.stdout)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--accept-hardware-change", result.stdout)

    def test_prepared_files_have_no_slurm_setup(self) -> None:
        config = {
            "cluster": {
                "name": "lab",
                "mode": "monitor",
                "monitor_host": "front",
                "website": {
                    "hostname": "lab.example.org",
                    "https": "letsencrypt",
                    "path": "/cluster/",
                    "allow": [],
                    "forwarded_by": None,
                    "build": "package",
                    "certificate": None,
                    "certificate_key": None,
                    "logo": None,
                    "login_address": "lab.example.org",
                },
            },
            "machines": {
                "front": {"address": "10.0.0.10", "aliases": []},
                "gpu1": {"address": "10.0.0.11", "aliases": []},
            },
            "users": ["alice"],
            "alerts": {"slack": False},
            "secrets": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            deploy.prepare_monitor(config, None, False, {}, work, None)
            inventory = yaml.safe_load((work / "inventory.yml").read_text())["all"]["children"]
            self.assertEqual(list(inventory["role_front"]["hosts"]), ["front"])
            self.assertEqual(list(inventory["role_machine"]["hosts"]), ["gpu1"])
            self.assertNotIn("role_slurm", inventory)
            self.assertFalse((work / "files" / "slurm.conf").exists())
            values = json.loads((work / "vars.json").read_text())["nanohpc"]
            self.assertEqual(values["mode"], "monitor")
            self.assertEqual(values["uv"]["version"], deploy.UV["version"])
            self.assertNotIn("slurm", values)
            self.assertEqual(values["users"], ["alice"])
            site = json.loads((work / "files" / "site.json").read_text())
            self.assertEqual(site["mode"], "monitor")
            self.assertEqual(site["users"], ["alice"])
            self.assertEqual(site["cluster_name"], "lab")
            machines = json.loads((work / "files" / "monitor-machines.json").read_text())
            self.assertEqual(machines["front"]["role"], "monitor")
            self.assertEqual(machines["gpu1"]["role"], "machine")

    def test_existing_slurm_setup_is_refused_before_any_deploy(self) -> None:
        config = {"cluster": {"monitor_host": "front", "name": "lab"}, "machines": {"front": {}, "gpu1": {}}}
        found = deploy.Probe(
            hostnames={"front": "front", "gpu1": "gpu1"},
            needs_password=[],
            errors=[],
            deployed={"front": ["front", "home"], "gpu1": None},
        )
        with patch.object(deploy, "probe", return_value=found), patch.object(deploy, "probe_monitor_gpus") as gpus:
            self.assertEqual(deploy.deploy_monitor(config, None, False, {}, True, None, None), 1)
            gpus.assert_not_called()

    def test_changed_gpu_inventory_needs_explicit_accept(self) -> None:
        config = {"cluster": {"monitor_host": "front", "name": "lab"}, "machines": {"front": {}, "gpu1": {}}}
        found = deploy.Probe(
            hostnames={"front": "front", "gpu1": "gpu1"},
            needs_password=[],
            errors=[],
            deployed={"front": ["monitor"], "gpu1": ["machine"]},
        )
        old = {"count": 1, "models": ["A"], "fake": False}
        new = {"count": 1, "models": ["B"], "fake": False}
        with (
            patch.object(deploy, "probe", return_value=found),
            patch.object(deploy, "probe_monitor_gpus", return_value=({"front": old, "gpu1": new}, [])),
            patch.object(deploy, "read_monitor_baseline", return_value=({"front": old, "gpu1": old}, None)),
            patch.object(deploy, "prepare_monitor") as prepare,
        ):
            self.assertEqual(deploy.deploy_monitor(config, None, False, {}, True, None, None), 1)
            prepare.assert_not_called()

    def test_node_deploy_checks_only_selected_machine_and_host(self) -> None:
        config = {
            "cluster": {"monitor_host": "front", "name": "lab"},
            "machines": {"front": {}, "gpu1": {}, "offline": {}},
        }
        found = deploy.Probe(
            hostnames={"front": "front", "gpu1": "gpu1"},
            needs_password=[],
            errors=[],
            deployed={"front": ["monitor"], "gpu1": ["machine"]},
        )
        with (
            patch.object(deploy, "probe", return_value=found) as probe,
            patch.object(deploy, "probe_monitor_gpus", return_value=({"front": {}, "gpu1": {}}, [])) as gpu_probe,
            patch.object(deploy, "read_monitor_baseline", return_value=(None, None)),
        ):
            self.assertEqual(deploy.deploy_monitor(config, None, False, {}, True, deploy.Only("node", "gpu1"), None), 1)
            probe.assert_called_once_with(["front", "gpu1"], None)
            gpu_probe.assert_called_once_with(["front", "gpu1"], None, {})


if __name__ == "__main__":
    unittest.main()
