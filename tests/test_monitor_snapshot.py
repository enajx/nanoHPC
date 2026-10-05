"""Exercise the snapshot collector command with labelled fake Slurm commands and a fake Prometheus."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

COLLECTOR = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-monitor-snapshot"

# Fake Slurm: one script answering as scontrol, squeue, sshare, sprio, sacct, and sacctmgr.
# gpu1 has 4 GPUs, cpu1 has none; the storage machine is not a Slurm node.
FAKE_SLURM = """import os,sys,json
from datetime import datetime,timedelta
from pathlib import Path
name=Path(sys.argv[0]).name
args=' '.join(sys.argv[1:])
now=datetime.now().replace(microsecond=0)
hour=int((now-timedelta(hours=1)).timestamp())
half=int((now-timedelta(minutes=30)).timestamp())
old_start=int((now-timedelta(days=8,hours=1)).timestamp())
old_end=int((now-timedelta(days=8)).timestamp())
year_start=int((now-timedelta(days=40,hours=1)).timestamp())
year_end=int((now-timedelta(days=40)).timestamp())
if os.environ.get('TEST_FAIL') == name: sys.exit(2)
if name=='sacct' and os.environ.get('SLURM_TIME_FORMAT')!='%s': sys.exit(4)
if name=='scontrol' and 'nodes' in args:
 allocated=os.environ.get('TEST_ALLOCATED_GPUS','2')
 print('NodeName=gpu1 State=MIXED Partitions=interactive,main CfgTRES=cpu=32,gres/gpu=4,gres/gpu:a6000=4 AllocTRES=cpu=8,gres/gpu=' + allocated + ',gres/gpu:a6000=' + allocated)
 print('NodeName=cpu1 State=IDLE Partitions=main CfgTRES=cpu=16,mem=64G AllocTRES=')
elif name=='scontrol' and 'partition' in args:
 print('PartitionName=interactive Default=NO Nodes=gpu1 MaxTime=08:00:00 DefMemPerCPU=8192 JobDefaults=DefCpuPerGPU=4 TRESBillingWeights=CPU=0,Mem=0,GRES/gpu=1')
 print('PartitionName=main Default=YES Nodes=gpu1,cpu1 MaxTime=1-00:00:00 DefMemPerCPU=8192 JobDefaults=DefCpuPerGPU=4 TRESBillingWeights=CPU=0,Mem=0,GRES/gpu=1')
elif name=='scontrol':
 print('PriorityWeightFairshare = 10000\\nPriorityWeightAge = 1000\\nPriorityDecayHalfLife = 7-00:00:00\\nPriorityMaxAge = 7-00:00:00')
elif name=='squeue' and '--json' in args:
 print(json.dumps({'jobs':[{'job_id':1,'name':'test \\"job\\"','user_name':'alice','job_state':['RUNNING'],'state_reason':'None','nodes':'gpu1','tres_req_str':'cpu=4,mem=32G,node=1,gres/gpu=1','start_time':{'number':100,'set':True},'submit_time':{'number':90,'set':True},'priority':{'number':2500,'set':True}}, {'job_id':2,'name':'cpu\\tjob','user_name':'bob','job_state':['PENDING'],'state_reason':'Dependency','tres_req_str':'cpu=2,mem=128M,node=1','start_time':{'number':9999999999,'set':True},'submit_time':{'number':90,'set':True},'priority':{'number':100,'set':True}}]}))
elif name=='squeue': print('1|alice|RUNNING\\n2|bob|PENDING')
elif name=='sshare': print('|5000|\\nroot|0|1\\nalice|3600|0.25\\nbob|1800|0.5')
elif name=='sprio': print('2|bob|5010|5000|10')
elif name=='sacct' and 'JobName' in args:
 if '--starttime=now-7days' not in args or '--state=CD,F,TO,OOM,NF,BF,DL,PR,CA' not in args: sys.exit(5)
 print(f'1|alice|COMPLETED|gpu1|main|cpu=4,mem=32G,node=1,gres/gpu=1|{hour-60}|{hour}|{half}|race\\n7|alice|COMPLETED|gpu1|main|billing=1,cpu=4,gres/gpu=1,mem=32G,node=1|{hour-60}|{hour}|{hour+3600}|train|a\\n8|bob|TIMEOUT|cpu1|interactive|cpu=2,mem=128M,node=1|{hour}|{hour}|{half}|she\\nll\\n9_[1-3]|alice|CANCELLED by 1000|None assigned|main|cpu=4,mem=16G,node=1|{hour-600}|None|{hour}|queued\\n10|alice|COMPLETED|gpu1|main|cpu=4,mem=16G,node=1|{old_start-60}|{old_start}|{old_end}|old')
