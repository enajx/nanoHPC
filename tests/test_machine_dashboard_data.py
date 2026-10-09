"""Evaluate the Machines dashboard's actual PromQL through promtool.

The readings include a machine outage, a CPU-counter reset on restart, a stale
GPU collector, and a gap when Prometheus itself receives no samples.
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


def input_series() -> list[dict[str, str]]:
    """Provide node and GPU readings from the front and one compute machine."""
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


def queries() -> list[tuple[str, str, str]]:
    """Read every panel's query, with the GPU-lines choice filled as Grafana does."""
    dashboard = json.loads(DASHBOARD.read_text())
    return [
        (
            panel["title"],
            target["refId"],
            target["expr"].replace("${gpu_group:raw}", "1" if target["refId"] == "A" else "0"),
        )
        for panel in dashboard["panels"]
        for target in panel["targets"]
    ]


@unittest.skipUnless(os.environ.get("PROMTOOL") or shutil.which("promtool"), "promtool is unavailable")
class MachineDashboardDataTest(unittest.TestCase):
    """The dashboard draws only readings from a running, recently scraped machine."""

    def test_no_invented_points(self) -> None:
        """A failed scrape, restart, stale GPU file, or Prometheus gap breaks its lines."""
        checks = []
        dashboard_queries = queries()
        self.assertEqual(
            {panel for panel, _, _ in dashboard_queries},
            {
                "CPU in use",
                "Available memory",
                "Available filesystem space",
                *GPU_PANELS,
            },
        )
        self.assertEqual(len(dashboard_queries), 11)
        for panel, ref, expr in dashboard_queries:
            for at, machines in (
                (300, ("front", "gpu1")),
                (660, ("gpu1",)),
                (750, ("gpu1",)),
                (825, ("gpu1",) if panel == "CPU in use" else ("front", "gpu1")),
                (900, ("front", "gpu1")),
                (GPU_STALE, ("gpu1",) if panel in GPU_PANELS else ("front", "gpu1")),
                (1080, ("front", "gpu1")),
                (1110, ("gpu1",) if panel == "CPU in use" else ("front", "gpu1")),
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
                        "exp_samples": [expected(panel, ref, machine) for machine in machines],
                    }
                )
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


if __name__ == "__main__":
    unittest.main()
