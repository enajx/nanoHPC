"""The shipped Grafana dashboards display the configured cluster name."""

import json
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "src/nanohpc/files/grafana"


class GrafanaNameTest(unittest.TestCase):
    """Both deploy modes keep stable dashboard links while naming visible pages."""

    def test_dashboard_titles_use_cluster_name(self) -> None:
        """Render each deployed dashboard for two different cluster names."""
        renderer = Environment(variable_start_string="[%", variable_end_string="%]")
        dashboards = {
            "overview.json": "nanohpc-overview",
            "machines.json": "nanohpc-machines",
            "queue.json": "nanohpc-queue",
            "queue-history.json": "nanohpc-queue-history",
            "usage.json": "nanohpc-usage",
            "history.json": "nanohpc-history",
            "monitor-history.json": "nanohpc-history",
        }
        for cluster_name in ("labcluster", "new-cluster"):
            for filename, uid in dashboards.items():
                with self.subTest(cluster_name=cluster_name, dashboard=filename):
                    source = (DASHBOARDS / filename).read_text()
                    rendered = renderer.from_string(source).render(nanohpc={"cluster_name": cluster_name, "users": []})
                    dashboard = json.loads(rendered)
                    self.assertTrue(dashboard["title"].startswith(f"{cluster_name}: "), dashboard["title"])
                    self.assertEqual(dashboard["uid"], uid)
                    if filename in ("usage.json", "history.json", "monitor-history.json"):
                        self.assertIn("{{", rendered)

    def test_provisioned_folder_uses_cluster_name(self) -> None:
        """The folder shown by Grafana follows the cluster setting."""
        role = ROOT / "src/nanohpc/ansible/roles/grafana/tasks/main.yml"
        tasks = yaml.safe_load(role.read_text())
        provider_task = next(task for task in tasks if task.get("name") == "Provision the dashboards folder")
        content = provider_task["ansible.builtin.copy"]["content"]
        for cluster_name in ("labcluster", "new-cluster"):
            with self.subTest(cluster_name=cluster_name):
                rendered = Environment().from_string(content).render(nanohpc={"cluster_name": cluster_name})
                provider = yaml.safe_load(rendered)["providers"][0]
                self.assertEqual(provider["folder"], cluster_name)
                self.assertEqual(provider["folderUid"], "nanohpc-cluster")
