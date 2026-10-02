"""Classify machines from private Prometheus measurements and Slurm state.

Imported by cluster-monitor-snapshot from the same folder (standard library only).

Each machine has a role ("Front node", "Compute", or "Storage") and is checked on:
- its node_exporter answering Prometheus (`up`);
- each of its required systemd units being active;
- each of its required mount points: not read-only, with free space and free inodes;
- on compute machines only, the Slurm node state (not down, drained, failed, or not responding);
- on machines with GPUs, fresh GPU readings.

Health: Offline when the exporter is down and Slurm says the node is not responding; Warning when a check
failed; Unknown when a reading is missing or stale; Healthy otherwise.

GPU use over the last hour (one-minute samples, machines with GPUs only): Idle when no GPU was busy, Full when
all GPUs were busy in more than half of the samples, Active otherwise, and Unknown when samples are missing.
Machines without GPUs (front node, storage machines, CPU-only compute machines) report "Not applicable".
"""

import json
import math
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

ROLES = ("Front node", "Compute", "Storage")
HEALTH_QUERY = '{__name__=~"up|node_systemd_unit_state|node_filesystem_(readonly|avail_bytes|free_bytes|size_bytes|files_free)|node_boot_time_seconds|node_memory_MemTotal_bytes|cluster_machine_.*|cluster_gpu_collection_timestamp_seconds",job=~"node|node-.+"}'
GPU_QUERY = "cluster_gpu_utilization_percent and on(machine) (time() - cluster_gpu_collection_timestamp_seconds < 90)"


def query(base: str, endpoint: str, parameters: dict[str, str | int]) -> list[dict[str, Any]]:
    """Use fixed internal queries; reject errors instead of publishing invented health."""
    with urlopen(base.rstrip("/") + "/api/v1/" + endpoint + "?" + urlencode(parameters), timeout=8) as response:
        payload = json.load(response)
    if payload.get("status") != "success" or payload.get("warnings"):
        raise ValueError("Machine measurements are unavailable or incomplete")
    return payload["data"]["result"]


