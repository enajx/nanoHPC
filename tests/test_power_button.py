"""The front node keeps running after a short power-key press."""

import configparser
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "src/nanohpc/ansible/roles/power_button"


class PowerButtonTests(unittest.TestCase):
    def test_front_node_deploys_the_key_settings_and_reads_them_now(self) -> None:
        """Only the front node gets the settings, including on a normal deploy."""
        plays = yaml.safe_load((ROOT / "src/nanohpc/ansible/site.yml").read_text())
        self.assertTrue(any(play["hosts"] == "role_front" and "power_button" in play.get("roles", []) for play in plays))
        settings = configparser.ConfigParser()
        settings.optionxform = str
        settings.read_string((ROLE / "files/50-nanohpc-ignore-buttons.conf").read_text())
        self.assertEqual(
            dict(settings["Login"]),
            {
                "HandlePowerKey": "ignore",
                "HandleRebootKey": "ignore",
                "HandleSuspendKey": "ignore",
                "HandleHibernateKey": "ignore",
            },
        )
        tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        self.assertTrue(any("busctl get-property" in str(task.get("ansible.builtin.command", "")) for task in tasks))
