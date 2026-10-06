"""Check the provisioned Grafana dashboards before deployment: identifiers, data sources, and queries that
work for any cluster (no machine names), plus the content of the overview and queue dashboards."""

import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "src" / "nanohpc" / "files" / "grafana"
NAMES = ["history", "machines", "monitor-history", "overview", "queue-history", "queue", "usage"]


def load(name: str) -> dict[str, Any]:
    """Return one dashboard's JSON."""
    return json.loads((DASHBOARDS / f"{name}.json").read_text())


def expressions(dashboard: dict[str, Any]) -> list[str]:
    """Return every PromQL expression in a dashboard's panels."""
    return [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", []) if "expr" in target]


def datasource_uids(value: Any) -> set[str]:
    """Return every data source uid named anywhere in a dashboard."""
    found: set[str] = set()
    if isinstance(value, dict):
        if isinstance(value.get("datasource"), dict) and "uid" in value["datasource"]:
            found.add(value["datasource"]["uid"])
        for item in value.values():
            found |= datasource_uids(item)
    elif isinstance(value, list):
        for item in value:
            found |= datasource_uids(item)
    return found


class DashboardTests(unittest.TestCase):
    """Every dashboard is generic and uses only the cluster's two Prometheus instances."""

    def test_identifiers_and_data_sources(self) -> None:
        self.assertEqual(sorted(path.stem for path in DASHBOARDS.glob("*.json")), sorted(NAMES))
        for name in NAMES:
            with self.subTest(name):
                dashboard = load(name)
                self.assertEqual(dashboard["uid"], f"nanohpc-{'history' if name == 'monitor-history' else name}")
                self.assertLessEqual(datasource_uids(dashboard), {"cluster-detail", "cluster-history"})
        self.assertEqual(datasource_uids(load("history")), {"cluster-history"})

    def test_queries_name_no_machine(self) -> None:
        """Queries select machines by pattern or variable, never by a fixed name, so any cluster works."""
        for name in NAMES:
            for expression in expressions(load(name)):
                with self.subTest(name=name, expression=expression):
                    self.assertNotRegex(expression, r'machine="[a-z]')
                    self.assertNotRegex(expression, r'job="node-[a-z]')

    def test_machine_queries_include_the_front_node(self) -> None:
        """The front node's exporter job is `node`, the others `node-<name>`: no query may leave it out."""
        for name in NAMES:
            for expression in expressions(load(name)):
                with self.subTest(name=name, expression=expression):
                    self.assertNotIn('job=~"node-.+"', expression)

    def test_slurm_figures_hide_when_stale(self) -> None:
        """When the status collector stops, its last figures are not shown as current."""
        for name in ["overview", "queue", "queue-history", "usage"]:
            for expression in expressions(load(name)):
                with self.subTest(name=name, expression=expression):
                    self.assertIn("and on() (time() - cluster_snapshot_timestamp_seconds < 90)", expression)

    def test_overview_panels(self) -> None:
        panels = load("overview")["panels"]
        self.assertEqual([panel["title"] for panel in panels], ["Allocated GPUs", "Jobs"])
        self.assertEqual(
            [target["expr"].split(" and ")[0] for target in panels[0]["targets"]], ["cluster_allocated_gpus"]
        )
        self.assertEqual(
            [target["expr"].split(" and ")[0] for target in panels[1]["targets"]],
            ["cluster_running_jobs", "cluster_pending_jobs"],
        )

    def test_queue_and_history_are_separate(self) -> None:
        queue, history = load("queue"), load("queue-history")
        self.assertEqual([panel["type"] for panel in queue["panels"]], ["table"])
        self.assertEqual(
            [panel["title"] for panel in history["panels"]],
            ["Allocated GPUs", "Running jobs", "Queue size", "Waiting time"],
        )
        self.assertEqual(
            [panel["targets"][0]["expr"].split(" and ")[0] for panel in history["panels"]],
            ["cluster_allocated_gpus", "cluster_running_jobs", "cluster_pending_jobs", "cluster_mean_wait_seconds_24h"],
        )
        self.assertIn('cluster_job_info{state="PENDING"}', history["panels"][3]["targets"][1]["expr"])
        self.assertEqual(
            [(panel["gridPos"]["x"], panel["gridPos"]["y"]) for panel in history["panels"]],
            [(0, 0), (12, 0), (0, 10), (12, 10)],
        )

    def test_queue_state_filter(self) -> None:
        """Running + pending by default; finished jobs from accounting can be chosen, with an End column."""
        queue = load("queue")
        state = queue["templating"]["list"][0]
        failed = "FAILED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|BOOT_FAIL|DEADLINE|PREEMPTED"
        self.assertEqual(
            [(option["text"], option["value"]) for option in state["options"]],
            [
                ("Running + pending", "RUNNING|PENDING"),
                ("Running", "RUNNING"),
                ("Pending", "PENDING"),
                ("Completed", "COMPLETED"),
                ("Failed", failed),
                ("Cancelled", "CANCELLED"),
                ("All", ".+"),
            ],
        )
        panel = queue["panels"][0]
        end = [target for target in panel["targets"] if "cluster_job_end_seconds" in target["expr"]]
        self.assertEqual(len(end), 1)
        organize = panel["transformations"][1]["options"]
        order = sorted(organize["indexByName"], key=organize["indexByName"].get)
        self.assertEqual(
            [organize["renameByName"].get(name, name) for name in order],
            [
                "Job ID",
                "User",
                "Name",
                "State",
                "GPUs",
                "CPUs",
                "RAM",
                "Elapsed",
                "Waiting",
                "End",
                "Priority",
                "Pending reason",
                "Node",
                "Partition",
            ],
        )
        self.assertEqual(queue["title"], "Running Jobs and Queue")


if __name__ == "__main__":
    unittest.main()