def measurements(base: str, end: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch current health and a full hour on a fixed one-minute sample grid."""
    return (
        query(base, "query", {"query": HEALTH_QUERY, "time": end}),
        query(base, "query_range", {"query": GPU_QUERY, "start": end - 3600, "end": end, "step": 60}),
    )


def machine_specs(current: list[dict[str, Any]], end: int) -> dict[str, Any]:
    """Join fixed exporter readings; stale inventory cannot claim current update status."""

    def value(metric: str, labels: dict[str, str]) -> float | None:
        values = [
            float(row["value"][1])
            for row in current
            if row["metric"]["__name__"] == metric and all(row["metric"].get(key) == val for key, val in labels.items())
        ]
        return values[0] if len(values) == 1 and math.isfinite(values[0]) else None

    up = value("up", {}) == 1
    stamp = value("cluster_machine_specs_timestamp_seconds", {})
    fresh = up and stamp is not None and 0 <= end - stamp < 900
    rows = [row["metric"] for row in current if row["metric"]["__name__"] == "cluster_machine_spec_info"]
    info = rows[0] if fresh and len(rows) == 1 else {}

    def measured(metric: str) -> float | None:
        return value(metric, {}) if fresh else None

    boot = value("node_boot_time_seconds", {}) if up else None
    restart = measured("cluster_machine_needs_restart")
    disks = []
    skipped = ("tmpfs", "devtmpfs", "overlay", "squashfs", "autofs", "nsfs", "tracefs")
    mounts = {
        row["metric"]["mountpoint"]
        for row in current
        if row["metric"]["__name__"] == "node_filesystem_size_bytes" and row["metric"].get("fstype") not in skipped
    }
    for mount in sorted(mounts) if up else []:
        total, free, available = [
            value("node_filesystem_" + metric, {"mountpoint": mount})
            for metric in ("size_bytes", "free_bytes", "avail_bytes")
        ]
        if total is not None and total > 0:
            disks.append(
                {
                    "mount": mount,
                    "total_bytes": total,
                    "used_bytes": total - free if free is not None else None,
                    "available_bytes": available,
                }
            )
    gpus = [
        {"index": row["metric"]["gpu"], "model": row["metric"]["model"], "memory_bytes": float(row["value"][1])}
        for row in current
        if row["metric"]["__name__"] == "cluster_machine_gpu_memory_bytes" and fresh
    ]
    gpus.sort(key=lambda gpu: int(gpu["index"]))
    return {
        "collected_at": stamp if fresh else None,
        **{key: info.get(key) for key in ("os", "kernel", "cpu_model", "driver", "cuda_driver")},
        "cuda_toolkits": info["cuda_toolkits"].split(",") if info.get("cuda_toolkits") else [] if info else None,
        "cpu_cores": measured("cluster_machine_cpu_cores"),
        "cpu_threads": measured("cluster_machine_cpu_threads"),
        "gpu_count": measured("cluster_machine_gpu_count"),
        "gpus": gpus,
        "ram_bytes": value("node_memory_MemTotal_bytes", {}) if up else None,
        "uptime_seconds": end - boot if boot is not None and boot <= end else None,
        "pending_updates": measured("cluster_machine_pending_updates"),
        "updates_checked_at": measured("cluster_machine_updates_checked_timestamp_seconds"),
        "needs_restart": bool(restart) if restart in (0, 1) else None,
        "disks": disks,
    }


def machine_status(
    name: str,
    role: str,
    slurm_state: str,
    gpu_count: int,
    units: list[str],
    mounts: list[str],
    health: list[dict[str, Any]],
    history: list[dict[str, Any]],
    end: int,
) -> dict[str, Any]:
    """Report only checked health; Full requires all GPUs busy for a majority of samples.

    name: the machine's name, as in Prometheus's `machine` label (and Slurm's node name for compute machines).
    role: "Front node", "Compute", or "Storage". Only compute machines are checked on `slurm_state`.
    slurm_state: the Slurm node state (for example "MIXED" or "IDLE+DRAIN"); "" for other machines.
    gpu_count: the GPUs Slurm has configured on the machine; 0 for machines without GPUs.
    units: the systemd units that must be active (for example "slurmd.service").
    mounts: the mount points that must be present and writable, with free space and inodes.
    health: instant readings from HEALTH_QUERY; history: GPU_QUERY over the hour ending at `end`.
    """
    if role not in ROLES:
        raise ValueError(f"Unknown machine role {role!r} for {name}")
    compute = role == "Compute"
    current = [row for row in health if row["metric"].get("machine") == name]

    def value(metric: str, mount: str | None) -> float | None:
        values = [
            float(row["value"][1])
            for row in current
            if row["metric"]["__name__"] == metric and (mount is None or row["metric"].get("mountpoint") == mount)
        ]
        return values[0] if len(values) == 1 and math.isfinite(values[0]) else None

    missing: list[str] = []
    problems: list[str] = []
    up = value("up", None)
    if up is None:
        missing.append("Host measurements unavailable")
    elif up != 1:
        problems.append("Host monitoring endpoint is not responding")
    for service in units:
        states = [
            float(row["value"][1])
            for row in current
            if row["metric"]["__name__"] == "node_systemd_unit_state"
            and row["metric"].get("name") == service
            and row["metric"].get("state") == "active"
        ]
        if len(states) != 1 or not math.isfinite(states[0]):
            missing.append(service + ": status unavailable")
        elif states[0] != 1:
            problems.append(service + ": inactive")
    responding = "NOT_RESPONDING" not in slurm_state and "NO_RESPOND" not in slurm_state
    if compute and (
        not responding
        or slurm_state.split("+")[0] not in ("IDLE", "MIXED", "ALLOCATED", "COMPLETING")
        or any(flag in slurm_state for flag in ("DRAIN", "FAIL", "INVALID"))
    ):
        problems.append("Slurm state: " + slurm_state)
    for mount in mounts:
        for metric in ["readonly", "avail_bytes", "files_free"]:
            observed = value("node_filesystem_" + metric, mount)
            if observed is None:
                missing.append(mount + ": missing " + metric)
            elif (metric == "readonly" and observed != 0) or (metric != "readonly" and observed <= 0):
                problems.append(mount + ": " + metric)
    gpu_usage = "Unknown" if gpu_count else "Not applicable"
    if gpu_count:
        timestamp = value("cluster_gpu_collection_timestamp_seconds", None)
        gpu_fresh = timestamp is not None and 0 <= end - timestamp < 90
        if not gpu_fresh:
            missing.append("GPU readings unavailable or stale")
        series = [row for row in history if row["metric"].get("machine") == name]
        expected = list(range(end - 3600, end + 1, 60))
        samples = [{int(t): float(v) for t, v in row["values"]} for row in series]
        unique_gpus = {row["metric"].get("uuid") for row in series}
        if len(samples) != gpu_count or not all(end in gpu and math.isfinite(gpu[end]) for gpu in samples):
            missing.append("Current GPU measurements incomplete")
        complete = (
            len(samples) == gpu_count
            and len(unique_gpus) == gpu_count
            and all(all(t in gpu and math.isfinite(gpu[t]) and 0 <= gpu[t] <= 100 for t in expected) for gpu in samples)
        )
        if gpu_fresh and complete:
            busy = [sum(gpu[t] > 0 for gpu in samples) for t in expected]
            full = sum(count == gpu_count for count in busy) > len(busy) / 2
            gpu_usage = "Idle" if max(busy) == 0 else "Full" if full else "Active"
        elif not series or len(series) != gpu_count:
            missing.append("GPU measurements incomplete")
    status = "Offline" if up == 0 and not responding else "Warning" if problems else "Unknown" if missing else "Healthy"
    return {
        "name": name,
        "role": role,
        "health": status,
        "health_details": problems + missing,
        "gpu_usage": gpu_usage,
        **({"specs": machine_specs(current, end)} if role != "Front node" else {}),
    }