elif name=='sacct' and 'Submit' in args:
 print(f'1|{hour-1200}|{hour}|COMPLETED\\n2|{hour}|None|CANCELLED\\n3|{half-600}|{half}|RUNNING\\n4|{old_start-1800}|{old_start}|COMPLETED\\n5|{year_start-3600}|{year_start}|COMPLETED')
elif name=='sacct':
 if '--starttime=2025-09-01T12:00:00' not in args: sys.exit(6)
 print(f'alice|1|3600|billing=999,cpu=8,gres/gpu=2,gres/gpu:a6000=2|COMPLETED|{hour}|{int(now.timestamp())}\\nbob|2|1800|cpu=8|COMPLETED|{hour}|{int(now.timestamp())}\\nalice|3|1800|gres/gpu:a6000=1|RUNNING|{half}|Unknown\\nalice|4|3600|gres/gpu:a6000=1|COMPLETED|{old_start}|{old_end}\\nalice|5|3600|gres/gpu:a6000=1|COMPLETED|{year_start}|{year_end}')
elif name=='sacctmgr': print('normal||30|\\ninteractive|08:00:00||gres/gpu=2\\nmain|1-00:00:00||')
else: sys.exit(3)
"""

MACHINES: dict[str, dict[str, Any]] = {
    "front": {"role": "front", "units": ["slurmctld.service", "slurmdbd.service"], "mounts": ["/", "/home"]},
    "gpu1": {"role": "compute", "building": "Lab A", "units": ["slurmd.service"], "mounts": ["/", "/home", "/scratch"]},
    "cpu1": {"role": "compute", "units": ["slurmd.service"], "mounts": ["/", "/home", "/scratch"]},
    "store": {"role": "storage", "units": ["nfs-server.service"], "mounts": ["/", "/home"]},
}

MONITOR_MACHINES: dict[str, dict[str, Any]] = {
    "front": {"role": "monitor", "units": ["prometheus.service"], "mounts": ["/"]},
    "gpu1": {"role": "machine", "units": [], "mounts": ["/"]},
    "cpu1": {"role": "machine", "units": [], "mounts": ["/"]},
}


def prometheus_samples(end: int) -> list[dict[str, Any]]:
    """Readings of a cluster where every machine is up, with active units and writable mounts."""

    def sample(machine: str, name: str, value: float, labels: dict[str, str]) -> dict[str, Any]:
        return {"metric": {"__name__": name, "machine": machine, **labels}, "value": [end, str(value)]}

    readings = [sample("gpu1", "cluster_gpu_collection_timestamp_seconds", end, {})]
    for machine, values in MACHINES.items():
        readings.append(sample(machine, "up", 1, {}))
        for unit in values["units"]:
            readings.append(sample(machine, "node_systemd_unit_state", 1, {"name": unit, "state": "active"}))
        for mount in values["mounts"]:
            for name, value in [("readonly", 0), ("avail_bytes", 1024), ("files_free", 100)]:
                readings.append(sample(machine, "node_filesystem_" + name, value, {"mountpoint": mount}))
    return readings


def monitor_samples(end: int) -> list[dict[str, Any]]:
    """Monitor machines report measured inventory and only required local checks."""

    def sample(machine: str, name: str, value: float, labels: dict[str, str]) -> dict[str, Any]:
        return {"metric": {"__name__": name, "machine": machine, **labels}, "value": [end, str(value)]}

    readings = []
    for machine, values in MONITOR_MACHINES.items():
        count = 4 if machine == "gpu1" else 0
        readings.extend(
            [
                sample(machine, "up", 1, {}),
                sample(machine, "cluster_machine_specs_timestamp_seconds", end, {}),
                sample(machine, "cluster_machine_gpu_count", count, {}),
            ]
        )
        if count:
            readings.append(sample(machine, "cluster_gpu_collection_timestamp_seconds", end, {}))
        for unit in values["units"]:
            readings.append(sample(machine, "node_systemd_unit_state", 1, {"name": unit, "state": "active"}))
        for mount in values["mounts"]:
            for name, value in [("readonly", 0), ("avail_bytes", 1024), ("files_free", 100)]:
                readings.append(sample(machine, "node_filesystem_" + name, value, {"mountpoint": mount}))
    return readings


# What the fake Prometheus reports for home quotas: the age of the quota reading in seconds (None: no quota
# series at all), and the machines reporting them.
QUOTAS: dict[str, Any] = {"age_seconds": None, "machines": ["store"]}


def quota_samples(end: int) -> list[dict[str, Any]]:
    """Quota series as the home machine's node_exporter publishes them, labelled by Prometheus with `machine`."""
    if QUOTAS["age_seconds"] is None:
        return []

    def sample(machine: str, name: str, value: float, labels: dict[str, str]) -> dict[str, Any]:
        metric = {"__name__": "cluster_home_quota_" + name, "machine": machine, "job": "node", **labels}
        return {"metric": metric, "value": [end, str(value)]}

    readings = []
    for machine in QUOTAS["machines"]:
        readings.append(sample(machine, "collected_timestamp_seconds", time.time() - QUOTAS["age_seconds"], {}))
        for name, value in [
            ("used_bytes", 300 * 1024**3),
            ("soft_bytes", 300 * 1024**3),
            ("hard_bytes", 400 * 1024**3),
        ]:
            readings.append(sample(machine, name, value, {"user": "alice"}))
    return readings


