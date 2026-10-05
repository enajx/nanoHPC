"""Check observed health and last-hour GPU activity, including missing data, for each kind of machine."""

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

MODULE = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster_machine_status.py"
END = 10020
TIMES = list(range(END - 3600, END + 1, 60))


def load() -> ModuleType:
    """Import the same status module the snapshot collector imports from its own folder."""
    spec = importlib.util.spec_from_file_location("cluster_machine_status", MODULE)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(machine: str, name: str, value: float, labels: dict[str, str]) -> dict[str, Any]:
    """One instant reading as Prometheus returns it."""
    return {"metric": {"__name__": name, "machine": machine, **labels}, "value": [END, str(value)]}


def healthy(machine: str, units: list[str], mounts: list[str], gpus: bool) -> list[dict[str, Any]]:
    """Readings of a machine whose exporter is up, units active, and mounts writable with free space."""
    readings = [sample(machine, "up", 1, {})]
    if gpus:
        readings.append(sample(machine, "cluster_gpu_collection_timestamp_seconds", END, {}))
    for mount in mounts:
        for name, value in [("readonly", 0), ("avail_bytes", 1024), ("files_free", 100)]:
            readings.append(sample(machine, "node_filesystem_" + name, value, {"mountpoint": mount}))
    for unit in units:
        readings.append(sample(machine, "node_systemd_unit_state", 1, {"name": unit, "state": "active"}))
    return readings


COMPUTE_MOUNTS = ["/", "/home", "/scratch"]


