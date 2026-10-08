"""node_exporter includes the user check units in its systemd collector."""

import re
import unittest
from pathlib import Path

import yaml
from jinja2 import Template

TASKS = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/node_metrics/tasks/main.yml"


class NodeMetricsUserCheckTest(unittest.TestCase):
    """Failed scanner and channel notifier units are visible as systemd metrics."""

    def test_slurm_mode_includes_scanner_and_notifier_units(self) -> None:
        tasks = yaml.safe_load(TASKS.read_text())
        service = next(task for task in tasks if task["name"] == "Install the node_exporter service")
        content = service["ansible.builtin.copy"]["content"]
        for mode, expected in (("slurm", True), ("monitor", False)):
            unit = Template(content).render(
                node_exporter_dir="/opt/node_exporter",
                on_front=True,
                inventory_hostname="front",
                nanohpc={"mode": mode, "machines": {}, "metrics": {"front_address": "127.0.0.1"}},
            )
            for name in ("nanohpc-user-check", "nanohpc-user-check-notify"):
                self.assertEqual(name in unit, expected, (mode, name, unit))
            included = unit.split("--collector.systemd.unit-include=(", 1)[1].split(r")\.", 1)[0]
            pattern = re.compile(rf"(?:{included})\.(?:service|timer)")
            for name in ("nanohpc-user-check", "nanohpc-user-check-notify"):
                for kind in ("service", "timer"):
                    self.assertEqual(pattern.fullmatch(f"{name}.{kind}") is not None, expected)


if __name__ == "__main__":
    unittest.main()