class FakePrometheus(BaseHTTPRequestHandler):
    """Fake Prometheus API: instant readings from MACHINES and QUOTAS, and an idle hour for gpu1's 4 GPUs."""

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parameters = {key: values[0] for key, values in parse_qs(url.query).items()}
        if url.path == "/api/v1/query" and "cluster_home_quota" in parameters["query"]:
            result = quota_samples(int(parameters["time"]))
        elif url.path == "/api/v1/query":
            result = prometheus_samples(int(parameters["time"]))
        elif url.path == "/api/v1/query_range":
            times = range(int(parameters["start"]), int(parameters["end"]) + 1, int(parameters["step"]))
            result = [
                {"metric": {"machine": "gpu1", "uuid": f"GPU-{gpu}"}, "values": [[t, "0"] for t in times]}
                for gpu in range(4)
            ]
        else:
            self.send_error(404)
            return
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet."""


class MonitorPrometheus(FakePrometheus):
    """Fake Prometheus with measured monitor-only machine inventory."""

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parameters = {key: values[0] for key, values in parse_qs(url.query).items()}
        if url.path == "/api/v1/query":
            result = monitor_samples(int(parameters["time"]))
        elif url.path == "/api/v1/query_range":
            times = range(int(parameters["start"]), int(parameters["end"]) + 1, int(parameters["step"]))
            result = [
                {"metric": {"machine": "gpu1", "uuid": f"GPU-{gpu}"}, "values": [[t, "0"] for t in times]}
                for gpu in range(4)
            ]
        else:
            self.send_error(404)
            return
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def fake_slurm(root: Path) -> dict[str, str]:
    """Put the fake Slurm commands first on PATH and return the environment to run the collector with."""
    fake = root / "commands"
    fake.mkdir()
    command = fake / "slurm-fake"
    command.write_text(f"#!{sys.executable}\n" + FAKE_SLURM)
    command.chmod(0o700)
    for name in ("scontrol", "squeue", "sshare", "sprio", "sacct", "sacctmgr"):
        (fake / name).symlink_to(command)
    return dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"])


class SnapshotTests(unittest.TestCase):
    """The public snapshot must reflect allocations, not CPU billing or invented health."""

    def test_collection_and_failure_preserve_truth(self) -> None:
        """Check sums, GPU typing, policy values, and atomic failure behavior through the command."""
        with tempfile.TemporaryDirectory(prefix="monitor-snapshot-") as directory:
            root = Path(directory)
            output = root / "status.json"
            metrics = root / "cluster.prom"
            mirror = root / "public-mirror.json"
            machines = root / "machines.md"
            machines_mirror = root / "public-machines.md"
            machine_file = root / "machines.json"
            machine_file.write_text(json.dumps(MACHINES))
            env = fake_slurm(root)
            args = [
                sys.executable,
                str(COLLECTOR),
                "--output",
                str(output),
                "--metrics",
                str(metrics),
                "--controller",
                "front",
                "--cluster-name",
                "demo",
                "--machines",
                str(machine_file),
                "--refresh-seconds",
                "30",
                "--accounting-start",
                "2025-09-01T12:00:00",
                "--mirror-output",
                str(mirror),
                "--machine-markdown-output",
                str(machines),
                "--machine-markdown-mirror-output",
                str(machines_mirror),
                "--users",
                "alice",
                "bob",
            ]
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(mirror.read_bytes(), output.read_bytes())
            self.assertEqual(machines_mirror.read_bytes(), machines.read_bytes())
            machine_text = machines.read_text()
            self.assertTrue(machine_text.startswith("# demo machines\n"))
            self.assertNotIn("](/", machine_text)
            self.assertIn(
                "| gpu1 | Unknown | Unknown | 2/4 | 4 GPUs | Unknown | Unknown | Unknown | Unknown |", machine_text
            )
            self.assertIn(
                "| cpu1 | Unknown | Not applicable | 0/0 | 0 | Not applicable | Unknown | Unknown | Unknown |",
                machine_text,
            )
            self.assertNotIn("| store |", machine_text)
            self.assertNotIn("backup", data)
            self.assertEqual(data["accounting_start"], "2025-09-01T12:00:00")
            self.assertEqual(
                [(node["name"], node["role"]) for node in data["nodes"]],
                [("front", "Front node"), ("gpu1", "Compute"), ("cpu1", "Compute"), ("store", "Storage")],
            )
            self.assertEqual(data["nodes"][1]["partitions"], ["interactive", "main"])
            self.assertEqual(data["nodes"][1]["building"], "Lab A")
            self.assertEqual((data["running_jobs"], data["pending_jobs"]), (1, 1))
            self.assertEqual(data["average_wait_seconds_30d"], 1200)
            self.assertEqual(
                [(job["id"], job["user"], job["state"], job["gpus"]) for job in data["jobs"]],
                [("1", "alice", "RUNNING", 1), ("2", "bob", "PENDING", 0)],
            )
            self.assertEqual(data["jobs"][0]["node"], "gpu1")
            self.assertGreater(data["jobs"][0]["seconds"], 0)
            self.assertEqual(data["jobs"][1]["priority"], 100)
            self.assertEqual((data["total_gpus"], data["allocated_gpus"]), (4, 2))
            self.assertEqual(data["nodes"][1]["available_gpus"], 2)
            self.assertEqual((data["nodes"][2]["available_gpus"], data["nodes"][2]["total_gpus"]), (0, 0))
            self.assertNotIn("available_gpus", data["nodes"][3])
            users = {item["user"]: item for item in data["ranking"]}
            self.assertEqual(users["alice"]["gpu_hours"], 4.5)
            self.assertEqual(users["alice"]["decayed_gpu_hours"], 1)
            self.assertEqual(users["bob"]["gpu_hours"], 0)
            person = {item["user"]: item for item in data["users"]}["alice"]
            self.assertAlmostEqual(person["gpu_hours_7d"], 2.5, places=2)
            self.assertAlmostEqual(person["gpu_hours_30d"], 3.5, places=2)
            self.assertAlmostEqual(person["gpu_hours_365d"], 4.5, places=2)
            # Without Prometheus there are no quota readings.
            self.assertIsNone(person["home"])
            self.assertNotIn("root", [item["user"] for item in data["users"]])
            self.assertNotIn("root", [item["user"] for item in data["ranking"]])
            self.assertEqual(data["priorities"][0]["fairshare"], 5000)
            self.assertIn("30", " ".join(item["value"] for item in data["policies"]))
            self.assertEqual(
                data["partitions"],
                [
                    {"name": "interactive", "default": False, "max_time": "08:00:00", "nodes": "gpu1"},
                    {"name": "main", "default": True, "max_time": "1-00:00:00", "nodes": "gpu1,cpu1"},
                ],
            )
            text = metrics.read_text()
            self.assertIn('cluster_allocated_gpu_hours{user="alice"} 4.5', text)
            self.assertIn('cluster_node_available_gpus{node="gpu1"} 2', text)
            self.assertIn('cluster_node_allocated_gpus{node="gpu1"} 2', text)
            self.assertIn('cluster_node_total_gpus{node="gpu1"} 4', text)
            self.assertIn('cluster_node_total_gpus{node="cpu1"} 0', text)
            self.assertNotIn('node="store"', text)
            self.assertIn("cluster_snapshot_timestamp_seconds", text)
            self.assertIn('cluster_job_requested_gpus{job_id="1"} 1', text)
            self.assertIn('cluster_job_requested_memory_bytes{job_id="1"} 34359738368', text)
            self.assertIn('cluster_job_requested_gpus{job_id="2"} 0', text)
            self.assertIn('cluster_job_elapsed_seconds{job_id="2"} 0', text)
            self.assertIn('reason="Dependency"', text)
            # Finished jobs of the last 7 days, from accounting; running and pending jobs come from squeue only.
            self.assertIn(
                'cluster_job_info{job_id="7",user="alice",job_name="train|a",state="COMPLETED",reason="",node="gpu1",partition="main"} 1',
                text,
            )
            self.assertIn('cluster_job_elapsed_seconds{job_id="7"} 3600', text)
            self.assertIn('cluster_job_waiting_seconds{job_id="7"} 60', text)
            self.assertIn('cluster_job_requested_gpus{job_id="7"} 1', text)
            self.assertIn('cluster_job_requested_memory_bytes{job_id="7"} 34359738368', text)
            self.assertRegex(text, r'cluster_job_end_seconds\{job_id="7"\} \d+')
            self.assertNotIn('cluster_job_priority{job_id="7"}', text)
            self.assertIn('state="TIMEOUT"', text)
            self.assertIn(
                'cluster_job_info{job_id="9_[1-3]",user="alice",job_name="queued",state="CANCELLED",reason="",node="",partition="main"} 1',
                text,
            )
            self.assertIn('cluster_job_elapsed_seconds{job_id="9_[1-3]"} 0', text)
            self.assertIn('cluster_job_waiting_seconds{job_id="9_[1-3]"} 600', text)
            self.assertNotIn('job_id="10"', text)
            self.assertEqual(text.count('cluster_job_info{job_id="1",'), 1)
            self.assertIn('cluster_job_info{job_id="1",user="alice",job_name="test \\"job\\"",state="RUNNING"', text)
            # A tab in a job name becomes a space and a newline is escaped, so the file stays valid for
            # Prometheus; a newline that splits an accounting line is joined back into the name.
            self.assertIn('cluster_job_info{job_id="2",user="bob",job_name="cpu job",', text)
            self.assertIn('cluster_job_info{job_id="8",user="bob",job_name="she\\nll",', text)
            self.assertNotIn('cluster_job_end_seconds{job_id="1"}', text)
            full = subprocess.run(
                args, env=dict(env, TEST_ALLOCATED_GPUS="4"), capture_output=True, text=True, check=False
            )
            self.assertEqual(full.returncode, 0, full.stderr)
            self.assertEqual(json.loads(output.read_text())["nodes"][1]["available_gpus"], 0)
            self.assertIn('cluster_node_available_gpus{node="gpu1"} 0', metrics.read_text())
            self.assertIn('cluster_node_allocated_gpus{node="gpu1"} 4', metrics.read_text())
            before = output.read_bytes()
            before_metrics = metrics.read_bytes()
            before_machines = machines.read_bytes()
            failed = subprocess.run(args, env=dict(env, TEST_FAIL="sacct"), capture_output=True, text=True, check=False)
            self.assertNotEqual(failed.returncode, 0)
            # A Slurm node missing from the machines file is an error, not a machine without checks.
            machine_file.write_text(json.dumps({name: value for name, value in MACHINES.items() if name != "cpu1"}))
            unlisted = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(unlisted.returncode, 0)
            self.assertIn("cpu1", unlisted.stderr)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(mirror.read_bytes(), before)
            self.assertEqual(metrics.read_bytes(), before_metrics)
            self.assertEqual(machines.read_bytes(), before_machines)
            self.assertEqual(machines_mirror.read_bytes(), before_machines)

    def test_health_and_quotas_from_prometheus(self) -> None:
        """Every machine measured healthy: GPU, CPU-only, and storage machines are Healthy, storage has no Slurm check.

        Home quotas come from Prometheus: shown when fresh, left out when older than 10 minutes or absent, and an
        error when more than one machine reports them.
        """
        self.addCleanup(QUOTAS.update, {"age_seconds": None, "machines": ["store"]})
        QUOTAS.update({"age_seconds": 30, "machines": ["store"]})
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrometheus)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="monitor-snapshot-") as directory:
            root = Path(directory)
            machine_file = root / "machines.json"
            machine_file.write_text(json.dumps(MACHINES))
            output = root / "status.json"
            args = [
                sys.executable,
                str(COLLECTOR),
                "--output",
                str(output),
                "--metrics",
                str(root / "cluster.prom"),
                "--controller",
                "front",
                "--cluster-name",
                "demo",
                "--machines",
                str(machine_file),
                "--refresh-seconds",
                "30",
                "--accounting-start",
                "2025-09-01T12:00:00",
                "--machine-markdown-output",
                str(root / "machines.md"),
                "--prometheus-url",
                f"http://127.0.0.1:{server.server_address[1]}",
                "--users",
                "alice",
                "bob",
            ]
            env = fake_slurm(root)
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(output.read_text())
            homes = {item["user"]: item["home"] for item in data["users"]}
            alice = {"used_bytes": 300 * 1024**3, "soft_bytes": 300 * 1024**3, "hard_bytes": 400 * 1024**3}
            self.assertEqual(homes, {"alice": alice, "bob": None})
            nodes = {node["name"]: node for node in data["nodes"]}
            self.assertEqual(
                {name: (node["health"], node["gpu_usage"], node["health_details"]) for name, node in nodes.items()},
                {
                    "front": ("Healthy", "Not applicable", []),
                    "gpu1": ("Healthy", "Idle", []),
                    "cpu1": ("Healthy", "Not applicable", []),
                    "store": ("Healthy", "Not applicable", []),
                },
            )
            self.assertIn("| gpu1 | Healthy | Idle | 2/4 |", (root / "machines.md").read_text())
            for age in (11 * 60, None):
                QUOTAS["age_seconds"] = age
                skipped = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(skipped.returncode, 0, skipped.stderr)
                homes = {item["user"]: item["home"] for item in json.loads(output.read_text())["users"]}
                self.assertEqual(homes, {"alice": None, "bob": None})
            QUOTAS.update({"age_seconds": 30, "machines": ["store", "front"]})
            before = output.read_bytes()
            twice = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(twice.returncode, 0)
            self.assertIn("more than one machine", twice.stderr)
            self.assertEqual(output.read_bytes(), before)

    def test_monitor_collection_without_slurm(self) -> None:
        """The public monitor path uses measurements only, with no scheduler figures or commands."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), MonitorPrometheus)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="monitor-only-snapshot-") as directory:
            root = Path(directory)
            machine_file = root / "machines.json"
            machine_file.write_text(json.dumps(MONITOR_MACHINES))
            output = root / "status.json"
            metrics = root / "cluster.prom"
            markdown = root / "machines.md"
            args = [
                sys.executable,
                str(COLLECTOR),
                "--mode", "monitor",
                "--output", str(output),
                "--metrics", str(metrics),
                "--controller", "front",
                "--cluster-name", "demo",
                "--machines", str(machine_file),
                "--refresh-seconds", "30",
                "--machine-markdown-output", str(markdown),
                "--prometheus-url", f"http://127.0.0.1:{server.server_address[1]}",
            ]
            empty_path = root / "no-commands"
            empty_path.mkdir()
            result = subprocess.run(args, env=dict(os.environ, PATH=str(empty_path)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(data["mode"], "monitor")
            self.assertEqual(data["refresh_seconds"], 30)
            self.assertEqual(data["total_gpus"], 4)
            self.assertEqual(
                [(node["name"], node["role"], node["health"], node["gpu_usage"], node["total_gpus"])
                 for node in data["nodes"]],
                [("front", "Monitor", "Healthy", "Not applicable", 0),
                 ("gpu1", "Machine", "Healthy", "Idle", 4),
                 ("cpu1", "Machine", "Healthy", "Not applicable", 0)],
            )
            for key in ("jobs", "running_jobs", "pending_jobs", "allocated_gpus", "ranking", "partitions"):
                self.assertNotIn(key, data)
            self.assertIn("cluster_snapshot_timestamp_seconds", metrics.read_text())
            self.assertNotIn("cluster_job_", metrics.read_text())
            self.assertIn("| gpu1 | Healthy | Idle |", markdown.read_text())
            self.assertNotIn("Available GPUs", markdown.read_text())
            self.assertNotIn("Slurm", markdown.read_text())


if __name__ == "__main__":
    unittest.main()
