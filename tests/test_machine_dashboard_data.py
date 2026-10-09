"""Evaluate the Machines dashboard's actual PromQL through promtool.

The readings include a machine outage, a CPU-counter reset on restart, stale
GPU and power collectors, and a gap when Prometheus itself receives no samples.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "src/nanohpc/files/grafana/machines.json"
STEP = 30
END = 1500
MACHINE_DOWN = range(630, 781, STEP)
PROMETHEUS_DOWN = range(1200, 1381, STEP)
RESTART = 810
SECOND_RESET = 1110
GPU_STALE = 1020
POWER_STALE = 1080
POWER_ALL_STALE = 1110
POWER_WATTS = {"front": 200, "gpu1": 400}
GPU_PANELS = {
    "GPU utilization": ("utilization_percent", 40),
    "GPU memory used": ("memory_used_bytes", 10),
    "GPU temperature": ("temperature_celsius", 60),
    "GPU power": ("power_watts", 200),
}


def times() -> list[int]:
    """Return every scrape time in the test history."""
    return list(range(0, END + 1, STEP))


def reading(machine: str, value: Callable[[int], int], counter: bool) -> str:
    """Return Prometheus test values, including missing and stale readings."""
    values = []
    for at in times():
        if machine == "front" and at in PROMETHEUS_DOWN:
            values.append("_")
        elif machine == "front" and at in MACHINE_DOWN:
            values.append("stale" if at == MACHINE_DOWN.start else "_")
        elif counter and machine == "front" and at >= SECOND_RESET:
            values.append(str(value(at - SECOND_RESET)))
        elif counter and machine == "front" and at >= RESTART:
            values.append(str(value(at - RESTART)))
        else:
            values.append(str(value(at)))
    return " ".join(values)


def up(machine: str) -> str:
    """Report failed scrapes during the outage and no samples during the Prometheus gap."""
    return " ".join(
        "_"
        if machine == "front" and at in PROMETHEUS_DOWN
        else "0"
        if machine == "front" and at in MACHINE_DOWN
        else "1"
        for at in times()
    )


def constant(number: int) -> Callable[[int], int]:
    """Use one measurement value at each scrape time."""

    def value_at(at: int) -> int:
        return number

    return value_at


def gpu_timestamp(machine: str) -> Callable[[int], int]:
    """Leave the front GPU collector's file stale at one scrape."""

    def value_at(at: int) -> int:
        return at - (80 if machine == "front" and at == GPU_STALE else 10)

    return value_at


def power_timestamp(machine: str) -> Callable[[int], int]:
    """Make one power collector stale, then both stale before recovery."""

    def value_at(at: int) -> int:
        stale = (at == POWER_STALE and machine == "front") or at == POWER_ALL_STALE
        return at - (80 if stale else 10)

    return value_at