class MachineStatusTests(unittest.TestCase):
    """Exercise the same status function used by the snapshot collector."""

    def test_measured_specs_and_stale_inventory(self) -> None:
        """Specs preserve units and distinguish failed probes from negative flags."""
        module = load()
        info = {
            "os": "Ubuntu",
            "kernel": "5.15",
            "cpu_model": "EPYC",
            "driver": "580",
            "cuda_driver": "13.0",
            "cuda_toolkits": "13.0.3",
        }
        readings = [
            sample("gpu1", "up", 1, {}),
            sample("gpu1", "cluster_machine_specs_timestamp_seconds", END - 50, {}),
            sample("gpu1", "cluster_machine_spec_info", 1, info),
            sample("gpu1", "cluster_machine_cpu_cores", 16, {}),
            sample("gpu1", "cluster_machine_cpu_threads", 32, {}),
            sample("gpu1", "node_boot_time_seconds", END - 1000, {}),
            sample("gpu1", "node_memory_MemTotal_bytes", 256 * 1024**3, {}),
            sample("gpu1", "cluster_machine_pending_updates", 3, {}),
            sample("gpu1", "cluster_machine_needs_restart", 0, {}),
            sample("gpu1", "cluster_machine_gpu_count", 1, {}),
            sample("gpu1", "cluster_machine_gpu_memory_bytes", 48 * 1024**3, {"gpu": "0", "model": "A6000"}),
        ]
        for metric, value in [("size_bytes", 1000), ("free_bytes", 400), ("avail_bytes", 350)]:
            readings.append(
                sample("gpu1", "node_filesystem_" + metric, value, {"mountpoint": "/scratch", "fstype": "ext4"})
            )
        units = ["slurmd.service"]

        def specs(state: str) -> dict[str, Any]:
            return module.machine_status("gpu1", "Compute", state, 1, units, COMPUTE_MOUNTS, readings, [], END)["specs"]

        result = specs("IDLE")
        self.assertEqual(result["cpu_cores"], 16)
        self.assertEqual(result["gpus"][0]["memory_bytes"], 48 * 1024**3)
        self.assertEqual(result["disks"][0]["used_bytes"], 600)
        self.assertEqual(result["uptime_seconds"], 1000)
        self.assertEqual(result["pending_updates"], 3)
        self.assertFalse(result["needs_restart"])
        readings[1]["value"][1] = str(END - 1000)
        result = specs("IDLE")
        self.assertIsNone(result["pending_updates"])
        self.assertIsNone(result["needs_restart"])
        self.assertIsNone(result["cpu_cores"])
        readings[0]["value"][1] = "0"
        self.assertIsNone(specs("DOWN")["uptime_seconds"])

    def test_health_and_usage_states(self) -> None:
        """Offline, Warning, Unknown, and Healthy, and Idle, Active, and Full GPU use on a GPU machine."""
        module = load()
        units = ["slurmd.service"]
        health = healthy("gpu1", units, COMPUTE_MOUNTS, True)
        history = [
            {"metric": {"machine": "gpu1", "uuid": str(gpu)}, "values": [[t, "0"] for t in TIMES]} for gpu in range(4)
        ]

        def status(state: str) -> dict[str, Any]:
            return module.machine_status("gpu1", "Compute", state, 4, units, COMPUTE_MOUNTS, health, history, END)

        for usage, active_minutes in [("Idle", 0), ("Active", 30), ("Full", 31)]:
            for series in history:
                series["values"] = [[t, "10" if index < active_minutes else "0"] for index, t in enumerate(TIMES)]
            result = status("IDLE")
            self.assertEqual(result["health"], "Healthy")
            self.assertEqual(result["gpu_usage"], usage)
        history[0]["values"].pop()
        self.assertEqual(status("IDLE")["gpu_usage"], "Unknown")
        self.assertEqual(status("DOWN")["health"], "Warning")
        health[0]["value"][1] = "0"
        self.assertEqual(status("DOWN+NOT_RESPONDING")["health"], "Offline")
        self.assertEqual(
            module.machine_status("gpu1", "Compute", "IDLE", 4, units, COMPUTE_MOUNTS, [], [], END)["health"],
            "Unknown",
        )
        front = module.machine_status("front", "Front node", "", 0, ["slurmctld.service"], ["/"], [], [], END)
        self.assertEqual(front["gpu_usage"], "Not applicable")
        self.assertNotIn("specs", front)
        health[0]["value"][1] = "1"
        health[1]["value"][1] = str(END - 300)
        self.assertEqual(status("IDLE")["health"], "Unknown")
        health[1]["value"][1] = str(END)
        health[2]["value"][1] = "1"
        self.assertEqual(status("IDLE")["health"], "Warning")

    def test_cpu_only_machine(self) -> None:
        """A compute machine without GPUs can be Healthy, its GPU use is Not applicable, and Slurm is checked."""
        module = load()
        units = ["slurmd.service"]
        health = healthy("cpu1", units, COMPUTE_MOUNTS, False)
        result = module.machine_status("cpu1", "Compute", "IDLE", 0, units, COMPUTE_MOUNTS, health, [], END)
        self.assertEqual(
            (result["role"], result["health"], result["gpu_usage"]), ("Compute", "Healthy", "Not applicable")
        )
        self.assertEqual(result["health_details"], [])
        self.assertIn("specs", result)
        drained = module.machine_status("cpu1", "Compute", "IDLE+DRAIN", 0, units, COMPUTE_MOUNTS, health, [], END)
        self.assertEqual(drained["health"], "Warning")
        self.assertEqual(drained["health_details"], ["Slurm state: IDLE+DRAIN"])

    def test_storage_machine_without_slurm(self) -> None:
        """A storage machine is checked only on its own units and mounts, never on a Slurm state."""
        module = load()
        units = ["nfs-server.service"]
        mounts = ["/", "/home"]
        health = healthy("store", units, mounts, False)
        result = module.machine_status("store", "Storage", "", 0, units, mounts, health, [], END)
        self.assertEqual(
            (result["role"], result["health"], result["gpu_usage"]), ("Storage", "Healthy", "Not applicable")
        )
        self.assertEqual(result["health_details"], [])
        self.assertIn("specs", result)
        health[-1]["value"][1] = "0"
        stopped = module.machine_status("store", "Storage", "", 0, units, mounts, health, [], END)
        self.assertEqual((stopped["health"], stopped["health_details"]), ("Warning", ["nfs-server.service: inactive"]))
        missing = module.machine_status("store", "Storage", "", 0, units, ["/", "/home", "/srv"], health, [], END)
        self.assertIn("/srv: missing readonly", missing["health_details"])
        self.assertFalse(any("Slurm" in detail for detail in missing["health_details"]))
        with self.assertRaises(ValueError):
            module.machine_status("store", "Backup", "", 0, units, mounts, health, [], END)

    def test_monitor_machine_health_and_gpu_inventory(self) -> None:
        """Monitor machines use exporter health and measured GPU inventory, without Slurm state."""
        module = load()
        readings = healthy("gpu1", ["node-exporter.service"], ["/"], True)
        readings.extend(
            [
                sample("gpu1", "cluster_machine_specs_timestamp_seconds", END, {}),
                sample("gpu1", "cluster_machine_gpu_count", 1, {}),
            ]
        )
        history = [{"metric": {"machine": "gpu1", "uuid": "GPU-1"}, "values": [[t, "0"] for t in TIMES]}]

        def status() -> dict[str, Any]:
            return module.monitor_machine_status("gpu1", "Machine", ["node-exporter.service"], ["/"], readings, history, END)

        self.assertEqual((status()["health"], status()["gpu_usage"], status()["total_gpus"]), ("Healthy", "Idle", 1))
        self.assertNotIn("Slurm", " ".join(status()["health_details"]))
        readings[0]["value"][1] = "0"
        self.assertEqual(status()["health"], "Offline")
        readings[0]["value"][1] = "1"
        readings[-2]["value"][1] = str(END - 1000)
        self.assertIsNone(status()["total_gpus"])
        self.assertEqual(status()["gpu_usage"], "Unknown")
        self.assertEqual(status()["health"], "Unknown")

    def test_monitor_cpu_and_service_failure(self) -> None:
        """Fresh zero GPUs is CPU-only; an inactive required unit warns."""
        module = load()
        readings = healthy("front", ["prometheus.service"], ["/"], False)
        readings.extend(
            [
                sample("front", "cluster_machine_specs_timestamp_seconds", END, {}),
                sample("front", "cluster_machine_gpu_count", 0, {}),
            ]
        )
        result = module.monitor_machine_status("front", "Monitor", ["prometheus.service"], ["/"], readings, [], END)
        self.assertEqual((result["health"], result["gpu_usage"], result["total_gpus"]), ("Healthy", "Not applicable", 0))
        readings[4]["value"][1] = "0"
        self.assertEqual(
            module.monitor_machine_status("front", "Monitor", ["prometheus.service"], ["/"], readings, [], END)["health"],
            "Warning",
        )


if __name__ == "__main__":
    unittest.main()