def input_series() -> list[dict[str, str]]:
    """Provide node, GPU, and wall-power readings from two machines."""
    rows = []
    for machine in ("front", "gpu1"):
        job = "node" if machine == "front" else "node-gpu1"
        labels = f'job="{job}",machine="{machine}"'
        rows.append({"series": f"up{{{labels}}}", "values": up(machine)})
        for cpu in ("0", "1"):
            rows.append(
                {
                    "series": f'node_cpu_seconds_total{{{labels},cpu="{cpu}",mode="idle"}}',
                    "values": reading(machine, lambda at: 5 + at * 15 // STEP, True),
                }
            )
        rows.append(
            {"series": f"node_memory_MemAvailable_bytes{{{labels}}}", "values": reading(machine, lambda at: 100, False)}
        )
        rows.append(
            {
                "series": f'node_filesystem_avail_bytes{{{labels},fstype="ext4",mountpoint="/"}}',
                "values": reading(machine, lambda at: 50, False),
            }
        )
        for metric, value in GPU_PANELS.values():
            rows.append(
                {
                    "series": f'cluster_gpu_{metric}{{{labels},gpu="0"}}',
                    "values": reading(machine, constant(value), False),
                }
            )
        rows.append(
            {
                "series": f"cluster_gpu_collection_timestamp_seconds{{{labels}}}",
                "values": reading(machine, gpu_timestamp(machine), False),
            }
        )
        rows.append(
            {
                "series": f'cluster_wall_power_watts{{{labels},source="power_supplies"}}',
                "values": reading(machine, constant(POWER_WATTS[machine]), False),
            }
        )
        rows.append(
            {
                "series": f"cluster_power_collection_timestamp_seconds{{{labels}}}",
                "values": reading(machine, power_timestamp(machine), False),
            }
        )
    return rows


def expected(panel: str, ref: str, machine: str) -> dict[str, str | int]:
    """Describe one real reading returned by a dashboard query."""
    job = "node" if machine == "front" else "node-gpu1"
    labels = f'job="{job}",machine="{machine}"'
    if panel == "CPU in use":
        return {"labels": f'{{machine="{machine}"}}', "value": 50}
    if panel == "Available memory":
        return {"labels": f"node_memory_MemAvailable_bytes{{{labels}}}", "value": 100}
    if panel == "Available filesystem space":
        return {"labels": f'node_filesystem_avail_bytes{{{labels},fstype="ext4",mountpoint="/"}}', "value": 50}
    metric, value = GPU_PANELS[panel]
    if ref == "A":
        return {"labels": f'cluster_gpu_{metric}{{{labels},gpu="0"}}', "value": value}
    return {"labels": f'{{machine="{machine}"}}', "value": value}


def samples(panel: str, ref: str, machines: tuple[str, ...]) -> list[dict[str, str | int]]:
    """Return a total power point only when at least one machine reports."""
    if panel == "Total power":
        return [{"labels": "{}", "value": sum(POWER_WATTS[machine] for machine in machines)}] if machines else []
    return [expected(panel, ref, machine) for machine in machines]


def queries(machine: str) -> list[tuple[str, str, str]]:
    """Read every panel's query, with Grafana's Machine and GPU choices filled in."""
    dashboard = json.loads(DASHBOARD.read_text())
    return [
        (
            panel["title"],
            target["refId"],
            target["expr"]
            .replace("${gpu_group:raw}", "1" if target["refId"] == "A" else "0")
            .replace("$machine", machine),
        )
        for panel in dashboard["panels"]
        for target in panel["targets"]
    ]


@unittest.skipUnless(os.environ.get("PROMTOOL") or shutil.which("promtool"), "promtool is unavailable")
class MachineDashboardDataTest(unittest.TestCase):
    """The dashboard draws only readings from a running, recently scraped machine."""

    def test_machine_filter(self) -> None:
        """One, several, and All keep only the selected machines in every panel."""
        dashboard = json.loads(DASHBOARD.read_text())
        machine = dashboard["templating"]["list"][1]
        self.assertEqual(machine["name"], "machine")
        self.assertEqual(machine["type"], "query")
        self.assertTrue(machine["multi"])
        self.assertTrue(machine["includeAll"])
        self.assertEqual(machine["allValue"], ".+")
        self.assertEqual(machine["current"]["value"], ["$__all"])
        self.assertIn('label_values(up{job=~"node|node-.+"}, machine)', machine["definition"])
        checks = []
        for choice, kept in (
            ("front", ("front",)),
            ("gpu1", ("gpu1",)),
            ("(front|gpu1)", ("front", "gpu1")),
            (".+", ("front", "gpu1")),
        ):
            for panel, ref, expr in queries(choice):
                checks.append(
                    {
                        "expr": expr,
                        "eval_time": "300s",
                        "exp_samples": samples(panel, ref, kept),
                    }
                )
        self.run_promtool(checks)

    def test_power_panel_layout(self) -> None:
        """Total power shares the bottom row with available filesystem space."""
        dashboard = json.loads(DASHBOARD.read_text())
        panels = {panel["title"]: panel for panel in dashboard["panels"]}
        self.assertEqual(panels["Available filesystem space"]["gridPos"], {"x": 0, "y": 24, "w": 12, "h": 8})
        self.assertEqual(panels["Total power"]["gridPos"], {"x": 12, "y": 24, "w": 12, "h": 8})

    def run_promtool(self, checks: list[dict[str, object]]) -> None:
        """Evaluate dashboard queries against the fixture with the real Prometheus engine."""
        config = {
            "rule_files": [],
            "evaluation_interval": "30s",
            "tests": [
                {
                    "interval": f"{STEP}s",
                    "input_series": input_series(),
                    "promql_expr_test": checks,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "machines_test.yml"
            path.write_text(yaml.safe_dump(config, sort_keys=False))
            promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
            assert promtool is not None
            result = subprocess.run(
                [promtool, "test", "rules", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_invented_points(self) -> None:
        """A failed scrape, restart, stale GPU file, or Prometheus gap breaks its lines."""
        checks = []
        dashboard_queries = queries(".+")
        self.assertEqual(
            {panel for panel, _, _ in dashboard_queries},
            {
                "CPU in use",
                "Available memory",
                "Available filesystem space",
                "Total power",
                *GPU_PANELS,
            },
        )
        self.assertEqual(len(dashboard_queries), 12)
        for panel, ref, expr in dashboard_queries:
            for at, machines in (
                (300, ("front", "gpu1")),
                (660, ("gpu1",)),
                (750, ("gpu1",)),
                (825, ("gpu1",) if panel == "CPU in use" else ("front", "gpu1")),
                (900, ("front", "gpu1")),
                (GPU_STALE, ("gpu1",) if panel in GPU_PANELS else ("front", "gpu1")),
                (POWER_STALE, ("gpu1",) if panel == "Total power" else ("front", "gpu1")),
                (
                    POWER_ALL_STALE,
                    () if panel == "Total power" else ("gpu1",) if panel == "CPU in use" else ("front", "gpu1"),
                ),
                (1140, ("gpu1",) if panel == "CPU in use" else ("front", "gpu1")),
                (1170, ("front", "gpu1")),
                (1230, ("gpu1",)),
                (1290, ("gpu1",)),
                (1425, ("gpu1",) if panel == "CPU in use" else ("front", "gpu1")),
                (1470, ("front", "gpu1")),
            ):
                checks.append(
                    {
                        "expr": expr,
                        "eval_time": f"{at}s",
                        "exp_samples": samples(panel, ref, machines),
                    }
                )
        self.run_promtool(checks)


if __name__ == "__main__":
    unittest.main()
