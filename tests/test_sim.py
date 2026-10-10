"""Tests for the simulated test cluster (`nanohpc sim up/down`).

The unit tests need no VMs. `SimClusterTest` starts real Lima VMs and runs only when NANOHPC_SIM=1 is set,
because it takes minutes and several GB of memory.
"""

import asyncio
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.parse
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml
from textual.widgets import DataTable

from nanohpc import fixuid, maintenance_lock, probe, ssh_update, update, wizard
from nanohpc.config import check_config, load_config
from nanohpc.probe import probe_machine
from nanohpc.sim import SimPlan, load_sim, render_cluster, render_ssh_config
from nanohpc.wizard.state import checklist

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "tests" / "sim"


def write_sim(directory: Path, fields: dict[str, Any]) -> Path:
    """Write a sim file next to a copy of the example cluster and return its path."""
    shutil.copy(ROOT / "examples" / "cluster.yml", directory / "cluster.yml")
    sim = {
        "cluster": "cluster.yml",
        "ubuntu": "24.04",
        "fake_gpus": ["gpu4", "gpu2", "gpu4i"],
        "vms": {"default": {"cpus": 1, "memory_gb": 1, "disk_gb": 10}},
        "extra_disk_gb": 10,
    }
    sim.update(fields)
    path = directory / "test.yml"
    path.write_text(yaml.safe_dump(sim))
    return path


def plan_of(path: Path) -> SimPlan:
    """Load a sim file that must be valid."""
    plan, errors = load_sim(path)
    assert plan is not None, errors
    return plan


class SimPlanTest(unittest.TestCase):
    """Reading a sim file and planning the VMs, without starting any."""

    def test_shipped_sim_files_are_valid(self) -> None:
        for name, machines in [("everyday", 6), ("home-on-storage", 6), ("large", 21), ("x86-build", 2), ("shared", 4)]:
            with self.subTest(name):
                plan, errors = load_sim(SIM / f"{name}.yml")
                self.assertEqual(errors, [])
                assert plan is not None
                self.assertEqual(len(plan.vms), machines)

    def test_vm_sizes_and_disks(self) -> None:
        plan = plan_of(SIM / "everyday.yml")
        vms = {vm.machine: vm for vm in plan.vms}
        self.assertEqual((vms["front"].cpus, vms["front"].memory_gb, vms["front"].disk_gb), (2, 3, 20))
        self.assertEqual((vms["cpu1"].cpus, vms["cpu1"].memory_gb), (1, 1))
        self.assertEqual(vms["front"].instance, "nanohpc-everyday-front")
        # One extra disk per device path, attached in device order (/dev/vdb, /dev/vdc, ...).
        self.assertEqual(vms["front"].disks, ["nanohpc-everyday-front-vdb"])
        self.assertEqual(vms["gpu4"].disks, ["nanohpc-everyday-gpu4-vdb"])
        self.assertEqual(vms["gpu2"].disks, [])
        self.assertEqual(plan.fake_gpus, ["gpu4", "gpu2", "gpu4i"])

    def test_shared_dataset_server_has_its_own_disk(self) -> None:
        plan = plan_of(SIM / "shared.yml")
        vms = {vm.machine: vm for vm in plan.vms}
        self.assertEqual(vms["store"].disks, ["nanohpc-shared-store-vdb"])

    def test_invalid_sim_files(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            ({"fake_gpus": ["cpu1"]}, "fake_gpus: cpu1 has no gpu in the cluster configuration"),
            ({"fake_gpus": ["nosuch"]}, "fake_gpus: nosuch is not a machine in the cluster configuration"),
            ({"ubuntu": "20.04"}, "ubuntu must be one of 22.04, 24.04, 26.04"),
            ({"vms": {"default": {"cpus": 1, "memory_gb": 1}}}, "vms.default.disk_gb is required"),
            (
                {"vms": {"default": {"cpus": 1, "memory_gb": 1, "disk_gb": 10}, "nosuch": {}}},
                "vms.nosuch is not a machine",
            ),
            ({"cluster": "missing.yml"}, "cluster: file not found"),
            ({"extra": 1}, "extra is not a known field"),
            ({"fake_gpus": [["gpu4"]]}, "fake_gpus[0] must be a machine name"),
            ({"fake_gpus": ["gpu4", "gpu4"]}, "fake_gpus has duplicates"),
            ({"cluster": 5}, "cluster must be a file name"),
        ]
        for fields, expected in cases:
            with self.subTest(expected), tempfile.TemporaryDirectory() as temporary:
                _, errors = load_sim(write_sim(Path(temporary), fields))
                self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")

    def test_invalid_cluster_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sim(Path(temporary), {})
            cluster = Path(temporary) / "cluster.yml"
            cluster.write_text(cluster.read_text().replace("memory_mb: 4096", "memory_mb: 0", 1))
            _, errors = load_sim(path)
        self.assertTrue(
            any("cluster.yml: machines.gpu4.memory_mb must be a positive integer" in e for e in errors), errors
        )

    def test_devices_must_follow_vm_disk_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sim(Path(temporary), {})
            cluster = Path(temporary) / "cluster.yml"
            cluster.write_text(cluster.read_text().replace("{device: /dev/vdb}", "{device: /dev/nvme1n1}"))
            _, errors = load_sim(path)
        expected = (
            "machines.gpu4: a simulated machine's extra disks are /dev/vdb, /dev/vdc, ... in order, found /dev/nvme1n1"
        )
        self.assertIn(expected, errors)


class MonitorSimPlanTest(unittest.TestCase):
    """Monitor simulations specify fake GPU count and model without Slurm machine GPU fields."""

    def test_shipped_monitor_sim(self) -> None:
        """A host, one fake-GPU machine, and one CPU-only machine are planned."""
        plan = plan_of(SIM / "monitor.yml")
        self.assertEqual({vm.machine for vm in plan.vms}, {"host", "gpu1", "cpu1"})
        self.assertEqual(plan.fake_gpus, {"gpu1": {"count": 2, "type": "A6000"}})
        addresses = {vm.machine: f"192.168.104.{50 + index}" for index, vm in enumerate(plan.vms)}
        with tempfile.TemporaryDirectory() as directory:
            rendered = render_cluster(plan, addresses, Path(directory))
        config, errors = check_config(yaml.safe_load(rendered))
        self.assertEqual(errors, [])
        self.assertEqual(config["cluster"]["mode"], "monitor")

    def test_monitor_fake_gpu_mapping_validation(self) -> None:
        """Reject unknown machines, invalid counts and models, and the Slurm list form in monitor mode."""
        cases = [
            ({"gpu1": {"count": 0, "type": "A6000"}}, "fake_gpus.gpu1.count"),
            ({"gpu1": {"count": True, "type": "A6000"}}, "fake_gpus.gpu1.count"),
            ({"gpu1": {"count": 2, "type": "bad type"}}, "fake_gpus.gpu1.type"),
            ({"missing": {"count": 2, "type": "A6000"}}, "missing is not a machine"),
            ({"gpu1": {"count": 2}}, "fake_gpus.gpu1.type"),
            (["gpu1"], "fake_gpus must be a mapping"),
        ]
        for fake_gpus, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                shutil.copy(ROOT / "examples" / "monitor.yml", folder / "monitor.yml")
                path = folder / "test.yml"
                path.write_text(
                    yaml.safe_dump(
                        {
                            "cluster": "monitor.yml",
                            "ubuntu": "24.04",
                            "fake_gpus": fake_gpus,
                            "vms": {"default": {"cpus": 1, "memory_gb": 1, "disk_gb": 10}},
                            "extra_disk_gb": 10,
                        }
                    )
                )
                _, errors = load_sim(path)
                self.assertTrue(any(expected in error for error in errors), errors)


class SimOutputTest(unittest.TestCase):
    """The files `sim up` writes: the cluster.yml with real VM addresses, and the SSH machine list."""

    def test_cluster_gets_vm_addresses_and_stays_valid(self) -> None:
        plan = plan_of(SIM / "everyday.yml")
        addresses = {vm.machine: f"192.168.104.{50 + index}" for index, vm in enumerate(plan.vms)}
        with tempfile.TemporaryDirectory() as directory:
            text = render_cluster(plan, addresses, Path(directory))
        config, errors = check_config(yaml.safe_load(text))
        self.assertEqual(errors, [])
        self.assertEqual({name: machine["address"] for name, machine in config["machines"].items()}, addresses)
        self.assertTrue(text.startswith("# Generated by nanohpc sim up"))

    def test_own_certificate_and_logo_for_the_website(self) -> None:
        """For `https: own`, sim up stands in for the administrator: it makes a test authority and a certificate
        for the website hostname. The logo path is made absolute, since the generated cluster.yml is elsewhere."""
        plan = plan_of(SIM / "ubuntu-2204.yml")
        addresses = {vm.machine: f"192.168.104.{50 + index}" for index, vm in enumerate(plan.vms)}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "cluster.yml").write_text(render_cluster(plan, addresses, state))
            config, errors = load_config(state / "cluster.yml", True, False)
            self.assertEqual(errors, [])
            website = config["cluster"]["website"]
            self.assertEqual(website["https"], "own")
            self.assertEqual(website["certificate"], str((state / "website-tls" / "cert.pem").resolve()))
            self.assertEqual(website["logo"], str((SIM / "logo.svg").resolve()))
            check = subprocess.run(
                ["openssl", "verify", "-CAfile", str(state / "website-tls" / "ca.pem"), website["certificate"]],
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            names = subprocess.run(
                ["openssl", "x509", "-noout", "-text", "-in", website["certificate"]],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn(f"DNS:{website['hostname']}", names)
            # A second sim up keeps the same certificate.
            before = (state / "website-tls" / "cert.pem").read_bytes()
            render_cluster(plan, addresses, state)
            self.assertEqual((state / "website-tls" / "cert.pem").read_bytes(), before)

    def test_ssh_config_lists_every_machine(self) -> None:
        plan = plan_of(SIM / "everyday.yml")
        ports = {vm.machine: 60000 + index for index, vm in enumerate(plan.vms)}
        text = render_ssh_config(plan, ports, "enaj", Path("/home/enaj/.lima/_config/user"))
        for machine, port in ports.items():
            self.assertIn(f"Host {machine}\n  HostName 127.0.0.1\n  Port {port}\n  User enaj\n", text)
        self.assertIn("IdentityFile /home/enaj/.lima/_config/user", text)


class SimCommandTest(unittest.TestCase):
    """The `nanohpc sim` command line, without VMs."""

    def test_dry_run_only_with_deploy(self) -> None:
        for action in ("up", "down"):
            result = subprocess.run(
                [sys.executable, "-c", "from nanohpc.cli import main; main()", "sim", action, str(SIM / "everyday.yml"), "--dry-run"],
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            self.assertEqual(result.returncode, 1, action)
            self.assertIn("--dry-run works only with nanohpc sim deploy", result.stderr)
            self.assertEqual(result.stdout, "")


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimClusterTest(unittest.TestCase):
    """End to end: `nanohpc sim up` makes reachable VMs, `nanohpc sim down` removes them. Real Lima VMs."""

    def run_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run a command in the repository and return its result."""
        return subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, check=False)

    def ssh(self, state: Path, machine: str, command: str) -> subprocess.CompletedProcess[str]:
        """Run a command on a simulated machine through the generated SSH config."""
        return self.run_command("ssh", "-F", str(state / "ssh_config"), machine, command)

    def test_up_and_down(self) -> None:
        sim_name = os.environ.get("NANOHPC_SIM_FILE", "everyday")
        sim = SIM / f"{sim_name}.yml"
        state = ROOT / ".nanohpc-sim" / sim_name
        try_down = True
        # Registered first, so VMs are removed even when `sim up` itself fails.
        self.addCleanup(lambda: try_down and self.run_command("uv", "run", "nanohpc", "sim", "down", str(sim)))
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # The generated cluster.yml is valid and holds the VMs' real addresses.
        result = self.run_command("uv", "run", "nanohpc", "validate", str(state / "cluster.yml"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = yaml.safe_load((state / "cluster.yml").read_text())
        self.assertEqual(yaml.safe_load((state / "fake-gpus.yml").read_text()), {"fake_gpus": plan_of(sim).fake_gpus})
        front = config["machines"]["front"]["address"]
        for machine, values in config["machines"].items():
            with self.subTest(machine=machine):
                # The host reaches every machine over SSH, with sudo.
                result = self.ssh(state, machine, "sudo -n true && hostname")
                self.assertEqual(result.returncode, 0, result.stderr)
                # Every machine reaches the front node, and the front node reaches it, on the cluster network.
                self.assertEqual(self.ssh(state, machine, f"ping -c 1 -W 2 {front}").returncode, 0)
                self.assertEqual(self.ssh(state, "front", f"ping -c 1 -W 2 {values['address']}").returncode, 0)
                # Each device named in the configuration exists on the machine.
                devices = [values.get("home", {}).get("device"), values.get("scratch", {}).get("device")]
                for device in filter(None, devices):
                    self.assertEqual(self.ssh(state, machine, f"test -b {device}").returncode, 0, device)

        # A second `sim up` reuses the running VMs.
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("creating", result.stdout)

        with tempfile.TemporaryDirectory() as temporary:
            # A changed VM size is refused, not silently ignored.
            changed = yaml.safe_load(sim.read_text())
            changed["cluster"] = str(plan_of(sim).cluster_path)
            changed["vms"]["default"]["cpus"] = 2
            resized = Path(temporary) / sim.name
            resized.write_text(yaml.safe_dump(changed))
            result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(resized))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("run nanohpc sim down first", result.stderr)

            # `sim down` removes everything even when the sim file no longer points at a valid cluster.
            changed["cluster"] = "missing.yml"
            resized.write_text(yaml.safe_dump(changed))
            result = self.run_command("uv", "run", "nanohpc", "sim", "down", str(resized))
        try_down = False
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listing = self.run_command("limactl", "list", "--format", "{{.Name}}").stdout
        self.assertNotIn(f"nanohpc-{sim_name}-", listing)
        disks = self.run_command("limactl", "disk", "ls", "--json").stdout
        self.assertNotIn(f"nanohpc-{sim_name}-", disks)
        self.assertFalse(state.exists())


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimMonitorDeployTest(unittest.TestCase):
    """Deploy monitor mode to real VMs and check the live monitoring boundary."""

    sim = SIM / "monitor.yml"
    state = ROOT / ".nanohpc-sim" / "monitor"

    def run_nanohpc(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the current source's command from the repository checkout."""
        return subprocess.run(
            [sys.executable, "-c", "from nanohpc.cli import main; main()", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def ssh(self, machine: str, command: str) -> subprocess.CompletedProcess[str]:
        """Run a command on one VM with the generated administrator SSH config."""
        return subprocess.run(
            ["ssh", "-F", str(self.state / "ssh_config"), machine, command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_monitor_only_deploy(self) -> None:
        if os.environ.get("NANOHPC_SIM_KEEP") != "1":
            self.addCleanup(self.run_nanohpc, "sim", "down", str(self.sim))
        up = self.run_nanohpc("sim", "up", str(self.sim))
        self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
        dry = self.run_nanohpc("sim", "deploy", str(self.sim), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        deployed = self.run_nanohpc("sim", "deploy", str(self.sim))
        self.assertEqual(deployed.returncode, 0, deployed.stdout + deployed.stderr)
        for machine in ("host", "gpu1", "cpu1"):
            with self.subTest(machine=machine, feature="unsupported wall power"):
                absent = self.ssh(
                    machine,
                    "test ! -e /var/lib/nanohpc/metrics-textfile/power.prom && "
                    "test ! -e /etc/systemd/system/nanohpc-power-metrics.timer",
                )
                self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
        checked = self.run_nanohpc(
            "check", str(self.state / "cluster.yml"), "--ssh-config", str(self.state / "ssh_config")
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        for machine in ("host", "gpu1", "cpu1"):
            with self.subTest(machine=machine):
                result = self.ssh(
                    machine,
                    'test "$(cat /etc/nanohpc/mode)" = monitor && ! test -e /etc/slurm/slurm.conf && ! getent passwd alice',
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshot = self.ssh("host", "cat /var/lib/nanohpc/monitor/status.json")
        self.assertEqual(snapshot.returncode, 0, snapshot.stdout + snapshot.stderr)
        status = json.loads(snapshot.stdout)
        self.assertEqual(status["mode"], "monitor")
        self.assertEqual(status["total_gpus"], 2)
        self.assertEqual(status["users"], [{"user": "alice"}, {"user": "bob"}])
        gpu = self.ssh(
            "host",
            'curl -fsSG --data-urlencode "query=cluster_gpu_utilization_percent" http://127.0.0.1:9090/api/v1/query',
        )
        self.assertEqual(gpu.returncode, 0, gpu.stdout + gpu.stderr)
        self.assertEqual(len(json.loads(gpu.stdout)["data"]["result"]), 2)
        website = self.ssh(
            "host",
            "curl -fsSk --resolve lab.example.org:443:127.0.0.1 https://lab.example.org/cluster/site.json",
        )
        self.assertEqual(website.returncode, 0, website.stdout + website.stderr)
        self.assertEqual(json.loads(website.stdout)["mode"], "monitor")
        cluster_name = yaml.safe_load((self.state / "cluster.yml").read_text())["cluster"]["name"]
        grafana = self.ssh("host", "curl -fsS http://127.0.0.1:3000/cluster/grafana/api/search?type=dash-db")
        self.assertEqual(grafana.returncode, 0, grafana.stdout + grafana.stderr)
        dashboards = json.loads(grafana.stdout)
        self.assertEqual({item["uid"] for item in dashboards}, {"nanohpc-machines", "nanohpc-history"})
        for dashboard in dashboards:
            self.assertTrue(dashboard["title"].startswith(f"{cluster_name}: "), dashboard)
            self.assertEqual(dashboard["folderTitle"], cluster_name)
        machines_dashboard = self.ssh(
            "host", "curl -fsS http://127.0.0.1:3000/cluster/grafana/api/dashboards/uid/nanohpc-machines"
        )
        self.assertEqual(machines_dashboard.returncode, 0, machines_dashboard.stdout + machines_dashboard.stderr)
        variables = json.loads(machines_dashboard.stdout)["dashboard"]["templating"]["list"]
        self.assertEqual([variable["name"] for variable in variables], ["gpu_group", "machine"])
        self.assertTrue(variables[1]["multi"])
        self.assertTrue(variables[1]["includeAll"])
        power_panel = next(
            panel
            for panel in json.loads(machines_dashboard.stdout)["dashboard"]["panels"]
            if panel["title"] == "Total power"
        )
        self.assertIn("cluster_wall_power_watts", power_panel["targets"][0]["expr"])
        machine_label_route = (
            "https://lab.example.org/cluster/grafana/api/datasources/uid/cluster-detail/"
            "resources/api/v1/label/machine/values"
        )
        public_labels = self.ssh("host", f"curl -fsSk --resolve lab.example.org:443:127.0.0.1 '{machine_label_route}'")
        self.assertEqual(public_labels.returncode, 0, public_labels.stdout + public_labels.stderr)
        self.assertTrue({"host", "gpu1", "cpu1"}.issubset(set(json.loads(public_labels.stdout)["data"])))
        denied = self.ssh(
            "host",
            "curl -sSk -o /dev/null -w '%{http_code}' --resolve lab.example.org:443:127.0.0.1 "
            "'https://lab.example.org/cluster/grafana/api/datasources/uid/cluster-detail/"
            "resources/api/v1/label/job/values'",
        )
        self.assertEqual(denied.stdout, "403")

        # Recreate the old folder on this VM, then confirm redeploy removes it.
        provider = "/etc/nanohpc/grafana/provisioning/dashboards/cluster.yml"
        previous = self.ssh("host", f"cat {provider}")
        self.assertIn(f"folder: {cluster_name}\n", previous.stdout)
        self.assertIn("folderUid: nanohpc-cluster", previous.stdout)
        old_provider = self.ssh(
            "host",
            f"sudo sed -i -e 's/^    folder: {cluster_name}$/    folder: Cluster/' "
            f"-e '/^    folderUid: nanohpc-cluster$/d' {provider} && sudo systemctl restart nanohpc-grafana",
        )
        self.assertEqual(old_provider.returncode, 0, old_provider.stdout + old_provider.stderr)
        folders_url = "http://127.0.0.1:3000/cluster/grafana/api/folders"
        for _ in range(60):
            before = self.ssh("host", f"curl -fsS '{folders_url}'")
            if before.returncode == 0 and any(folder["title"] == "Cluster" for folder in json.loads(before.stdout)):
                break
            time.sleep(1)
        else:
            self.fail(f"Grafana did not create the old folder: {before.stdout} {before.stderr}")
        # The VM has no management chip. Give its front node one fake input sensor,
        # then check the real deploy, systemd, node_exporter, and Prometheus path.
        fake_ipmi = "#!/bin/sh\nprintf 'PSU1 Power In | DFh | ok | 10.0 | 500 Watts\\n'\n"
        encoded = base64.b64encode(fake_ipmi.encode()).decode()
        installed = self.ssh(
            "host",
            f"printf '%s' '{encoded}' | base64 -d | sudo tee /usr/local/bin/ipmitool >/dev/null && "
            "sudo chmod 0755 /usr/local/bin/ipmitool",
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        upgraded = self.run_nanohpc("sim", "deploy", str(self.sim))
        self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
        timer = self.ssh("host", "systemctl is-active nanohpc-power-metrics.timer")
        self.assertEqual(timer.stdout.strip(), "active", timer.stderr)
        power_query = "curl -fsSG --data-urlencode 'query=cluster_wall_power_watts' http://127.0.0.1:9090/api/v1/query"
        for _ in range(30):
            power = self.ssh("host", power_query)
            if power.returncode == 0 and len(json.loads(power.stdout)["data"]["result"]) == 1:
                break
            time.sleep(2)
        else:
            self.fail(f"The power reading did not reach Prometheus: {power.stdout} {power.stderr}")
        reading = json.loads(power.stdout)["data"]["result"][0]
        self.assertEqual(reading["metric"]["machine"], "host")
        self.assertEqual(float(reading["value"][1]), 500)
        stamp_command = (
            "sed -n 's/^cluster_power_collection_timestamp_seconds //p' /var/lib/nanohpc/metrics-textfile/power.prom"
        )
        first_stamp = float(self.ssh("host", stamp_command).stdout.strip())
        for _ in range(35):
            time.sleep(2)
            later = self.ssh("host", stamp_command)
            if later.returncode == 0 and float(later.stdout.strip()) > first_stamp:
                break
        else:
            self.fail("The wall-power timer did not refresh the reading")
        after = self.ssh("host", f"curl -fsS '{folders_url}'")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertEqual(
            [
                (folder["uid"], folder["title"])
                for folder in json.loads(after.stdout)
                if folder["uid"] != "sharedwithme"
            ],
            [("nanohpc-cluster", cluster_name)],
        )
        moved = self.ssh("host", "curl -fsS http://127.0.0.1:3000/cluster/grafana/api/search?type=dash-db")
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertEqual({item["uid"] for item in json.loads(moved.stdout)}, {"nanohpc-machines", "nanohpc-history"})
        self.assertTrue(all(item["folderUid"] == "nanohpc-cluster" for item in json.loads(moved.stdout)))

        # A temporary failed sensor read during a later deploy must leave its
        # scheduled collector in place, so it can recover without another deploy.
        broken = base64.b64encode(b"#!/bin/sh\nexit 1\n").decode()
        failed_reader = self.ssh(
            "host", f"printf '%s' '{broken}' | base64 -d | sudo tee /usr/local/bin/ipmitool >/dev/null"
        )
        self.assertEqual(failed_reader.returncode, 0, failed_reader.stderr)
        redeployed = self.run_nanohpc("sim", "deploy", str(self.sim))
        self.assertEqual(redeployed.returncode, 0, redeployed.stdout + redeployed.stderr)
        preserved = self.ssh(
            "host",
            "systemctl is-active nanohpc-power-metrics.timer && "
            "test -e /etc/systemd/system/nanohpc-power-metrics.service",
        )
        self.assertEqual(preserved.returncode, 0, preserved.stdout + preserved.stderr)
        stale_stamp = float(self.ssh("host", stamp_command).stdout.strip())
        restored = self.ssh(
            "host",
            f"printf '%s' '{encoded}' | base64 -d | sudo tee /usr/local/bin/ipmitool >/dev/null",
        )
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        for _ in range(45):
            time.sleep(2)
            refreshed = self.ssh("host", stamp_command)
            if refreshed.returncode == 0 and refreshed.stdout.strip() and float(refreshed.stdout.strip()) > stale_stamp:
                break
        else:
            self.fail("The retained wall-power timer did not recover after the sensor returned")


class SimUsersBase(unittest.TestCase):
    """Helpers for real-VM tests that log in as cluster users with a key generated for the test."""

    sim = SIM / "everyday.yml"
    state = ROOT / ".nanohpc-sim" / "everyday"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.keys = Path(temporary.name)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.keys / "id")], check=True)
        # A private SSH agent holding the test key, like an administrator's own agent.
        self.agent_socket = str(self.keys / "agent.sock")
        agent = subprocess.Popen(["ssh-agent", "-D", "-a", self.agent_socket], stdout=subprocess.DEVNULL)
        self.addCleanup(agent.wait)
        self.addCleanup(agent.terminate)
        for _ in range(50):
            if Path(self.agent_socket).exists():
                break
            time.sleep(0.1)
        subprocess.run(
            ["ssh-add", "-q", str(self.keys / "id")], env={**os.environ, "SSH_AUTH_SOCK": self.agent_socket}, check=True
        )

    def run_command(
        self, *arguments: str, stdin: str | None = None, agent: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run a command in the repository; with `agent`, the test SSH agent is the only one visible."""
        environment = {key: value for key, value in os.environ.items() if key != "SSH_AUTH_SOCK"}
        if agent:
            environment["SSH_AUTH_SOCK"] = self.agent_socket
        return subprocess.run(
            arguments, cwd=ROOT, input=stdin, capture_output=True, text=True, check=False, env=environment
        )

    def ssh(self, machine: str, command: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run a command on a simulated machine as the VM's default user."""
        return self.run_command("ssh", "-F", str(self.state / "ssh_config"), machine, command, stdin=stdin)

    def ssh_config_for(self, user: str) -> Path:
        """Write an SSH config that logs in to every machine as `user` with the test key and agent forwarding."""
        return self.ssh_config_with_key(user, self.keys / "id")

    def ssh_config_with_key(self, user: str, key: Path) -> Path:
        """Write an SSH config that logs in to every machine as `user` with only `key`, and agent forwarding."""
        text = (self.state / "ssh_config").read_text()
        lines = []
        for line in text.splitlines():
            if line.strip().startswith("User "):
                line = f"  User {user}"
            elif line.strip().startswith("IdentityFile "):
                line = f"  IdentityFile {key}\n  ForwardAgent yes"
            lines.append(line)
        path = self.keys / f"ssh_config_{user}_{key.name}"
        path.write_text("\n".join(lines) + "\n")
        return path

    def as_user(self, user: str, machine: str, command: str, agent: bool) -> subprocess.CompletedProcess[str]:
        """Log in as a cluster user with the test key and run a command."""
        config = self.ssh_config_for(user)
        return self.run_command("ssh", "-F", str(config), "-o", "BatchMode=yes", machine, command, agent=agent)

    def bob_login(self, key: Path) -> subprocess.CompletedProcess[str]:
        """Log in to the front node as bob with his own key (the root login test gives him one)."""
        config = self.ssh_config_with_key("bob", key)
        return self.run_command("ssh", "-F", str(config), "-o", "BatchMode=yes", "front", "true", agent=False)

    def on_front(self, command: str) -> str:
        """Run a command on the front node, require success, and return its output."""
        result = self.ssh("front", command)
        self.assertEqual(result.returncode, 0, f"{command}\n{result.stdout}{result.stderr}")
        return result.stdout

    def deploy(self, ssh_config: Path, agent: bool) -> subprocess.CompletedProcess[str]:
        """Run `nanohpc sim deploy` (it knows the fake GPUs) through a given SSH config."""
        return self.run_command(
            "uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--ssh-config", str(ssh_config), agent=agent
        )  # fmt: skip

    def remove_at_end(self) -> None:
        """Remove the simulated cluster when the test ends, unless NANOHPC_SIM_KEEP=1: then it stays up, and the
        next run deploys onto it again (quicker while fixing something; a cluster that already ran a test may
        not behave like a new one, so a milestone ends with a run from scratch)."""
        if os.environ.get("NANOHPC_SIM_KEEP") == "1":
            return
        self.addCleanup(self.run_command, "uv", "run", "nanohpc", "sim", "down", str(self.sim))

    def up_with_test_key(self) -> tuple[Path, dict[str, Any], str]:
        """Bring the sim cluster up and give its users the test key. Return the cluster file, its config,
        and the test public key. The VMs are removed when the test ends."""
        # Registered first, so VMs are removed even when `sim up` itself fails.
        self.remove_at_end()
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(self.sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Give the users the test key, in the generated cluster.yml that `sim deploy` reads.
        cluster = self.state / "cluster.yml"
        config = yaml.safe_load(cluster.read_text())
        public_key = (self.keys / "id.pub").read_text().strip()
        for user in config["users"]:
            user["ssh_keys"] = [public_key]
        cluster.write_text(yaml.safe_dump(config))
        return cluster, config, public_key

    def up_and_deploy(self) -> tuple[Path, dict[str, Any], str]:
        """Bring the sim cluster up with the test key and deploy it."""
        cluster, config, public_key = self.up_with_test_key()
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        return cluster, config, public_key

    def reboot_vm(self, machine: str, instance: str) -> None:
        """Power-cycle one Lima VM and require a new Linux boot ID."""
        before = self.ssh(machine, "cat /proc/sys/kernel/random/boot_id")
        self.assertEqual(before.returncode, 0, before.stderr)
        stopped = self.run_command("limactl", "stop", instance)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        started = self.run_command("limactl", "start", "--tty=false", "--timeout=20m", instance)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.refresh_vm_port(machine, instance)
        for _ in range(24):
            after = self.ssh(machine, "cat /proc/sys/kernel/random/boot_id")
            if after.returncode == 0:
                break
            time.sleep(5)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertNotEqual(after.stdout.strip(), before.stdout.strip(), machine)

    def refresh_vm_port(self, machine: str, instance: str) -> None:
        """Update the test SSH config after Lima assigns a new forwarded port."""
        # Lima assigns a new forwarded SSH port when a VM starts again.
        listing = self.run_command("limactl", "list", "--format", "{{.Name}} {{.SSHLocalPort}}")
        self.assertEqual(listing.returncode, 0, listing.stdout + listing.stderr)
        port = dict(line.split() for line in listing.stdout.splitlines() if line.strip())[instance]
        config = self.state / "ssh_config"
        lines = config.read_text().splitlines()
        current = ""
        replaced = False
        for index, line in enumerate(lines):
            if line.startswith("Host "):
                current = line.removeprefix("Host ")
            elif current == machine and line.startswith("  Port "):
                lines[index] = f"  Port {port}"
                replaced = True
        self.assertTrue(replaced, f"{machine} is missing from {config}")
        config.write_text("\n".join(lines) + "\n")

    def wait_for_vm(self, machine: str, command: str) -> str:
        """Wait for a boot service or mount to become usable, then return its output."""
        for _ in range(24):
            result = self.ssh(machine, command)
            if result.returncode == 0:
                return result.stdout
            time.sleep(5)
        self.fail(f"{machine}: {command} did not become ready: {result.stdout}{result.stderr}")

    def finished_job(self, job: str) -> tuple[str, str]:
        """Return the state and node of a job, once accounting has recorded it."""
        command = (
            f"for i in $(seq 20); do sacct -X -n -P -j {job} -o State | grep -q COMPLETED && break; sleep 1; done;"
            f" sacct -X -n -P -j {job} -o State,NodeList"
        )
        state, node = self.on_front(command).strip().split("|")
        return state, node

    def check_website(self, config: dict[str, Any]) -> None:
        """Check the website over real HTTPS from the front node: the certificate (Pebble's for Let's Encrypt,
        sim up's test authority for an own certificate), the pages, the routes refused, and the network limit."""
        website = config["cluster"]["website"]
        host = website["hostname"]
        path = website.get("path", "/cluster/").rstrip("/") + "/"
        if website["https"] == "own":
            copied = self.ssh(
                "front", "cat > /tmp/website-ca.pem", stdin=(self.state / "website-tls" / "ca.pem").read_text()
            )
            self.assertEqual(copied.returncode, 0, copied.stderr)
        else:
            self.on_front(
                "curl -sf --cacert /etc/nanohpc/test-acme/ca.pem https://127.0.0.1:15000/roots/0 > /tmp/website-ca.pem"
            )

        def fetch(machine: str, target: str, url_path: str, options: str) -> subprocess.CompletedProcess[str]:
            return self.ssh(
                machine,
                f"curl -s --cacert /tmp/website-ca.pem --resolve {host}:443:{target} {options} 'https://{host}{url_path}'",
            )

        def status(machine: str, target: str, url_path: str, options: str) -> str:
            return fetch(machine, target, url_path, f"-o /dev/null -w '%{{http_code}}' {options}").stdout.strip()

        # The certificate is checked: curl fails on an untrusted one or a wrong hostname.
        page = fetch("front", "127.0.0.1", path, "-f")
        self.assertEqual(page.returncode, 0, page.stderr)
        self.assertIn("<script", page.stdout)
        site = json.loads(fetch("front", "127.0.0.1", f"{path}site.json", "-f").stdout)
        self.assertEqual(site["cluster_name"], config["cluster"]["name"])
        docs = fetch("front", "127.0.0.1", f"{path}docs.md", "-f").stdout
        self.assertIn(website["login_address"], docs)
        self.assertNotIn("{{", docs)
        pages = ["data/status.json", "machines.md", "policy.md", "grafana/d/nanohpc-overview"]
        if site["logo"] is not None:
            pages.append(site["logo"])
        for url_path in pages:
            self.assertEqual(status("front", "127.0.0.1", f"{path}{url_path}", ""), "200", url_path)
        self.assertIn("no-cache", fetch("front", "127.0.0.1", path, "-sI").stdout)
        # Only the listed Grafana routes, read-only; nothing that changes the site.
        self.assertEqual(status("front", "127.0.0.1", f"{path}grafana/api/search", ""), "403")
        self.assertEqual(status("front", "127.0.0.1", f"{path}grafana/login", ""), "403")
        machine_labels = f"{path}grafana/api/datasources/uid/cluster-detail/resources/api/v1/label/machine/values"
        self.assertEqual(status("front", "127.0.0.1", machine_labels, ""), "200")
        self.assertIn(status("front", "127.0.0.1", machine_labels, "-X POST"), {"403", "405"})
        self.assertEqual(
            status(
                "front",
                "127.0.0.1",
                f"{path}grafana/api/datasources/uid/cluster-detail/resources/api/v1/label/job/values",
                "",
            ),
            "403",
        )
        self.assertIn(status("front", "127.0.0.1", f"{path}site.json", "-X POST"), {"403", "405"})
        # Plain HTTP only redirects to HTTPS.
        redirect = self.ssh(
            "front",
            f"curl -s -o /dev/null -w '%{{http_code}} %{{redirect_url}}' -H 'Host: {host}' http://127.0.0.1{path}",
        )
        self.assertEqual(redirect.stdout.strip(), f"301 https://{host}{path}")
        # From another machine: open to anyone, or refused outside the allowed networks.
        front = config["machines"]["front"]["address"]
        copied = self.ssh("cpu1", "cat > /tmp/website-ca.pem", stdin=self.on_front("cat /tmp/website-ca.pem"))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        expected = "403" if website.get("allow") else "200"
        self.assertEqual(status("cpu1", front, f"{path}site.json", ""), expected)
        self.assertEqual(status("cpu1", front, f"{path}grafana/d/nanohpc-overview", ""), expected)
        self.browser_check(host, path)

    def browser_check(self, host: str, path: str) -> None:
        """Open every page of the website in a real browser through an SSH tunnel to the front node (real nginx,
        Grafana, and security headers): the live Playwright test in the website source. It needs Node and
        Playwright's Chromium on this machine (npm ci; npx playwright install chromium in the website source).
        Afterwards, nginx's log must show no route it refused for the browser."""
        source = ROOT / "src/nanohpc/website-source"
        self.assertIsNotNone(shutil.which("npm"), "the browser check needs Node (npm) on this machine")
        refused = "sudo grep -c 'forbidden by rule' /var/log/nginx/error.log || true"
        before = int(self.on_front(refused).strip() or 0)
        tunnel = subprocess.Popen(
            ["ssh", "-F", str(self.state / "ssh_config"), "-N", "-o", "ExitOnForwardFailure=yes",
             "-L", "18443:127.0.0.1:443", "front"],
        )  # fmt: skip
        self.addCleanup(tunnel.wait)
        self.addCleanup(tunnel.terminate)
        time.sleep(3)
        environment = {
            **os.environ,
            "NANOHPC_LIVE_URL": f"https://{host}{path}",
            "NANOHPC_LIVE_HOST_RULES": f"MAP {host}:443 127.0.0.1:18443",
        }
        result = subprocess.run(
            ["npm", "run", "test:live"], cwd=source, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, self.failure(result)[-2000:])
        tunnel.terminate()
        after = int(self.on_front(refused).strip() or 0)
        self.assertEqual(after, before, self.on_front("sudo tail -20 /var/log/nginx/error.log"))

    @staticmethod
    def failure(result: subprocess.CompletedProcess[str]) -> str:
        """What a failed deploy says: every fatal and unreachable line (which can be far from the end of the
        output), then the end of the output and the errors."""
        lines = result.stdout.splitlines()
        reasons = [line for line in lines if line.startswith(("fatal:", "[ERROR]")) or "UNREACHABLE" in line]
        return "\n".join(reasons[:20]) + "\n...\n" + result.stdout[-4000:] + result.stderr

    def assert_left_out(self, result: subprocess.CompletedProcess[str], machine: str) -> None:
        """Require a deploy whose dry run failed on `machine` only: that machine was left out of the real run,
        which ran on the others, and the deploy exits with the dry run's failure code (3)."""
        self.assertEqual(result.returncode, 3, self.failure(result))
        self.assertIn(f"Dry run: {machine} failed: ", result.stdout)
        self.assertIn(f"Dry run failed on {machine}: left out of this deploy, unchanged.", result.stdout)
        self.assertIn(
            f"Left out of this deploy (their dry run failed; nothing was changed on them):\n  {machine}: ",
            result.stderr,
        )
        self.assertEqual(result.stdout.count("PLAY RECAP"), 2, "the dry run, then the real run")

    def machine_state(self, machine: str) -> str:
        """Return what a deploy could change on a machine: the files (with checksums) in /etc, /srv, /opt,
        /usr/local, and /var/lib/nanohpc, without the folders that change by themselves (metrics, the status
        snapshot, the Prometheus, Grafana, and Alertmanager data, the scratch image, the backup copy); unit files
        and running services; mounts; and on the front node, quota limits and Slurm's QoS and associations."""
        script = textwrap.dedent("""
            set -eu
            skip='/var/lib/nanohpc/(prometheus|history|grafana|alertmanager|metrics-textfile|monitor|monitor-textfile|scratch\\.img)(/|$)|^/srv/nanohpc-backup(/|$)'
            find /etc /srv /opt /usr/local /var/lib/nanohpc -xdev 2>/dev/null | grep -Ev "$skip" | sort | tr '\\n' '\\0' \\
              | xargs -0 stat -c '%n %a %U %G %s %Y'
            find /etc /srv /opt /usr/local /var/lib/nanohpc -xdev -type f 2>/dev/null | grep -Ev "$skip" | sort | tr '\\n' '\\0' \\
              | xargs -0 sha256sum
            systemctl list-unit-files --no-legend --no-pager | grep -v '^session-' | sort  # not the login sessions
            # fwupd is D-Bus activated and can stop on its own between two snapshots.
            systemctl list-units --type=service --state=running --no-legend --no-pager --plain | awk '$1 != "fwupd.service" {print $1}' | sort
            findmnt -rn -o TARGET,SOURCE,OPTIONS | sort
            if command -v sacctmgr >/dev/null && test -f /etc/slurm/slurmdbd.conf; then
              repquota -u -O csv /home | cut -d, -f1,5,6
              sacctmgr -n -P show qos; sacctmgr -n -P show assoc
            fi
        """)
        result = self.ssh(machine, f"sudo bash -c {shlex.quote(script)}")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_no_changes(self, result: subprocess.CompletedProcess[str], machines: int) -> None:
        """Require a successful deploy whose dry run found nothing to change, and whose real run changed nothing
        on any machine."""
        self.assertEqual(result.returncode, 0, self.failure(result))
        dry_run, marker, real_run = result.stdout.partition("Dry run passed: applying the changes.")
        self.assertTrue(marker, result.stdout[-4000:])
        summary = [line for line in dry_run.splitlines() if line.startswith("Dry run: ") and " has " in line]
        self.assertEqual(
            len(summary), machines, "\n".join(line for line in dry_run.splitlines() if line.startswith("Dry run"))
        )
        for line in summary:
            self.assertTrue(line.endswith(" has nothing to change"), line)
        recap = [line for line in real_run.splitlines() if " : ok=" in line]
        self.assertEqual(len(recap), machines, result.stdout[-4000:])
        changed = []
        task = ""
        for line in real_run.splitlines():
            if line.startswith("TASK ["):
                task = line
            elif line.startswith("changed: "):
                changed.append(f"{task} {line}")
        for line in recap:
            self.assertIn("changed=0 ", line, "\n".join(changed))


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimReleaseTest(SimUsersBase):
    """A deploy on one Ubuntu release, with the small cluster. Real Lima VMs.
    NANOHPC_SIM_FILE picks the sim file (default ubuntu-2604; also ubuntu-2204)."""

    def setUp(self) -> None:
        name = os.environ.get("NANOHPC_SIM_FILE", "ubuntu-2604")
        self.sim = SIM / f"{name}.yml"
        self.state = ROOT / ".nanohpc-sim" / name
        super().setUp()

    def test_deploy_on_release(self) -> None:
        _, config, _ = self.up_with_test_key()
        # A scratch disk without a filesystem stops that machine with the command to run; nanoHPC never formats it.
        self.assertEqual(self.ssh("gpu4", "sudo wipefs -q -a /dev/vdb").returncode, 0)
        # The stop comes in the dry run: gpu4 is left out of the real run, which sets up the other machines.
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assert_left_out(result, "gpu4")
        self.assertIn("gpu4: /dev/vdb has no filesystem. nanoHPC never formats a disk.", result.stdout)
        self.assertIn("mkfs.ext4 /dev/vdb", result.stdout)
        self.assertEqual(self.ssh("gpu4", "sudo blkid /dev/vdb").returncode, 2)
        # gpu4 was left out, unchanged; the other machines were deployed.
        self.assertEqual(self.ssh("gpu4", "test -e /etc/nanohpc").returncode, 1)
        self.assertEqual(self.ssh("gpu4", "getent passwd alice").returncode, 2)
        for machine in config["machines"]:
            if machine != "gpu4":
                self.assertIn("alice:x:2000:", self.ssh(machine, "getent passwd alice").stdout, machine)
        self.assertEqual(self.ssh("gpu4", "sudo mkfs.ext4 -q /dev/vdb").returncode, 0)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        version = yaml.safe_load(self.sim.read_text())["ubuntu"]
        self.assertIn(version, self.ssh("front", "cat /etc/os-release").stdout)
        nodes = sorted(line for line in self.on_front("sinfo -h -N -o '%N %P %T'").split("\n") if line)
        self.assertEqual(nodes, ["cpu1 main* idle", "gpu4 interactive idle", "gpu4 main* idle"])
        submit = "cd /tmp && sudo -u alice sbatch --parsable --wait -o /dev/null"
        for options, node in (("--gpus=1", "gpu4"), ("-w cpu1", "cpu1")):
            job = self.on_front(f"{submit} -p main {options} --wrap hostname").strip()
            self.assertEqual(self.finished_job(job), ("COMPLETED", node))
        sudo = "sudo -S -p '' true </dev/null"
        # OpenSSH 10.1+ (Ubuntu 26.04) keeps the forwarded agent socket in the user's home, on NFS on gpu4:
        # /home is exported with no_root_squash so sudo (root) can reach it.
        for machine in ("front", "gpu4"):
            self.assertEqual(self.as_user("alice", machine, sudo, agent=True).returncode, 0, machine)
        self.assertNotEqual(self.as_user("bob", "front", sudo, agent=True).returncode, 0)
        # /home never lets a program gain root, on any machine.
        for machine in ("front", "gpu4", "cpu1"):
            self.assertIn("nosuid", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout, machine)
        front = config["machines"]["front"]["address"]
        for machine in ("gpu4", "cpu1"):
            self.assertEqual(
                self.ssh(machine, "findmnt -n -o SOURCE --mountpoint /home").stdout.strip(), f"{front}:/home"
            )
        self.on_front("sudo -u alice sh -c 'echo shared > /home/alice/check'")
        self.assertEqual(self.ssh("cpu1", "sudo -u alice cat /home/alice/check").stdout.strip(), "shared")
        # -a: every filesystem with quotas (/home's own disk, or / when /home is on the root disk).
        self.assertIn("alice,ok,ok,", self.on_front("sudo repquota -a -u -O csv"))
        self.assertEqual(self.ssh("gpu4", "findmnt -n -o SOURCE --mountpoint /scratch").stdout.strip(), "/dev/vdb")
        self.assertTrue(self.ssh("cpu1", "findmnt -n -o SOURCE --mountpoint /scratch").stdout.startswith("/dev/loop"))
        self.check_website(config)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assert_no_changes(result, len(config["machines"]))
        result = self.deploy(self.ssh_config_for("alice"), agent=True)
        self.assertEqual(result.returncode, 0, self.failure(result))


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimSmallSlurmDeployTest(SimUsersBase):
    """Deploy two fresh VMs and run a Slurm job, sized for the manual Linux x86 runner."""

    sim = SIM / "x86-build.yml"
    state = ROOT / ".nanohpc-sim" / "x86-build"

    def test_slurm_build_and_job(self) -> None:
        self.up_with_test_key()
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        self.assertIn("Dry run passed: applying the changes.", result.stdout)
        self.assertEqual(self.on_front("sinfo --version").strip(), "slurm 26.05.4")
        self.assertEqual(self.ssh("cpu1", "slurmd --version").stdout.strip(), "slurm 26.05.4")
        job = self.on_front(
            "cd /tmp && sudo -u alice sbatch --parsable --wait -o /dev/null -p main -w cpu1 --wrap hostname"
        ).strip()
        self.assertEqual(self.finished_job(job), ("COMPLETED", "cpu1"))


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimUserCheckTest(SimUsersBase):
    """The deployed minute check and channel notifier work on Ubuntu 24.04 VMs."""

    sim = SIM / "x86-build.yml"
    state = ROOT / ".nanohpc-sim" / "x86-build"

    def test_deployed_user_check_and_channel_notification(self) -> None:
        """Deploy the real services, stop a user tunnel, and post its finding from the front node."""
        cluster, config, public_key = self.up_with_test_key()
        config["users"].append({"name": "bob", "uid": 2001, "ssh_keys": [public_key]})
        cluster.write_text(yaml.safe_dump(config))
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        for machine in ("front", "cpu1"):
            self.assertEqual(self.ssh(machine, "systemctl is-active nanohpc-user-check.timer").stdout.strip(), "active")
            self.assertEqual(self.ssh(machine, "sudo systemctl start nanohpc-user-check.service").returncode, 0)
        self.on_front("sudo pkill -x cloudflared || true")
        self.on_front("sudo cp /usr/bin/sleep /tmp/cloudflared; sudo chmod 755 /tmp/cloudflared")
        self.on_front("sudo rm -f /tmp/cloudflared-bob.log /tmp/cloudflared.pid")
        self.on_front(
            "sudo -u bob sh -c 'nohup /tmp/cloudflared 120 >/tmp/cloudflared-bob.log 2>&1 & echo $!' > /tmp/cloudflared.pid"
        )
        before = self.on_front("ps -p $(cat /tmp/cloudflared.pid) -o user=,stat= || true").strip()
        self.assertTrue(before.startswith("bob ") and "S" in before, f"bob's test tunnel did not start: {before}")
        self.assertEqual(self.ssh("front", "sudo systemctl restart nanohpc-user-check.service").returncode, 0)
        state = self.on_front("ps -p $(cat /tmp/cloudflared.pid) -o stat= || true").strip()
        metrics = self.on_front("cat /var/lib/nanohpc/metrics-textfile/user-check.prom")
        self.assertTrue(not state or state.startswith("Z"), f"cloudflared is still running: {state}\n{metrics}")
        self.assertIn('kind="tunnel"', metrics)
        self.assertIn('user="bob"', metrics)
        self.assertIn('program="cloudflared"', metrics)
        self.assertIn('action="stopped"', metrics)

        receiver = textwrap.dedent("""
            import http.server
            class Hook(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    with open("/tmp/user-check-webhook.log", "ab") as log:
                        log.write(body + b"\\n")
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
            http.server.HTTPServer(("127.0.0.1", 18081), Hook).serve_forever()
        """)
        self.assertEqual(self.ssh("front", "cat > /tmp/user-check-webhook.py", stdin=receiver).returncode, 0)
        self.on_front("rm -f /tmp/user-check-webhook.log")
        self.on_front("(nohup python3 /tmp/user-check-webhook.py >/dev/null 2>&1 &)")
        self.addCleanup(self.ssh, "front", "pkill -f /tmp/user-check-webhook.py")
        config["alerts"] = {"slack": True}
        cluster.write_text(yaml.safe_dump(config))
        (cluster.parent / ".env").write_text("NANOHPC_SLACK_WEBHOOK=http://127.0.0.1:18081/slack\n")
        result = self.deploy(self.state / "ssh_config", agent=False)
        self.assertEqual(result.returncode, 0, self.failure(result))
        self.assertEqual(self.on_front("systemctl is-active nanohpc-user-check-notify.timer").strip(), "active")
        posted = self.on_front("cat /tmp/user-check-webhook.log")
        for word in ("bob", "cloudflared", "front", "stopped"):
            self.assertIn(word, posted)

        config["user_check"] = {"allowed_tunnels": [{"user": "bob", "program": "cloudflared", "machines": ["front"]}]}
        cluster.write_text(yaml.safe_dump(config))
        result = self.deploy(self.state / "ssh_config", agent=False)
        self.assertEqual(result.returncode, 0, self.failure(result))
        self.on_front("sudo rm -f /tmp/cloudflared-bob-allowed.log /tmp/cloudflared-allowed.pid")
        self.on_front(
            "sudo -u bob sh -c 'nohup /tmp/cloudflared 120 >/tmp/cloudflared-bob-allowed.log 2>&1 & echo $!' > /tmp/cloudflared-allowed.pid"
        )
        allowed_pid = self.on_front("cat /tmp/cloudflared-allowed.pid").strip()
        self.assertEqual(self.ssh("front", "sudo systemctl restart nanohpc-user-check.service").returncode, 0)
        allowed_state = self.on_front(f"ps -p {allowed_pid} -o user=,stat= || true").strip()
        self.assertTrue(
            allowed_state.startswith("bob ") and "S" in allowed_state,
            f"allowed cloudflared was stopped: {allowed_state}",
        )
        allowed_metrics = self.on_front("cat /var/lib/nanohpc/metrics-textfile/user-check.prom")
        self.assertNotIn(f'id="front-{allowed_pid}-', allowed_metrics)
        self.on_front(f"sudo kill {allowed_pid} || true")


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimDeployTest(SimUsersBase):
    """End to end: `nanohpc sim deploy` sets up Slurm, users, SSH access, sudo, and Munge on the everyday
    cluster. Real Lima VMs. The first run builds Slurm on the front VM (tens of minutes); later runs use the
    cached packages. The users get a key generated for the test, so real logins can be tried."""

    def test_deploy(self) -> None:
        cluster, config, public_key = self.up_with_test_key()
        machines = config["machines"]
        # The first deploy leaves out gpu4i: a dry run on every machine, then the real run.
        cluster.write_text(yaml.safe_dump({**config, "machines": {n: m for n, m in machines.items() if n != "gpu4i"}}))
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        for machine in machines:
            if machine != "gpu4i":
                self.assertRegex(result.stdout, rf"\nDry run: {machine} would change \d+ things: ", machine)
        dry_run, marker, real_run = result.stdout.partition("Dry run passed: applying the changes.")
        self.assertTrue(marker)
        self.assertEqual(dry_run.count("PLAY RECAP"), 1)
        self.assertEqual(real_run.count("PLAY RECAP"), 1)
        # Then the cluster gains gpu4i, a new machine: its dry run passes too, and the real run sets it up.
        cluster.write_text(yaml.safe_dump(config))
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        self.assertRegex(result.stdout, r"\nDry run: gpu4i would change \d+ things: ")
        self.assertIn("Dry run passed: applying the changes.", result.stdout)

        with self.subTest("users and UIDs on every machine"):
            for machine in machines:
                passwd = self.ssh(machine, "getent passwd alice bob").stdout
                self.assertIn("alice:x:2000:2000:", passwd, machine)
                self.assertIn("bob:x:2001:2001:", passwd, machine)

        with self.subTest("key-only login: users on the front node, only administrators elsewhere"):
            for machine in machines:
                settings = self.ssh(machine, "sudo sshd -T").stdout.lower()
                self.assertIn("passwordauthentication no", settings, machine)
                self.assertEqual(self.as_user("alice", machine, "true", agent=False).returncode, 0, machine)
                bob = self.as_user("bob", machine, "true", agent=False)
                self.assertEqual(bob.returncode == 0, machine == "front", f"{machine}: {bob.stderr}")

        with self.subTest("administrators' forwarded key unlocks sudo, nobody else's"):
            # An empty stdin makes sudo fail at once if it would ask for a password.
            sudo = "sudo -S -p '' true </dev/null"
            self.assertEqual(self.as_user("alice", "gpu4", sudo, agent=True).returncode, 0)
            self.assertNotEqual(self.as_user("alice", "gpu4", sudo, agent=False).returncode, 0)
            self.assertNotEqual(self.as_user("bob", "front", sudo, agent=True).returncode, 0)
            self.assertEqual(self.ssh("gpu4", "test -e /etc/sudoers.d/nanohpc-admins").returncode, 1)

        with self.subTest("one Munge key across machines"):
            credential = self.ssh("front", "munge -n").stdout
            for machine in ("gpu4", "cpu1"):
                self.assertEqual(self.ssh(machine, "unmunge", stdin=credential).returncode, 0, machine)

        with self.subTest("Slurm nodes and partitions"):
            nodes = sorted(line for line in self.on_front("sinfo -h -N -o '%N %P %T'").split("\n") if line)
            expected = ["cpu1 main* idle", "gpu2 main* idle", "gpu4 interactive idle", "gpu4 main* idle"]
            self.assertEqual(nodes, sorted([*expected, "gpu4i interactive idle"]))
            self.assertIn("Gres=gpu:a6000:4", self.on_front("scontrol show node gpu4"))
            self.assertIn("26.05.4", self.on_front("sinfo --version"))

        with self.subTest("jobs run where they fit, with equal fair-share"):
            submit = "cd /tmp && sudo -u alice sbatch --parsable --wait -o /dev/null"
            gpu_job = self.on_front(f"{submit} -p main --gpus=1 --wrap hostname").strip()
            cpu_job = self.on_front(f"{submit} -p main -w cpu1 --wrap hostname").strip()
            for job, allowed in ((gpu_job, {"gpu4", "gpu2"}), (cpu_job, {"cpu1"})):
                # Accounting records a finished job a moment after `sbatch --wait` returns.
                command = (
                    f"for i in $(seq 20); do sacct -X -n -P -j {job} -o State | grep -q COMPLETED && break; sleep 1; done;"
                    f" sacct -X -n -P -j {job} -o State,NodeList"
                )
                state, node = self.on_front(command).strip().split("|")
                self.assertEqual(state, "COMPLETED", job)
                self.assertIn(node, allowed, job)
            shares = self.on_front("sshare -n -P -A labcluster -a -o User,RawShares")
            self.assertIn("alice|1", shares.split())
            self.assertIn("bob|1", shares.split())

        with self.subTest("/home shared from the front node, private, with quotas"):
            front = machines["front"]["address"]
            for machine in ("gpu4", "gpu2", "cpu1", "gpu4i"):
                self.assertEqual(
                    self.ssh(machine, "findmnt -n -o SOURCE,FSTYPE --mountpoint /home").stdout.split(),
                    [f"{front}:/home", "nfs4"],
                    machine,
                )
            self.assertNotIn("nfs", self.ssh("store", "findmnt -n -o FSTYPE --target /home").stdout)
            self.on_front("sudo -u alice sh -c 'echo shared > /home/alice/check'")
            self.assertEqual(self.ssh("gpu4", "sudo -u alice cat /home/alice/check").stdout.strip(), "shared")
            self.assertNotEqual(self.ssh("gpu4", "sudo -u bob ls /home/alice").returncode, 0)
            self.assertEqual(self.on_front("stat -c '%U %a' /home/alice").strip(), "alice 700")
            quotas = {
                row.split(",")[0]: row.split(",") for row in self.on_front("sudo repquota -u -O csv /home").splitlines()
            }
            header = quotas["User"]
            soft, hard = header.index("BlockSoftLimit"), header.index("BlockHardLimit")
            self.assertEqual(
                (quotas["alice"][soft], quotas["alice"][hard]), (str(300 * 1024 * 1024), str(400 * 1024 * 1024))
            )

        with self.subTest("local scratch on a disk or in an image, with per-user caches and cleanup"):
            self.assertEqual(self.ssh("gpu4", "findmnt -n -o SOURCE --mountpoint /scratch").stdout.strip(), "/dev/vdb")
            self.assertTrue(
                self.ssh("gpu2", "findmnt -n -o SOURCE --mountpoint /scratch").stdout.startswith("/dev/loop")
            )
            self.assertEqual(
                self.ssh("gpu2", "sudo stat -c %s /var/lib/nanohpc/scratch.img").stdout.strip(), str(2 * 1024**3)
            )
            self.assertEqual(self.ssh("gpu2", "stat -c '%U %a' /scratch/alice").stdout.strip(), "alice 700")
            cache = self.ssh("gpu2", "sudo -u alice bash -lc 'echo $UV_CACHE_DIR'").stdout.strip()
            self.assertEqual(cache, "/scratch/alice/uv-cache")
            self.assertEqual(
                self.ssh("gpu2", "systemctl is-enabled nanohpc-scratch-cleanup.timer").stdout.strip(), "enabled"
            )
            staged = "/scratch/staged/private/2000"
            self.ssh(
                "gpu2",
                f"sudo -u alice sh -c 'mkdir -p {staged}/old {staged}/new && touch -d \"20 days ago\" {staged}/old/.last-used && touch {staged}/new/.last-used'",
            )
            self.assertEqual(self.ssh("gpu2", "sudo systemctl start nanohpc-scratch-cleanup.service").returncode, 0)
            self.assertEqual(self.ssh("gpu2", f"sudo ls {staged}").stdout.split(), ["new"])

        with self.subTest("uv, cluster-health, stage-dataset, and cluster-submit"):
            self.assertTrue(self.ssh("gpu2", "uv --version").stdout.startswith("uv 0.12.21 "))
            for machine in machines:
                health = self.ssh(machine, "sudo cluster-health")
                self.assertEqual(health.returncode, 0, f"{machine}: {health.stdout}")
            script = (ROOT / "tests" / "stage_dataset_test.sh").read_text()
            staged = self.ssh("gpu2", "bash -s /usr/local/bin/stage-dataset", stdin=script)
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            project = textwrap.dedent("""\
                set -e
                rm -rf ~/proj && mkdir ~/proj && cd ~/proj && git init -q
                printf 'input data\\n' > input.txt
                cat > job.sh <<'JOB'
                #!/bin/bash
                #SBATCH --partition=main
                #SBATCH --nodelist=gpu2
                #SBATCH --wait
                #CLUSTER copy-back=results
                mkdir -p results
                pwd > results/where.txt
                cat input.txt >> results/where.txt
                JOB
                git add input.txt job.sh && git -c user.name=a -c user.email=a@a commit -qm job
                cluster-submit job.sh
            """)
            submitted = self.ssh("front", "cd /tmp && sudo -iu alice bash -s", stdin=project)
            self.assertEqual(submitted.returncode, 0, submitted.stdout + submitted.stderr)
            where = self.on_front("sudo -u alice cat /home/alice/proj/results/where.txt").splitlines()
            self.assertTrue(where[0].startswith("/scratch/alice/cluster-jobs/job-"), where)
            self.assertEqual(where[1], "input data")
            # The private scratch copy is removed after a successful job.
            self.assertEqual(self.ssh("gpu2", "sudo ls /scratch/alice/cluster-jobs").stdout.split(), [])

        with self.subTest("daily cleanup keeps a recent failed job copy using real Slurm accounting"):
            failed_job = textwrap.dedent("""\
                set -e
                cd /home/alice/proj
                cat > fail.sh <<'JOB'
                #!/bin/bash
                #SBATCH --partition=main
                #SBATCH --nodelist=gpu2
                #SBATCH --wait
                exit 17
                JOB
                git add fail.sh
                git -c user.name=a -c user.email=a@a commit -qm fail
                cluster-submit fail.sh
            """)
            failed = self.ssh("front", "cd /tmp && sudo -iu alice bash -s", stdin=failed_job)
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            folders = self.ssh(
                "gpu2", "sudo -u alice find /scratch/alice/cluster-jobs -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'"
            ).stdout.splitlines()
            self.assertEqual(len(folders), 1, folders)
            match = re.fullmatch(r"job-(\d+)-[A-Za-z0-9_]+", folders[0])
            self.assertIsNotNone(match, folders[0])
            assert match is not None
            for _ in range(20):
                accounting = self.ssh("gpu2", f"sudo -u alice sacct -X -n -P -j {match[1]} --format=State,End")
                if re.search(r"FAILED\|\d{4}-\d\d-\d\dT", accounting.stdout):
                    break
                time.sleep(1)
            else:
                self.fail(f"Slurm did not record the failed job's end time: {accounting.stdout} {accounting.stderr}")
            service = self.ssh("gpu2", "sudo systemctl start nanohpc-scratch-cleanup.service")
            self.assertEqual(service.returncode, 0, service.stderr)
            self.assertEqual(
                self.ssh("gpu2", f"sudo -u alice test -d /scratch/alice/cluster-jobs/{folders[0]}").returncode, 0
            )

        with self.subTest("metrics from every machine over TLS, fake GPU readings, daily rules, history"):

            def query(expression: str) -> list[dict[str, Any]]:
                encoded = urllib.parse.quote(expression)
                answer = self.on_front(f"curl -sf 'http://127.0.0.1:9090/api/v1/query?query={encoded}'")
                return json.loads(answer)["data"]["result"]

            up = {series["metric"]["machine"]: series["value"][1] for series in query('up{job=~"node.*"}')}
            self.assertEqual(up, {machine: "1" for machine in machines})
            gpus = query("cluster_gpu_utilization_percent")
            self.assertEqual(sorted({series["metric"]["machine"] for series in gpus}), ["gpu2", "gpu4", "gpu4i"])
            self.assertEqual(len(gpus), 10)
            specs = {series["metric"]["machine"]: series["value"][1] for series in query("cluster_machine_gpu_count")}
            self.assertEqual(specs, {"front": "0", "gpu4": "4", "gpu2": "2", "cpu1": "0", "gpu4i": "4", "store": "0"})
            self.assertEqual(self.on_front("curl -sf http://127.0.0.1:9091/-/ready").strip() != "", True)
            # node_exporter's sandbox must not make filesystems look read-only.
            readonly = {
                series["metric"]["machine"]: series["value"][1]
                for series in query('node_filesystem_readonly{mountpoint="/"}')
            }
            self.assertEqual(readonly, {machine: "0" for machine in machines})
            # Right after a deploy the front node's health report has no warnings.
            report = self.ssh("front", "sudo cluster-health").stdout
            self.assertNotIn("WARN", report, report)
            # Without the front node's client certificate, a machine's exporter refuses the connection.
            gpu4 = machines["gpu4"]["address"]
            self.assertNotEqual(self.ssh("front", f"curl -sk --max-time 5 https://{gpu4}:9100/metrics").returncode, 0)
            # The daily summary rules pass their promtool tests (promtool is installed on the front node).
            # Copied with the repository's layout, since the test file names the rules file by a relative path.
            for relative in ("src/nanohpc/files/prometheus-daily-rules.yml", "tests/prometheus_daily_rules_test.yml"):
                copied = self.ssh("front", f"mkdir -p /tmp/rules/$(dirname {relative}) && cat > /tmp/rules/{relative}",
                                  stdin=(ROOT / relative).read_text())  # fmt: skip
                self.assertEqual(copied.returncode, 0, copied.stderr)
            promtool = self.on_front("ls -d /opt/nanohpc-metrics/prometheus-*/promtool").strip()
            rules = self.ssh("front", f"cd /tmp/rules/tests && {promtool} test rules prometheus_daily_rules_test.yml")
            self.assertEqual(rules.returncode, 0, rules.stdout + rules.stderr)

        with self.subTest("website over HTTPS with a certificate from the test Let's Encrypt, renewed"):
            self.check_website(config)
            host = config["cluster"]["website"]["hostname"]
            serial = f"openssl s_client -connect 127.0.0.1:443 -servername {host} </dev/null 2>/dev/null | openssl x509 -noout -serial"
            before = self.on_front(serial)
            renew = self.ssh(
                "front",
                "sudo REQUESTS_CA_BUNDLE=/etc/nanohpc/test-acme/ca.pem /opt/nanohpc-certbot/current/bin/certbot renew --force-renewal "
                "--non-interactive --config-dir /etc/nanohpc/website/letsencrypt --work-dir /var/lib/nanohpc/certbot "
                "--logs-dir /var/log/nanohpc-certbot --deploy-hook /usr/local/sbin/nanohpc-website-reload",
            )
            self.assertEqual(renew.returncode, 0, renew.stdout + renew.stderr)
            time.sleep(2)
            self.assertNotEqual(self.on_front(serial), before)
            self.assertEqual(self.on_front("systemctl is-active nanohpc-website-certificate.timer").strip(), "active")
            # The renewal service itself runs (nothing is due, so it renews nothing).
            self.on_front("sudo systemctl start nanohpc-website-certificate.service")

        with self.subTest("Grafana: six dashboards for anonymous viewers, read-only, on localhost only"):
            grafana = (
                f"http://127.0.0.1:3000{config['cluster']['website'].get('path', '/cluster/').rstrip('/')}/grafana"
            )
            found = json.loads(self.on_front(f"curl -sf '{grafana}/api/search?type=dash-db'"))
            self.assertEqual(
                sorted(item["uid"] for item in found),
                sorted(
                    f"nanohpc-{name}" for name in ["history", "machines", "overview", "queue-history", "queue", "usage"]
                ),
            )
            for dashboard in found:
                self.assertTrue(dashboard["title"].startswith(f"{config['cluster']['name']}: "), dashboard)
                self.assertEqual(dashboard["folderTitle"], config["cluster"]["name"])
            # Both data sources answer through Grafana for an anonymous viewer.
            for source in ("cluster-detail", "cluster-history"):
                answer = self.on_front(f"curl -sf '{grafana}/api/datasources/proxy/uid/{source}/api/v1/query?query=1'")
                self.assertEqual(json.loads(answer)["status"], "success")
            # Anonymous viewers cannot save a dashboard.
            save = self.on_front(
                f"curl -s -o /dev/null -w '%{{http_code}}' -X POST -H 'Content-Type: application/json' "
                f'-d \'{{"dashboard": {{"title": "x"}}}}\' {grafana}/api/dashboards/db'
            )
            self.assertIn(save.strip(), {"401", "403"})
            # Nor create snapshots or annotations.
            for path, body in (("snapshots", '{"dashboard": {}}'), ("annotations", '{"text": "x"}')):
                refused = self.on_front(
                    f"curl -s -o /dev/null -w '%{{http_code}}' -X POST -H 'Content-Type: application/json' "
                    f"-d '{body}' {grafana}/api/{path}"
                )
                self.assertIn(refused.strip(), {"401", "403", "404"}, path)
            # The machine panels (CPU, memory, disks) cover every machine, the front node included.
            panels = json.loads((ROOT / "src/nanohpc/files/grafana/machines.json").read_text())["panels"]
            for panel in panels:
                for target in panel.get("targets", []):
                    if target.get("expr", "").startswith(("100 * (1 - avg", "node_")):
                        shown = {series["metric"]["machine"] for series in query(target["expr"])}
                        self.assertEqual(shown, set(machines), f"{panel['title']}: {target['expr']}")
            front = machines["front"]["address"]
            self.assertNotEqual(self.ssh("gpu4", f"curl -s --max-time 5 http://{front}:3000/").returncode, 0)

        with self.subTest("status snapshot every 30 seconds: machines, jobs, quotas, Slurm figures"):

            def snapshot() -> dict[str, Any]:
                return json.loads(self.on_front("cat /var/lib/nanohpc/monitor/status.json"))

            job = self.on_front(
                "cd /tmp && sudo -u alice sbatch --parsable -p main -t 5 -o /dev/null --wrap 'sleep 300'"
            ).strip()
            self.addCleanup(self.ssh, "front", f"sudo scancel {job}")
            first = snapshot()
            for _ in range(20):
                status = snapshot()
                healthy = {node["name"]: node["health"] for node in status["nodes"]}
                if status["generated_at"] != first["generated_at"] and job in {
                    str(item["id"]) for item in status["jobs"]
                } and set(healthy.values()) == {"Healthy"}:  # fmt: skip
                    break
                time.sleep(6)
            self.assertNotEqual(status["generated_at"], first["generated_at"])
            self.assertEqual(status["refresh_seconds"], 30)
            # Two refreshes in a row are about 30 seconds apart.
            stamps = [status["generated_at"]]
            for _ in range(60):
                time.sleep(2)
                latest = snapshot()["generated_at"]
                if latest != stamps[-1]:
                    stamps.append(latest)
                if len(stamps) == 3:
                    break
            self.assertEqual(len(stamps), 3, stamps)
            gap = (datetime.fromisoformat(stamps[2]) - datetime.fromisoformat(stamps[1])).total_seconds()
            self.assertTrue(25 <= gap <= 40, stamps)
            self.assertIn(job, {str(item["id"]) for item in status["jobs"]})
            self.assertEqual(
                {node["name"]: node["health"] for node in status["nodes"]},
                {machine: "Healthy" for machine in machines},
                json.dumps(status["nodes"], indent=1),
            )
            roles = {node["name"]: node["role"] for node in status["nodes"]}
            self.assertEqual(roles["front"], "Front node")
            self.assertEqual(roles["store"], "Storage")
            alice = next(card for card in status["users"] if card["user"] == "alice")
            self.assertEqual(alice["home"]["soft_bytes"], config["home"]["quota_soft_gb"] * 1024**3)
            self.assertIn(
                f"# {config['cluster']['name']} machines", self.on_front("cat /var/lib/nanohpc/monitor/machines.md")
            )
            # The Slurm figures reach Prometheus (the job is running or waiting).
            for _ in range(20):
                jobs = sum(
                    float(series["value"][1])
                    for name in ("cluster_running_jobs", "cluster_pending_jobs")
                    for series in query(name)
                )
                if jobs >= 1:
                    break
                time.sleep(5)
            self.assertGreaterEqual(jobs, 1)

        with self.subTest("nightly /home mirror to the backup machine: owners kept, deletions mirrored, restore"):
            backup = config["machines"]["store"]["backup"]["path"].rstrip("/") + "/home"
            self.on_front(
                "sudo -u alice sh -c 'mkdir -p ~/keep && echo precious > ~/keep/data.txt && echo old > ~/gone.txt'"
            )
            self.on_front("sudo systemctl start nanohpc-backup.service")
            self.assertIn(
                "cluster_backup_last_exit_code 0", self.on_front("cat /var/lib/nanohpc/metrics-textfile/backup.prom")
            )
            stored = self.ssh("store", f"sudo cat {backup}/alice/keep/data.txt")
            self.assertEqual(stored.stdout.strip(), "precious", stored.stderr)
            # The unprivileged account owns the copy; the real owner is kept as an extended attribute.
            self.assertEqual(
                self.ssh("store", f"sudo stat -c %U {backup}/alice/keep/data.txt").stdout.strip(), "nanohpc-backup"
            )
            attributes = self.ssh(
                "store", f"sudo python3 -c 'import os; print(os.listxattr(\"{backup}/alice/keep/data.txt\"))'"
            )
            self.assertIn("user.rsync.%stat", attributes.stdout, attributes.stderr)
            # One mirror: a file deleted from /home is deleted from the copy at the next run.
            self.on_front("sudo -u alice rm ~alice/gone.txt && sudo systemctl start nanohpc-backup.service")
            self.assertEqual(self.ssh("store", f"sudo test -e {backup}/alice/gone.txt").returncode, 1)
            # Restore one user's folder with the same key: owners come back.
            ssh_options = "ssh -i /etc/nanohpc/backup/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=/etc/nanohpc/backup/known_hosts"
            store = config["machines"]["store"]["address"]
            self.on_front(
                f"sudo rsync -a --numeric-ids --rsync-path='rsync --fake-super' -e '{ssh_options}' "
                f"nanohpc-backup@{store}:{backup}/alice/keep/ /tmp/restored/"
            )
            self.assertEqual(self.on_front("stat -c %U /tmp/restored/data.txt").strip(), "alice")
            # The key can only run rsync on the backup folder.
            shell = self.ssh("front", f"sudo {ssh_options} nanohpc-backup@{store} id")
            self.assertNotEqual(shell.returncode, 0)
            self.assertNotIn("uid=", shell.stdout)
            self.assertEqual(self.on_front("systemctl is-active nanohpc-backup.timer").strip(), "active")

        with self.subTest("partition rules and limits"):
            shell = self.run_command(
                "ssh", "-tt", "-F", str(self.state / "ssh_config"), "front",
                "cd /tmp && sudo -u alice srun -p interactive --pty bash -l", stdin="exit\n",
            )  # fmt: skip
            self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
            refused = self.ssh("front", "cd /tmp && sudo -u alice srun -p main hostname")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("main accepts submitted background jobs only", refused.stderr)
            refused = self.ssh("front", "cd /tmp && sudo -u alice srun -p interactive hostname")
            self.assertIn("interactive accepts only the interactive shell", refused.stderr)
            refused = self.ssh("front", "cd /tmp && sudo -u alice sbatch -p main --gpus=7 --wrap hostname")
            self.assertIn("main allows at most 6 GPUs", refused.stderr)
            self.assertEqual(
                self.on_front("sacctmgr -n -P show qos interactive format=MaxTRESPU").strip(), "gres/gpu=2"
            )

        with self.subTest("a second deploy changes nothing"):
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assert_no_changes(result, len(machines))

        with self.subTest("--dry-run shows changes in cluster.yml, and changes nothing on any machine"):
            before = {machine: self.machine_state(machine) for machine in machines}
            original = cluster.read_text()
            changed = yaml.safe_load(original)
            changed["users"].append({"name": "carol", "uid": 2005, "ssh_keys": [public_key]})
            changed["home"]["quota_soft_gb"] = changed["home"]["quota_soft_gb"] - 10
            changed["partitions"]["interactive"]["max_time"] = "06:00:00"
            cluster.write_text(yaml.safe_dump(changed))
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--dry-run")
            cluster.write_text(original)
            self.assertEqual(result.returncode, 0, self.failure(result))
            front = next(line for line in result.stdout.splitlines() if line.startswith("Dry run: front "))
            for task in (
                "accounts : Create the users with their fixed UIDs",
                "home_server : Set each user's home quota",
                "slurm_controller : Set each partition's time limit",
            ):
                self.assertIn(task, front)
            self.assertIn("Dry run passed. Nothing was changed (--dry-run).", result.stdout)
            self.assertEqual(result.stdout.count("PLAY RECAP"), 1, "only the dry run ran")
            # Read directly on the machines: nothing changed (apt's package lists are not compared).
            for machine in machines:
                self.assertEqual(self.machine_state(machine), before[machine], machine)

        with self.subTest("nanohpc check: no problems and nothing changed, then a stopped service is named"):
            check = ("uv", "run", "nanohpc", "check", str(cluster), "--ssh-config", str(self.state / "ssh_config"))
            before = {machine: self.machine_state(machine) for machine in machines}
            result = self.run_command(*check)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("No problems.", result.stdout)
            rows = {line.split()[0]: line for line in result.stdout.splitlines()[1 : len(machines) + 1]}
            self.assertEqual(sorted(rows), sorted(machines), result.stdout)
            for row in rows.values():
                self.assertTrue(row.endswith(f"  {metadata.version('nanohpc')}"), row)
                # Right after a deploy, the front node can warn about metrics that are not fresh yet.
                self.assertRegex(row, r"  (ok|WARN) \(sudo\)  ")
            self.assertIn("  4/4 (devices)  ", rows["gpu4"])
            self.assertIn("  0/0  ", rows["cpu1"])
            for machine in machines:
                self.assertEqual(self.machine_state(machine), before[machine], machine)
            self.assertEqual(self.ssh("cpu1", "sudo systemctl stop munge").returncode, 0)
            result = self.run_command(*check)
            self.ssh("cpu1", "sudo systemctl start munge")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertTrue(result.stdout.splitlines()[1].startswith("cpu1 "), result.stdout)
            self.assertIn("\n  cpu1: cluster-health: FAIL service munge\n", result.stdout)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimRedeployTest(SimUsersBase):
    """The safety checks that each need another deploy, on the everyday cluster: a missing certificate issued
    again, a drained node as a warning only, a deploy by an administrator's forwarded key, the stop with no key
    and no terminal, administrators' root login (also with the home machine's NFS server stopped), the dry run's
    stop on a key in /root/.ssh that the deploy would make stop working, a removed user's login taken away, and a
    UID conflict that leaves that machine out after its dry run. Run when accounts, SSH, sudo, preflight,
    deploy.py, or the certificates change, and before the release. Real Lima VMs."""

    def test_redeploys(self) -> None:
        cluster, _, public_key = self.up_and_deploy()

        with self.subTest("a missing metrics certificate is issued again by the next deploy"):
            self.assertEqual(self.ssh("gpu2", "sudo rm /etc/nanohpc/metrics-tls/node.crt").returncode, 0)
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertEqual(self.ssh("gpu2", "test -s /etc/nanohpc/metrics-tls/node.crt").returncode, 0)

        with self.subTest("a drained node is a warning in the health report, not a failed deploy"):
            self.on_front("sudo scontrol update nodename=cpu1 state=drain reason=maintenance-test")
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertIn(
                "WARN  no Slurm node is down, drained, not responding, or in maintenance (cpu1", result.stdout
            )
            self.on_front("sudo scontrol update nodename=cpu1 state=resume")

        with self.subTest("a later deploy by an administrator through the forwarded key, without a password"):
            result = self.deploy(self.ssh_config_for("alice"), agent=True)
            self.assertEqual(result.returncode, 0, self.failure(result))
            # The VM's default account that deployed first is still allowed.
            self.assertEqual(self.ssh("gpu4", "true").returncode, 0)

        with self.subTest("with no key for sudo and no terminal, deploy stops before any change"):
            result = self.deploy(self.ssh_config_for("alice"), agent=False)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Nothing was changed: sudo needs a password on", result.stderr)
            self.assertNotIn("PLAY", result.stdout)

        with self.subTest("administrators log in as root with their keys everywhere, also with /home down"):
            # bob (not an administrator) gets a key of his own.
            bob_key = self.keys / "bob"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nanohpc-test-bob", "-f", str(bob_key)], check=True)  # fmt: skip
            bob_public = (self.keys / "bob.pub").read_text().strip()
            with_bob_key = yaml.safe_load(cluster.read_text())
            for user in with_bob_key["users"]:
                if user["name"] == "bob":
                    user["ssh_keys"] = [bob_public]
            cluster.write_text(yaml.safe_dump(with_bob_key))
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assertEqual(result.returncode, 0, self.failure(result))
            as_root_bob = self.ssh_config_with_key("root", bob_key)

            def bob_as_root(machine: str) -> subprocess.CompletedProcess[str]:
                return self.run_command("ssh", "-F", str(as_root_bob), "-o", "BatchMode=yes", machine, "true")

            self.assertEqual(self.bob_login(bob_key).returncode, 0)  # bob's key works
            for machine in with_bob_key["machines"]:
                root = self.as_user("root", machine, "whoami", agent=False)
                self.assertEqual((root.returncode, root.stdout.strip()), (0, "root"), f"{machine}: {root.stderr}")
                self.assertNotEqual(bob_as_root(machine).returncode, 0, machine)
            # A key in /root/.ssh/authorized_keys is ignored: sshd reads root's keys only from nanoHPC's file.
            plant = (
                f"sudo install -d -m 0700 /root/.ssh && echo '{bob_public}' | sudo tee -a /root/.ssh/authorized_keys"
            )
            self.assertEqual(self.ssh("gpu4", plant).returncode, 0)
            self.assertNotEqual(bob_as_root("gpu4").returncode, 0)
            self.ssh("gpu4", "sudo sed -i '/nanohpc-test-bob$/d' /root/.ssh/authorized_keys")
            # With the home machine's NFS server stopped, root login on a compute node still works, quickly.
            self.on_front("sudo systemctl stop nfs-server")
            try:
                started = time.monotonic()
                login = subprocess.run(
                    ["ssh", "-F", str(self.ssh_config_for("root")), "-o", "BatchMode=yes", "gpu4", "whoami"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                    env={key: value for key, value in os.environ.items() if key != "SSH_AUTH_SOCK"},
                )
                seconds = time.monotonic() - started
            finally:
                self.on_front("sudo systemctl start nfs-server")
            self.assertEqual(login.returncode, 0, login.stderr)
            self.assertEqual(login.stdout.strip(), "root")
            self.assertLess(seconds, 20)
            # /home answers again on gpu4 before the next deploy (the NFS client retries on its own).
            wait = "for i in $(seq 24); do timeout -s KILL 10 sudo -u alice ls /home/alice >/dev/null && exit 0; sleep 5; done; exit 1"  # fmt: skip
            self.assertEqual(self.ssh("gpu4", wait).returncode, 0)

        with self.subTest("the dry run stops on a key in /root/.ssh that the deploy would make stop working"):
            # cpu1 as before nanoHPC's root setting: without the Match block, sshd reads root's keys from /root/.ssh.
            outsider_key = self.keys / "outsider"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nanohpc-test-outsider", "-f", str(outsider_key)], check=True)  # fmt: skip
            outsider_public = (self.keys / "outsider.pub").read_text().strip()
            listing = subprocess.run(
                ["ssh-keygen", "-l", "-f", str(self.keys / "outsider.pub")], capture_output=True, text=True, check=True
            )
            outsider_fingerprint = listing.stdout.split()[1]
            setting_off = "sudo sed -i '/^Match User root/,$d' /etc/ssh/sshd_config.d/10-nanohpc.conf && sudo systemctl restart ssh"  # fmt: skip
            self.assertEqual(self.ssh("cpu1", setting_off).returncode, 0)
            plant = f"sudo install -d -m 0700 /root/.ssh && echo '{outsider_public}' | sudo tee -a /root/.ssh/authorized_keys"  # fmt: skip
            self.assertEqual(self.ssh("cpu1", plant).returncode, 0)
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--dry-run")
            self.assertEqual(result.returncode, 3, self.failure(result))
            self.assertIn("Dry run: cpu1 failed: ", result.stdout)
            self.assertIn(
                f"/root/.ssh/authorized_keys: ssh-ed25519 {outsider_fingerprint} nanohpc-test-outsider", result.stdout
            )
            self.assertIn("These keys will stop working for root after this deploy", result.stdout)
            self.assertIn("Dry run failed on cpu1. Nothing was changed (--dry-run).", result.stderr)
            # Without that key, a deploy passes on cpu1 and puts nanoHPC's root setting back.
            self.ssh("cpu1", "sudo sed -i '/nanohpc-test-outsider$/d' /root/.ssh/authorized_keys")
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assertEqual(result.returncode, 0, self.failure(result))
            address = yaml.safe_load(cluster.read_text())["machines"]["cpu1"]["address"]
            effective = self.ssh("cpu1", f"sudo /usr/sbin/sshd -T -C user=root,host=cpu1,addr={address}")
            self.assertIn("authorizedkeysfile /etc/ssh/authorized_keys/root", effective.stdout.splitlines())

        with self.subTest("removing a user from cluster.yml takes away their login"):
            bob_key = self.keys / "bob"
            self.assertEqual(self.bob_login(bob_key).returncode, 0)
            without_bob = yaml.safe_load(cluster.read_text())
            without_bob["users"] = [user for user in without_bob["users"] if user["name"] != "bob"]
            cluster.write_text(yaml.safe_dump(without_bob))
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertNotEqual(self.bob_login(bob_key).returncode, 0)
            self.assertEqual(self.ssh("front", "test -e /etc/ssh/authorized_keys/bob").returncode, 1)

        with self.subTest("a UID conflict leaves that machine out of the deploy, unchanged"):
            self.assertEqual(self.ssh("gpu2", "sudo useradd -u 3005 carol").returncode, 0)
            changed = yaml.safe_load(cluster.read_text())
            changed["users"] += [
                {"name": "carol", "uid": 2005, "ssh_keys": [public_key]},
                {"name": "dave", "uid": 2006, "ssh_keys": [public_key]},
            ]
            cluster.write_text(yaml.safe_dump(changed))
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assert_left_out(result, "gpu2")
            self.assertIn(
                "gpu2: user carol has UID 3005 and primary group ID 3005, but cluster.yml says 2005", result.stdout
            )
            self.assertIn("carol:x:3005:", self.ssh("gpu2", "getent passwd carol").stdout)
            # gpu2 was left unchanged: the other new user was not created there, but was elsewhere.
            self.assertEqual(self.ssh("gpu2", "getent passwd dave").returncode, 2)
            for machine in ("gpu4", "front"):
                self.assertIn("dave:x:2006:", self.ssh(machine, "getent passwd dave").stdout, machine)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimPartialDeployTest(SimUsersBase):
    """Partial deploys (`--only`) on the everyday cluster, first deployed without gpu4i: a new user with --only
    users (while a partition change waits in cluster.yml), the partition change with --only partitions, gpu4i with
    --only node gpu4i, then a full deploy that finds nothing left to change. Real Lima VMs."""

    def only(self, *words: str) -> subprocess.CompletedProcess[str]:
        """Run `nanohpc sim deploy --only ...`."""
        return self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--only", *words)

    @staticmethod
    def dry_run_lines(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
        """Return the dry run's summary, one line per machine, by machine name."""
        lines = [line for line in result.stdout.splitlines() if line.startswith("Dry run: ")]
        return {line.split()[2]: line for line in lines if " would change " in line or " has nothing " in line}

    def test_partial_deploys(self) -> None:
        cluster, config, public_key = self.up_with_test_key()
        machines = config["machines"]
        changed = yaml.safe_load(yaml.safe_dump(config))
        changed["machines"].pop("gpu4i")
        cluster.write_text(yaml.safe_dump(changed))
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))

        with self.subTest("--only users adds a user everywhere; a partition change in cluster.yml waits"):
            changed["users"].append({"name": "carol", "uid": 2005, "ssh_keys": [public_key]})
            changed["partitions"]["main"]["max_time"] = "12:00:00"
            cluster.write_text(yaml.safe_dump(changed))
            # A users-only update must install the command and widen the old service's write path.
            self.assertEqual(self.ssh("gpu2", "sudo rm /usr/local/bin/scratch-job-cleanup").returncode, 0)
            self.assertEqual(
                self.ssh(
                    "gpu2",
                    "sudo sed -i 's@ReadWritePaths=/scratch$@ReadWritePaths=/scratch/staged /scratch/locks@' "
                    "/etc/systemd/system/nanohpc-scratch-cleanup.service",
                ).returncode,
                0,
            )
            result = self.only("users")
            self.assertEqual(result.returncode, 0, self.failure(result))
            summary = self.dry_run_lines(result)
            self.assertEqual(sorted(summary), ["cpu1", "front", "gpu2", "gpu4", "store"], result.stdout[-4000:])
            for line in summary.values():
                self.assertRegex(line, r"^Dry run: \S+ would change \d+ things?: ", line)
                for other in ("slurm.conf", "job submission", "QoS", "/etc/hosts", "exports"):
                    self.assertNotIn(other, line)
            self.assertIn("accounts : Create the users with their fixed UIDs", summary["store"])
            self.assertIn("scratch : Create each user's private scratch folder", summary["gpu2"])
            self.assertIn("slurm_controller : Add the users as job submitters", summary["front"])
            self.assertIn("home_server : Set each user's home quota", summary["front"])
            for machine in changed["machines"]:
                self.assertIn("carol:x:2005:2005:", self.ssh(machine, "getent passwd carol").stdout, machine)
            quotas = self.on_front("sudo repquota -u -O csv /home")
            self.assertIn("carol,", quotas)
            self.assertIn(f",{300 * 1024 * 1024},{400 * 1024 * 1024},", quotas.split("carol,", 1)[1].splitlines()[0])
            self.assertIn("carol", self.on_front("sacctmgr -n -P show assoc user=carol format=User"))
            self.assertEqual(self.ssh("gpu2", "stat -c '%U %a' /scratch/carol").stdout.strip(), "carol 700")
            self.assertEqual(self.ssh("gpu2", "test -x /usr/local/bin/scratch-job-cleanup").returncode, 0)
            self.assertIn(
                "ReadWritePaths=/scratch\n",
                self.ssh("gpu2", "sudo cat /etc/systemd/system/nanohpc-scratch-cleanup.service").stdout,
            )
            self.assertEqual(self.as_user("carol", "front", "true", agent=False).returncode, 0)
            # The partition was not changed.
            self.assertIn("MaxTime=1-00:00:00", self.on_front("scontrol show partition main"))

        with self.subTest("--only partitions changes a partition's time limit on the Slurm machines"):
            result = self.only("partitions")
            self.assertEqual(result.returncode, 0, self.failure(result))
            summary = self.dry_run_lines(result)
            self.assertEqual(sorted(summary), ["cpu1", "front", "gpu2", "gpu4"], result.stdout[-4000:])
            self.assertIn("slurm_controller : Install slurm.conf and cgroup.conf", summary["front"])
            self.assertIn("slurm_compute : Install slurm.conf and cgroup.conf", summary["cpu1"])
            self.assertIn("MaxTime=12:00:00", self.on_front("scontrol show partition main"))
            self.assertEqual(self.on_front("sinfo -h -p main -o %l").split()[0], "12:00:00")
            self.assertIn("12:00:00", self.on_front("sacctmgr -n -P show qos main format=MaxWall"))

        with self.subTest("--only node gpu4i adds a machine to the cluster"):
            changed["machines"]["gpu4i"] = machines["gpu4i"]
            cluster.write_text(yaml.safe_dump(changed))
            # Every other part needs every machine deployed first.
            refused = self.only("users")
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("gpu4i has not been deployed yet: deploy it first with --only node gpu4i", refused.stderr)
            self.assertNotIn("PLAY", refused.stdout)
            result = self.only("node", "gpu4i")
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertRegex(result.stdout, r"\nDry run: gpu4i would change \d+ things: ")
            nodes = self.on_front("sinfo -h -N -n gpu4i -o '%N %P %T'").split("\n")
            self.assertEqual([line for line in nodes if line], ["gpu4i interactive idle"])
            # The interactive partition accepts only the interactive shell.
            shell = self.run_command(
                "ssh", "-tt", "-F", str(self.state / "ssh_config"), "front",
                "cd /tmp && sudo -u alice srun -p interactive -w gpu4i --gpus=1 --pty bash -l", stdin="hostname\nexit\n",
            )  # fmt: skip
            self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
            self.assertIn(self.ssh("gpu4i", "hostname").stdout.strip(), shell.stdout)
            front = machines["front"]["address"]
            self.assertEqual(
                self.ssh("gpu4i", "findmnt -n -o SOURCE,FSTYPE --mountpoint /home").stdout.split(),
                [f"{front}:/home", "nfs4"],
            )
            self.assertIn("gpu4i", self.ssh("gpu2", "getent hosts gpu4i").stdout)
            query = urllib.parse.quote('up{machine="gpu4i"}')
            answer = json.loads(self.on_front(f"curl -sf 'http://127.0.0.1:9090/api/v1/query?query={query}'"))
            self.assertEqual([series["value"][1] for series in answer["data"]["result"]], ["1"])
            website = config["cluster"]["website"]
            host, path = website["hostname"], website.get("path", "/cluster/")
            status = json.loads(
                self.on_front(f"curl -sfk --resolve {host}:443:127.0.0.1 https://{host}{path}data/status.json")
            )
            self.assertIn("gpu4i", [node["name"] for node in status["nodes"]])

        with self.subTest("a full deploy afterwards changes nothing"):
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assert_no_changes(result, len(machines))


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimAlertsTest(SimUsersBase):
    """The slow alert checks, on the everyday cluster: stale GPU readings, and Slack alerts with a stand-in Slack
    server (a failing check, then its recovery; up to half an hour of waiting). Run when alerts or metrics
    change, and before the release. Real Lima VMs."""

    def test_alerts(self) -> None:
        cluster, _, _ = self.up_and_deploy()

        with self.subTest("GPU readings that stop arriving are reported as stale"):
            self.assertEqual(self.ssh("gpu2", "sudo systemctl stop nanohpc-gpu-metrics.timer").returncode, 0)
            time.sleep(130)
            report = self.ssh("front", "sudo cluster-health").stdout
            self.assertIn("WARN  GPU readings are fresh (under 2 minutes old) (gpu2)", report, report)
            self.assertEqual(self.ssh("gpu2", "sudo systemctl start nanohpc-gpu-metrics.timer").returncode, 0)

        with self.subTest("Slack alerts from the front node: a failing check on a machine, and its recovery"):
            # A stand-in for Slack on the front node records what Alertmanager posts.
            receiver = textwrap.dedent("""
                import http.server
                class Hook(http.server.BaseHTTPRequestHandler):
                    def do_POST(self):
                        body = self.rfile.read(int(self.headers["Content-Length"]))
                        with open("/tmp/slack.log", "ab") as log:
                            log.write(body + b"\\n")
                        # Like Slack's incoming webhooks: 200 with the body "ok".
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Content-Length", "2")
                        self.end_headers()
                        self.wfile.write(b"ok")
                http.server.HTTPServer(("127.0.0.1", 18080), Hook).serve_forever()
            """)
            self.assertEqual(self.ssh("front", "cat > /tmp/slack.py", stdin=receiver).returncode, 0)
            self.on_front("rm -f /tmp/slack.log; (nohup python3 /tmp/slack.py >/dev/null 2>&1 &)")
            self.addCleanup(self.ssh, "front", "pkill -f /tmp/slack.py")
            with_slack = yaml.safe_load(cluster.read_text())
            with_slack["alerts"]["slack"] = True
            cluster.write_text(yaml.safe_dump(with_slack))
            (cluster.parent / ".env").write_text("NANOHPC_SLACK_WEBHOOK=http://127.0.0.1:18080/slack\n")
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assertEqual(result.returncode, 0, self.failure(result))
            # The webhook is a secret: not in the deploy's output, its variables, or a file left behind.
            self.assertNotIn("18080", result.stdout + result.stderr)
            work = Path.home() / ".cache/nanohpc/clusters/labcluster"
            self.assertNotIn("18080", (work / "vars.json").read_text())
            self.assertFalse((work / "secrets.json").exists())

            def posted(*texts: str) -> bool:
                """Whether one message posted to the stand-in Slack contains all these texts."""
                messages = self.ssh("front", "cat /tmp/slack.log 2>/dev/null").stdout.splitlines()
                return any(all(text in message for text in texts) for message in messages)

            # A check starts failing on cpu1: one problem message.
            self.assertEqual(
                self.ssh(
                    "cpu1",
                    "sudo systemctl stop nanohpc-scratch-cleanup.timer && sudo systemctl start nanohpc-health.service",
                ).returncode,
                0,
            )
            for _ in range(90):
                if posted("Problem", "scratch cleanup timer", "cpu1"):
                    break
                time.sleep(10)
            self.assertTrue(
                posted("Problem", "scratch cleanup timer", "cpu1"), self.ssh("front", "cat /tmp/slack.log").stdout
            )
            # It recovers: one resolved message.
            self.assertEqual(
                self.ssh(
                    "cpu1",
                    "sudo systemctl start nanohpc-scratch-cleanup.timer && sudo systemctl start nanohpc-health.service",
                ).returncode,
                0,
            )
            for _ in range(90):
                if posted("Resolved", "scratch cleanup timer", "cpu1"):
                    break
                time.sleep(10)
            self.assertTrue(
                posted("Resolved", "scratch cleanup timer", "cpu1"), self.ssh("front", "cat /tmp/slack.log").stdout
            )


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimAutoDeployTest(SimUsersBase):
    """Automatic deploys: the front node deploys the whole cluster from a configuration repository by itself.
    The repository is a bare Git repository of the cluster user bob on the front node, standing in for GitHub;
    the front node reads it with its own read-only key. Real Lima VMs."""

    def setUp(self) -> None:
        self.sim = SIM / "auto-deploy.yml"
        self.state = ROOT / ".nanohpc-sim" / "auto-deploy"
        super().setUp()

    def push(self, config: dict[str, Any], message: str) -> str:
        """Commit cluster.yml to the stand-in repository as bob; return the new commit."""
        script = textwrap.dedent(f"""
            set -e
            rm -rf /tmp/config-work && git clone -q /home/bob/config.git /tmp/config-work 2>/dev/null
            cd /tmp/config-work
            cat > cluster.yml
            git add cluster.yml
            git -c user.name=bob -c user.email=bob@example.org commit -qm '{message}'
            git push -q /home/bob/config.git HEAD:main
            git rev-parse HEAD
        """)
        command = f"cd /tmp && sudo -u bob bash -c {shlex.quote(script)}"
        result = self.ssh("front", command, stdin=yaml.safe_dump(config, sort_keys=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def run_auto_deploy(self) -> subprocess.CompletedProcess[str]:
        """Start one automatic deploy on the front node and wait for it (the timer does the same)."""
        return self.ssh("front", "sudo systemctl start nanohpc-auto-deploy.service")

    def test_front_node_deploys_from_the_repository(self) -> None:
        cluster, config, public_key = self.up_with_test_key()
        front = config["machines"]["front"]["address"]
        config["nanohpc_version"] = metadata.version("nanohpc")
        config["auto_deploy"] = {
            "enabled": True,
            "repository": f"bob@{front}:/home/bob/config.git",
            "branch": "main",
            "every_minutes": 1440,  # the test starts each run itself
        }
        cluster.write_text(yaml.safe_dump(config, sort_keys=False))

        with self.subTest("first deploy: the front node's keys, and how to let it read the repository"):
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertIn("cannot read", result.stdout)
            # Every machine accepts the front node's key for root, from the front node only.
            for machine in config["machines"]:
                keys = self.ssh(machine, "sudo cat /etc/ssh/authorized_keys/root").stdout
                self.assertIn(f'from="{front}"', keys, machine)
            self.assertIn(config["nanohpc_version"], self.on_front("sudo /opt/nanohpc-tool/bin/nanohpc --version"))

        with self.subTest("the repository key is added: the front node deploys its newest commit"):
            repository_key = self.on_front("sudo cat /etc/nanohpc/auto-deploy/repository_ed25519.pub").strip()
            for user in config["users"]:
                if user["name"] == "bob":
                    user["ssh_keys"] = [*user["ssh_keys"], repository_key]
            cluster.write_text(yaml.safe_dump(config, sort_keys=False))
            self.on_front("cd /tmp && sudo -u bob git init -q --bare --initial-branch=main /home/bob/config.git")
            commit = self.push(config, "first")
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertNotIn("cannot read", result.stdout)
            run = self.run_auto_deploy()
            self.assertEqual(
                run.returncode, 0, self.on_front("sudo journalctl -u nanohpc-auto-deploy -n 80 --no-pager")
            )
            self.assertEqual(self.on_front("sudo cat /var/lib/nanohpc/auto-deploy/state/deployed").strip(), commit)
            metrics = self.on_front("cat /var/lib/nanohpc/metrics-textfile/auto-deploy.prom")
            self.assertIn("cluster_auto_deploy_last_exit_code 0", metrics)
            # An automatic deploy runs the dry run first too.
            self.on_front(
                "sudo journalctl -u nanohpc-auto-deploy --no-pager -o cat | grep -F 'Dry run passed: applying'"
            )

        with self.subTest("a new commit changes the cluster with no one logging in"):
            config["users"].append({"name": "carol", "uid": 2010, "ssh_keys": [public_key]})
            self.push(config, "add carol")
            self.assertEqual(self.run_auto_deploy().returncode, 0)
            self.assertIn("carol:x:2010:", self.ssh("cpu1", "getent passwd carol").stdout)

        with self.subTest("a broken commit changes nothing, is reported, and is not retried"):
            broken = {**config, "unknown_field": True}
            bad = self.push(broken, "broken")
            self.assertNotEqual(self.run_auto_deploy().returncode, 0)
            self.assertEqual(self.on_front("sudo cat /var/lib/nanohpc/auto-deploy/state/failed").strip(), bad)
            self.assertNotIn(
                "cluster_auto_deploy_last_exit_code 0",
                self.on_front("cat /var/lib/nanohpc/metrics-textfile/auto-deploy.prom"),
            )
            self.assertIn("carol:x:2010:", self.ssh("cpu1", "getent passwd carol").stdout)
            self.assertEqual(self.run_auto_deploy().returncode, 0)  # the same commit: waits for a newer one
            self.assertIn("WARN  the last automatic deploy worked", self.ssh("front", "sudo cluster-health").stdout)

        with self.subTest("a commit whose dry run fails on a machine: that machine is left out, and it is reported"):
            # dave's UID is taken on cpu1, so the preflight check stops the dry run there.
            self.assertEqual(self.ssh("cpu1", "sudo useradd -u 3011 dave").returncode, 0)
            conflict = {**config, "users": [*config["users"], {"name": "dave", "uid": 2011, "ssh_keys": [public_key]}]}
            bad = self.push(conflict, "dave")
            self.assertNotEqual(self.run_auto_deploy().returncode, 0)
            self.assertEqual(self.on_front("sudo cat /var/lib/nanohpc/auto-deploy/state/failed").strip(), bad)
            self.assertIn(
                "cluster_auto_deploy_last_exit_code 3",
                self.on_front("cat /var/lib/nanohpc/metrics-textfile/auto-deploy.prom"),
            )
            journal = self.on_front("sudo journalctl -u nanohpc-auto-deploy --no-pager -o cat")
            self.assertIn("Dry run: cpu1 failed: preflight", journal)
            self.assertIn(
                f"deploy of commit {bad} failed with exit code 3 (its dry run failed on some machines, which were"
                " left out, unchanged)",
                journal,
            )
            self.assertEqual(self.ssh("cpu1", "getent passwd dave").stdout.split(":")[2], "3011")
            for machine in ("front", "gpu4"):
                self.assertIn("dave:x:2011:", self.ssh(machine, "getent passwd dave").stdout, machine)
            self.assertEqual(self.ssh("cpu1", "sudo userdel dave").returncode, 0)

        with self.subTest("a fixed commit deploys again"):
            fixed = self.push(config, "fixed")
            self.assertEqual(self.run_auto_deploy().returncode, 0)
            self.assertEqual(self.on_front("sudo cat /var/lib/nanohpc/auto-deploy/state/deployed").strip(), fixed)

        with self.subTest("the front node's key logs in as root only from the front node"):
            gpu4 = config["machines"]["gpu4"]["address"]
            self.on_front("sudo ssh -F /etc/nanohpc/auto-deploy/ssh_config gpu4 true")
            key = self.on_front("sudo cat /etc/nanohpc/auto-deploy/id_ed25519")
            self.assertEqual(self.ssh("cpu1", "umask 077 && cat > /tmp/front-key", stdin=key).returncode, 0)
            elsewhere = self.ssh(
                "cpu1",
                f"ssh -i /tmp/front-key -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{gpu4} true",
            )
            self.assertNotEqual(elsewhere.returncode, 0)
            self.ssh("cpu1", "rm -f /tmp/front-key")

        with self.subTest("an automatic deploy between a manual deploy's dry run and real run stops the manual one"):
            state = "/var/lib/nanohpc/auto-deploy/state/deployed"
            deployed = self.on_front(f"sudo cat {state}").strip()
            environment = {key: value for key, value in os.environ.items() if key != "SSH_AUTH_SOCK"}
            process = subprocess.Popen(
                ["uv", "run", "nanohpc", "sim", "deploy", str(self.sim)], cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )  # fmt: skip
            assert process.stdout is not None
            lines = []
            for line in process.stdout:
                lines.append(line)
                if line.startswith("TASK [Make or read the key]"):  # the dry run has read the automatic deploys
                    break
            # Stands in for an automatic deploy that ran meanwhile.
            self.on_front(f"echo 0000000000000000000000000000000000000000 | sudo tee {state}")
            output = "".join(lines) + process.stdout.read()
            process.stdout.close()
            process.wait()
            self.on_front(f"echo {deployed} | sudo tee {state}")
            self.assertEqual(process.returncode, 3, output[-4000:])
            self.assertIn("An automatic deploy ran during this deploy's dry run", output)
            self.assertEqual(self.on_front("systemctl is-active nanohpc-auto-deploy.timer").strip(), "active")

        with self.subTest("a manual deploy after automatic ones changes nothing, and pauses them while it runs"):
            # The administrator's cluster.yml is the repository's (with carol from the commits above).
            cluster.write_text(yaml.safe_dump(config, sort_keys=False))
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assert_no_changes(result, len(config["machines"]))
            self.assertEqual(self.on_front("systemctl is-active nanohpc-auto-deploy.timer").strip(), "active")

        with self.subTest("a commit cannot turn automatic deploys off; a manual deploy can"):
            off = {**config, "auto_deploy": {**config["auto_deploy"], "enabled": False}}
            self.push(off, "turn off")
            self.assertNotEqual(self.run_auto_deploy().returncode, 0)
            self.assertIn(
                "turn them off with nanohpc deploy",
                self.on_front("sudo journalctl -u nanohpc-auto-deploy -n 40 --no-pager"),
            )
            self.assertIn("nanohpc-auto-deploy@", self.ssh("cpu1", "sudo cat /etc/ssh/authorized_keys/root").stdout)
            cluster.write_text(yaml.safe_dump(off, sort_keys=False))
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, self.failure(result))
            # The front node's key is gone from root's keys; the administrators' keys stay.
            root_keys = self.ssh("cpu1", "sudo cat /etc/ssh/authorized_keys/root").stdout
            self.assertNotIn("nanohpc-auto-deploy@", root_keys)
            self.assertIn(public_key, root_keys)
            self.assertNotEqual(
                self.ssh("front", "systemctl is-enabled nanohpc-auto-deploy.timer").stdout.strip(), "enabled"
            )


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimSetupTest(SimUsersBase):
    """What the setup wizard does on real machines, before any deploy: the read-only probe, and fix-uid (a dry
    run that changes nothing, then --apply). On the small cluster of the variations sim file. Real Lima VMs."""

    def setUp(self) -> None:
        self.sim = SIM / "variations.yml"
        self.state = ROOT / ".nanohpc-sim" / "variations"
        super().setUp()

    async def probe_in_wizard(self, path: Path, ssh_config: Path, machines: list[str]) -> None:
        """Open the wizard (headless, with the real probe and fix-uid functions, as cli.py passes them), probe each
        machine with p as an administrator would, check the results and the checklist, and quit with q."""
        dependencies = wizard.Dependencies(
            probe.probe_machine, probe.user_ids, probe.uid_problems, probe.uid_owner, fixuid.plan_fix, fixuid.apply_fix
        )
        app = wizard.WizardApp(path, ssh_config, dependencies)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#machine-table", DataTable)
            for name in machines:
                table.move_cursor(row=table.get_row_index(name))
                table.focus()
                await pilot.press("p")
                await pilot.pause()
            for _ in range(3):
                await app.workers.wait_for_complete()
                await pilot.pause()
            for name in machines:
                self.assertTrue(str(table.get_row(name)[-1]).startswith("✓"), table.get_row(name))
                items = {item.key: item.ok for item in checklist(app.state, name)}
                self.assertTrue(items["ssh"] and items["ubuntu"], (name, items))
            await pilot.press("q")
            await pilot.pause()
        self.assertFalse(app.saved)

    def test_probe_and_fix_uid(self) -> None:
        self.remove_at_end()
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(self.sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ssh_config = self.state / "ssh_config"
        cluster = yaml.safe_load((self.state / "cluster.yml").read_text())

        with self.subTest("the probe reads each machine without changing it"):
            for name, machine in cluster["machines"].items():
                facts = probe_machine(name, ssh_config)
                self.assertIsNone(facts.error, name)
                self.assertEqual(facts.ubuntu, "24.04", name)
                self.assertTrue(facts.sudo_ok, name)
                self.assertIn(machine["address"], facts.addresses, name)
                self.assertEqual(facts.cpus, 2 if name == "front" else 1, name)
                self.assertGreater(facts.memory_mb, (2500 if name == "front" else 700), name)
                self.assertEqual(facts.gpus, [], name)  # fake GPUs have no nvidia-smi
            disks = {disk.path: disk for disk in probe_machine("gpu4", ssh_config).disks}
            self.assertIn("/dev/vdb", disks)  # the scratch disk sim up attached
            self.assertEqual(disks["/dev/vdb"].fstype, "ext4")

        with (
            self.subTest("the wizard probes every machine from the Machines step and changes nothing"),
            tempfile.TemporaryDirectory() as folder,
        ):
            path = Path(folder) / "cluster.yml"
            shutil.copy(self.state / "cluster.yml", path)
            asyncio.run(self.probe_in_wizard(path, ssh_config, list(cluster["machines"])))
            self.assertEqual(path.read_bytes(), (self.state / "cluster.yml").read_bytes())

        with self.subTest("fix-uid: a read-only plan, then the renumbering after --apply"):
            self.assertEqual(self.ssh("gpu4", "sudo useradd -u 3005 -U -m alice").returncode, 0)
            self.assertEqual(self.ssh("gpu4", "sudo -u alice touch /tmp/alice-file").returncode, 0)
            paths = [f"/tmp/alice-file-{index}" for index in range(6)]
            created = self.ssh("gpu4", "sudo -u alice sh -c 'for i in 0 1 2 3 4 5; do touch /tmp/alice-file-$i; done'")
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            command = [
                "uv",
                "run",
                "nanohpc",
                "fix-uid",
                str(self.state / "cluster.yml"),
                "alice",
                "gpu4",
                "--ssh-config",
                str(ssh_config),
            ]
            dry = self.run_command(*command)
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertIn("nothing was changed", dry.stdout)
            for path in paths:
                self.assertIn(f'    "{path}"\n', dry.stdout)
            self.assertIn("alice:x:3005:3005:", self.ssh("gpu4", "getent passwd alice").stdout)
            applied = self.run_command(*command, "--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            preview = applied.stdout.split("ran as root on gpu4:", 1)[0]
            for path in paths:
                self.assertIn(f'    "{path}"\n', preview)
            self.assertIn("alice:x:2000:2000:", self.ssh("gpu4", "getent passwd alice").stdout)
            self.assertEqual(
                self.ssh("gpu4", "stat -c %u:%g /tmp/alice-file /home/alice").stdout.split(), ["2000:2000", "2000:2000"]
            )


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimHomeBindRemountTest(SimUsersBase):
    """A later deploy applies changed options to /home when it is a bind mount on the root disk."""

    def setUp(self) -> None:
        self.sim = SIM / "variations.yml"
        self.state = ROOT / ".nanohpc-sim" / "variations"
        super().setUp()

    def test_home_bind_remount(self) -> None:
        self.up_and_deploy()
        edit = (
            "from pathlib import Path; p = Path('/etc/fstab'); "
            "p.write_text(''.join(line.replace(',nodev', '').replace(',nosuid', '') "
            "if ' /home ' in line else line for line in p.read_text().splitlines(keepends=True)))"
        )
        changed = self.ssh("front", f"sudo python3 -c {shlex.quote(edit)} && sudo mount -o remount,bind,suid,dev /home")
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        before = self.on_front("cat /etc/fstab; findmnt -n -o OPTIONS --mountpoint /home")
        self.assertNotIn("nodev", before.splitlines()[-1].split(","))
        self.assertNotIn("nosuid", before.splitlines()[-1].split(","))

        dry = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--dry-run")
        self.assertEqual(dry.returncode, 0, self.failure(dry))
        self.assertEqual(self.on_front("cat /etc/fstab; findmnt -n -o OPTIONS --mountpoint /home"), before)

        applied = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(applied.returncode, 0, self.failure(applied))
        options = self.on_front("findmnt -n -o OPTIONS --mountpoint /home").strip().split(",")
        self.assertIn("nodev", options)
        self.assertIn("nosuid", options)
        self.assertEqual(len(self.on_front("findmnt -n -o TARGET --mountpoint /home").splitlines()), 1)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimRootHomeRebootTest(SimUsersBase):
    """A root-disk /home bind mount and its quotas survive a reboot."""

    def setUp(self) -> None:
        self.sim = SIM / "variations.yml"
        self.state = ROOT / ".nanohpc-sim" / "variations"
        super().setUp()

    def test_root_home_survives_reboot(self) -> None:
        _, config, _ = self.up_and_deploy()
        self.on_front("sudo -u alice sh -c 'echo reboot-check > /home/alice/nanohpc-reboot-check'")
        front = next(vm.instance for vm in plan_of(self.sim).vms if vm.machine == "front")
        self.reboot_vm("front", front)
        mount = self.wait_for_vm("front", "findmnt -n -o SOURCE,FSTYPE,OPTIONS --mountpoint /home")
        _, fstype, options = mount.strip().split()
        self.assertEqual(fstype, "ext4")
        self.assertIn("nodev", options.split(","))
        self.assertIn("nosuid", options.split(","))
        self.assertEqual(
            self.wait_for_vm("front", "sudo -u alice cat /home/alice/nanohpc-reboot-check").strip(), "reboot-check"
        )
        self.assertIn(" is on", self.wait_for_vm("front", "sudo quotaon -p -u / | grep -F ' is on'"))
        quotas = self.wait_for_vm("front", "sudo repquota -u -O csv /")
        rows = {row.split(",")[0]: row.split(",") for row in quotas.splitlines()}
        columns = rows["User"]
        self.assertEqual(
            (rows["alice"][columns.index("BlockSoftLimit")], rows["alice"][columns.index("BlockHardLimit")]),
            (str(config["home"]["quota_soft_gb"] * 1024 * 1024), str(config["home"]["quota_hard_gb"] * 1024 * 1024)),
        )


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimRebootTest(SimUsersBase):
    """A deployed home disk, NFS clients, quotas, and both scratch kinds survive a machine reboot."""

    def setUp(self) -> None:
        self.sim = SIM / "home-on-storage.yml"
        self.state = ROOT / ".nanohpc-sim" / "home-on-storage"
        super().setUp()

    def test_mounts_and_quotas_survive_reboot(self) -> None:
        _, config, _ = self.up_and_deploy()
        instances = {vm.machine: vm.instance for vm in plan_of(self.sim).vms}
        store_address = config["machines"]["store"]["address"]
        marker = "sudo -u alice timeout 10 cat /home/alice/nanohpc-reboot-check"
        made = self.ssh("store", "sudo -u alice sh -c 'echo reboot-check > /home/alice/nanohpc-reboot-check'")
        self.assertEqual(made.returncode, 0, made.stderr)
        for machine in ("gpu4", "gpu2"):
            made = self.ssh(machine, "sudo -u alice sh -c 'echo before > /scratch/alice/nanohpc-reboot-check'")
            self.assertEqual(made.returncode, 0, f"{machine}: {made.stdout}{made.stderr}")

        for machine in ("store", "front", "gpu4", "gpu2"):
            with self.subTest(machine=machine):
                self.reboot_vm(machine, instances[machine])
                mount = self.wait_for_vm(machine, "findmnt -n -o SOURCE,FSTYPE,OPTIONS --mountpoint /home")
                source, fstype, options = mount.strip().split()
                if machine == "store":
                    self.assertEqual((source, fstype), ("/dev/vdb", "ext4"))
                    self.assertIn(" is on", self.wait_for_vm(machine, "sudo quotaon -p -u /home | grep -F ' is on'"))
                    quotas = self.wait_for_vm(machine, "sudo repquota -u -O csv /home")
                    rows = {row.split(",")[0]: row.split(",") for row in quotas.splitlines()}
                    columns = rows["User"]
                    self.assertEqual(
                        (
                            rows["alice"][columns.index("BlockSoftLimit")],
                            rows["alice"][columns.index("BlockHardLimit")],
                        ),
                        (
                            str(config["home"]["quota_soft_gb"] * 1024 * 1024),
                            str(config["home"]["quota_hard_gb"] * 1024 * 1024),
                        ),
                    )
                else:
                    self.assertEqual((source, fstype), (f"{store_address}:/home", "nfs4"))
                self.assertEqual(self.wait_for_vm(machine, marker).strip(), "reboot-check")
                self.assertIn("nodev", options.split(","))
                self.assertIn("nosuid", options.split(","))

                if machine in ("gpu4", "gpu2"):
                    scratch = self.wait_for_vm(machine, "findmnt -n -o SOURCE,OPTIONS --mountpoint /scratch")
                    scratch_source, scratch_options = scratch.strip().split()
                    if machine == "gpu4":
                        self.assertEqual(scratch_source, "/dev/vdb")
                    else:
                        self.assertTrue(scratch_source.startswith("/dev/loop"), scratch_source)
                    self.assertIn("nodev", scratch_options.split(","))
                    self.assertIn("nosuid", scratch_options.split(","))
                    self.wait_for_vm(
                        machine,
                        'sudo -u alice sh -c \'test "$(cat /scratch/alice/nanohpc-reboot-check)" = before '
                        "&& echo after > /scratch/alice/nanohpc-reboot-check "
                        '&& test "$(cat /scratch/alice/nanohpc-reboot-check)" = after\'',
                    )


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimMissingHomeDiskTest(SimUsersBase):
    """A missing local /home disk leaves root SSH available without exporting an empty directory."""

    def setUp(self) -> None:
        self.sim = SIM / "home-on-storage.yml"
        self.state = ROOT / ".nanohpc-sim" / "home-on-storage"
        super().setUp()

    def test_missing_disk_boot_and_restored_export(self) -> None:
        """Remove the storage VM's home disk for one boot, then restore it with its data."""
        self.up_and_deploy()
        store = next(vm for vm in plan_of(self.sim).vms if vm.machine == "store")
        self.assertEqual(len(store.disks), 1)
        self.assertEqual(store.home_device, "/dev/vdb")
        marker = self.ssh("store", "sudo -u alice sh -c 'echo original-home > /home/alice/disk-check'")
        self.assertEqual(marker.returncode, 0, marker.stdout + marker.stderr)

        # Check the boot settings before removing the disk, so a broken test run never waits on a blocked boot.
        fstab = self.ssh("store", "awk '$2 == \"/home\" {print $4}' /etc/fstab")
        self.assertEqual(fstab.returncode, 0, fstab.stderr)
        self.assertIn("nofail", fstab.stdout.strip().split(","))
        requires = self.ssh("store", "systemctl show nfs-server.service --property=Requires --value")
        self.assertEqual(requires.returncode, 0, requires.stderr)
        self.assertIn("home.mount", requires.stdout.split())

        stopped = self.run_command("limactl", "stop", store.instance)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        try:
            detached = self.run_command(
                "limactl", "edit", "--tty=false", "--set", ".additionalDisks = []", store.instance
            )
            self.assertEqual(detached.returncode, 0, detached.stdout + detached.stderr)
            started = self.run_command("limactl", "start", "--tty=false", "--timeout=20m", store.instance)
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            self.refresh_vm_port("store", store.instance)
            root = self.as_user("root", "store", "whoami", agent=False)
            self.assertEqual((root.returncode, root.stdout.strip()), (0, "root"), root.stderr)
            mount = self.as_user("root", "store", "findmnt -n --mountpoint /home", agent=False)
            self.assertNotEqual(mount.returncode, 0, mount.stdout + mount.stderr)
            nfs = self.as_user("root", "store", "systemctl is-active nfs-server.service", agent=False)
            self.assertNotEqual(nfs.stdout.strip(), "active", nfs.stdout + nfs.stderr)
            exports = self.as_user(
                "root",
                "store",
                "if test -e /proc/fs/nfsd/exports; then cat /proc/fs/nfsd/exports; fi",
                agent=False,
            )
            self.assertEqual(exports.returncode, 0, exports.stdout + exports.stderr)
            self.assertNotIn("/home", exports.stdout)
        finally:
            stopped = self.run_command("limactl", "stop", store.instance)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            attached = self.run_command(
                "limactl",
                "edit",
                "--tty=false",
                "--set",
                f'.additionalDisks = [{{"name": "{store.disks[0]}", "format": false}}]',
                store.instance,
            )
            self.assertEqual(attached.returncode, 0, attached.stdout + attached.stderr)
            started = self.run_command("limactl", "start", "--tty=false", "--timeout=20m", store.instance)
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            self.refresh_vm_port("store", store.instance)

        content = self.wait_for_vm("store", "sudo -u alice cat /home/alice/disk-check")
        self.assertEqual(content.strip(), "original-home")
        mounted = self.ssh("store", "findmnt -n -o SOURCE --mountpoint /home")
        self.assertEqual((mounted.returncode, mounted.stdout.strip()), (0, "/dev/vdb"), mounted.stderr)
        nfs = self.wait_for_vm("store", "systemctl is-active nfs-server.service")
        self.assertEqual(nfs.strip(), "active")
        exports = self.ssh("store", "sudo exportfs -v")
        self.assertEqual(exports.returncode, 0, exports.stdout + exports.stderr)
        self.assertIn("/home", exports.stdout)
        client_file = self.wait_for_vm("front", "sudo -u alice timeout 10 cat /home/alice/disk-check")
        self.assertEqual(client_file.strip(), "original-home")


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimXfsQuotaTest(SimUsersBase):
    """XFS home and scratch disks work, and the home quota stops writes over NFS."""

    def setUp(self) -> None:
        self.sim = SIM / "xfs-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "xfs-quota"
        super().setUp()

    def test_xfs_disks_and_nfs_quota(self) -> None:
        _, config, _ = self.up_with_test_key()
        for machine in ("store", "gpu4"):
            installed = self.ssh(machine, "sudo apt-get install -y xfsprogs")
            self.assertEqual(installed.returncode, 0, f"{machine}: {installed.stdout}{installed.stderr}")
            filesystem = self.ssh(machine, "sudo blkid -s TYPE -o value /dev/vdb")
            self.assertEqual(filesystem.returncode, 0, f"{machine}: {filesystem.stdout}{filesystem.stderr}")
            if filesystem.stdout.strip() != "xfs":
                mounted = self.ssh(machine, "findmnt -n -o TARGET --source /dev/vdb")
                self.assertEqual(mounted.stdout.strip(), "", f"{machine}: /dev/vdb is mounted at {mounted.stdout}")
                formatted = self.ssh(machine, "sudo mkfs.xfs -f -q /dev/vdb")
                self.assertEqual(formatted.returncode, 0, f"{machine}: {formatted.stdout}{formatted.stderr}")
        deployed = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(deployed.returncode, 0, self.failure(deployed))

        home = self.on_front("findmnt -n -o SOURCE,FSTYPE,OPTIONS --mountpoint /home")
        self.assertIn("nfs4", home)
        disk = self.ssh("store", "findmnt -n -o SOURCE,FSTYPE,OPTIONS --mountpoint /home")
        self.assertEqual(disk.returncode, 0, disk.stderr)
        self.assertIn("/dev/vdb xfs", disk.stdout)
        scratch = self.ssh("gpu4", "findmnt -n -o SOURCE,FSTYPE --mountpoint /scratch")
        self.assertEqual(scratch.returncode, 0, scratch.stderr)
        self.assertEqual(scratch.stdout.strip(), "/dev/vdb xfs")
        scratch_file = self.ssh(
            "gpu4",
            "sudo -u alice sh -c 'echo xfs > /scratch/alice/nanohpc-xfs-check; cat /scratch/alice/nanohpc-xfs-check'",
        )
        self.assertEqual(scratch_file.returncode, 0, scratch_file.stdout + scratch_file.stderr)
        self.assertEqual(scratch_file.stdout.strip(), "xfs")
        home_file = self.ssh(
            "front", "sudo -u alice sh -c 'echo xfs > /home/alice/nanohpc-xfs-check; cat /home/alice/nanohpc-xfs-check'"
        )
        self.assertEqual(home_file.returncode, 0, home_file.stdout + home_file.stderr)
        self.assertEqual(home_file.stdout.strip(), "xfs")
        quotas = self.ssh("store", "sudo repquota -u -O csv /home")
        self.assertEqual(quotas.returncode, 0, quotas.stdout + quotas.stderr)
        rows = {row.split(",")[0]: row.split(",") for row in quotas.stdout.splitlines()}
        columns = rows["User"]
        self.assertEqual(
            (rows["alice"][columns.index("BlockSoftLimit")], rows["alice"][columns.index("BlockHardLimit")]),
            (str(config["home"]["quota_soft_gb"] * 1024 * 1024), str(config["home"]["quota_hard_gb"] * 1024 * 1024)),
        )

        limited = self.ssh("store", "sudo setquota -u alice 2048 2048 0 0 /home")
        self.assertEqual(limited.returncode, 0, limited.stdout + limited.stderr)
        written = self.ssh(
            "front", "sudo -u alice dd if=/dev/zero of=/home/alice/nanohpc-quota-check bs=1M count=4 status=none"
        )
        self.assertNotEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertIn("Disk quota exceeded", written.stderr)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimKernelQuotaTest(SimUsersBase):
    """A new Ubuntu kernel still has quota modules when the home machine reboots."""

    def setUp(self) -> None:
        self.sim = SIM / "kernel-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "kernel-quota"
        super().setUp()

    def test_home_quotas_after_kernel_update(self) -> None:
        self.up_and_deploy()
        extra_meta = self.on_front("dpkg-query -W -f='${Status}' linux-image-extra-virtual")
        self.assertEqual(extra_meta, "install ok installed", "future virtual kernels need their extra modules")
        meta = self.on_front("dpkg-query -W -f='${Status}' linux-image-generic")
        self.assertEqual(meta, "install ok installed", "the future generic kernels need their extra modules")
        redeploy = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(redeploy.returncode, 0, self.failure(redeploy))
        self.on_front("sudo -u alice sh -c 'echo before > /home/alice/nanohpc-kernel-check'")
        before = self.on_front("uname -r").strip()
        self.on_front("sudo apt-get update -qq")
        self.on_front(
            "sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y linux-virtual linux-image-extra-virtual"
        )
        dependency = self.on_front("dpkg-query -W -f='${Depends}' linux-image-virtual").strip()
        self.assertTrue(dependency.startswith("linux-image-"), dependency)
        new_kernel = dependency.removeprefix("linux-image-")
        if new_kernel == before:
            self.skipTest("no newer Ubuntu kernel is available")
        extra = self.on_front(f"dpkg-query -W -f='${{Status}}' linux-modules-extra-{new_kernel}")
        self.assertEqual(extra, "install ok installed")
        front = next(vm.instance for vm in plan_of(self.sim).vms if vm.machine == "front")
        self.reboot_vm("front", front)
        self.assertEqual(self.wait_for_vm("front", "uname -r").strip(), new_kernel)
        self.assertIn("/dev/vdb ext4", self.wait_for_vm("front", "findmnt -n -o SOURCE,FSTYPE --mountpoint /home"))
        self.assertIn(" is on", self.wait_for_vm("front", "sudo quotaon -p -u /home | grep -F ' is on'"))
        self.assertEqual(
            self.wait_for_vm("front", "sudo -u alice cat /home/alice/nanohpc-kernel-check").strip(), "before"
        )
        self.wait_for_vm("front", "sudo -u alice sh -c 'echo after > /home/alice/nanohpc-kernel-check'")
        redeploy = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(redeploy.returncode, 0, self.failure(redeploy))
        self.assertIn(" is on", self.on_front("sudo quotaon -p -u /home | grep -F ' is on'"))


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimUpdateReportTest(SimUsersBase):
    """The read-only update report reaches all Ubuntu 24.04 VMs and leaves APT lists alone."""

    def setUp(self) -> None:
        self.sim = SIM / "kernel-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "kernel-quota"
        super().setUp()

    def test_update_report_on_vms(self) -> None:
        cluster, config, _ = self.up_with_test_key()
        names = list(config["machines"])
        list_state = "find /var/lib/apt/lists -maxdepth 1 -type f -printf '%f %T@ %s\\n' | sort"
        before = {name: self.ssh(name, list_state).stdout for name in names}
        result = self.run_command(
            "uv", "run", "nanohpc", "update-report", str(cluster), "--ssh-config", str(self.state / "ssh_config")
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in names:
            self.assertIn(f"{name}: ", result.stdout)
            self.assertIn("package lists from ", result.stdout)
            self.assertEqual(self.ssh(name, list_state).stdout, before[name])


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimComputeUpdateTest(SimUsersBase):
    """A confirmed update installs a real APT upgrade on an Ubuntu 24.04 compute VM."""

    def setUp(self) -> None:
        self.sim = SIM / "kernel-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "kernel-quota"
        super().setUp()

    def test_dry_run_and_apply_on_vm(self) -> None:
        cluster, _, _ = self.up_and_deploy()
        home = self.ssh("cpu1", "awk '$2 == \"/home\" {print $4}' /etc/fstab")
        self.assertEqual(home.returncode, 0, home.stdout + home.stderr)
        self.assertIn("nofail", home.stdout.strip().split(","))
        reboot_setting = self.ssh("cpu1", "apt-config dump Unattended-Upgrade::Automatic-Reboot")
        self.assertIn('Unattended-Upgrade::Automatic-Reboot "false";', reboot_setting.stdout)
        for machine in ("front", "gpu4", "cpu1"):
            waiting = self.ssh(machine, "apt-config shell enabled APT::Periodic::Unattended-Upgrade")
            self.assertEqual(waiting.stdout.strip(), "enabled='0'", waiting.stdout + waiting.stderr)
            policy = self.ssh(machine, "sudo /usr/local/sbin/nanohpc-auto-updates-check")
            self.assertEqual(policy.returncode, 0, f"{machine}: {policy.stdout}{policy.stderr}")
        # Lima writes Netplan after boot and its base /boot entry lacks nofail. The real saved-setting
        # checks should pass on a safe baseline, while SimRestartCheckTest covers refusal of unsafe settings.
        baseline = r"""
import os, pathlib, time
fstab = pathlib.Path('/etc/fstab')
lines = []
for line in fstab.read_text().splitlines():
    fields = line.split()
    if len(fields) >= 4 and fields[1] == '/boot' and 'nofail' not in fields[3].split(','):
        fields[3] += ',nofail'
        line = '\t'.join(fields)
    lines.append(line)
fstab.write_text('\n'.join(lines) + '\n')
boot_time = time.time() - float(pathlib.Path('/proc/uptime').read_text().split()[0])
for path in pathlib.Path('/etc/netplan').glob('*.yaml'):
    os.utime(path, (boot_time - 10, boot_time - 10))
"""
        safe = self.ssh("cpu1", "sudo /usr/bin/python3 -c " + shlex.quote(baseline))
        self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
        setup = """
set -eu
sudo apt-get install -y dpkg-dev
for name in nanohpc-update-fixture openssh-nanohpc-fixture nanohpc-security-fixture; do
  for version in 1 2 3; do
    if [ "$version" = 3 ] && [ "$name" != nanohpc-security-fixture ]; then continue; fi
    folder=/tmp/$name-$version
    mkdir -p "$folder/DEBIAN"
    printf 'Package: %s\\nVersion: %s\\nArchitecture: all\\nMaintainer: nanoHPC test <test@example.invalid>\\nDescription: disposable update test\\n' "$name" "$version" > "$folder/DEBIAN/control"
    dpkg-deb --build "$folder" "/tmp/${name}_${version}_all.deb" >/dev/null
  done
  sudo dpkg -i "/tmp/${name}_1_all.deb"
done
mkdir -p /tmp/nanohpc-original-sources
sudo cp -a /etc/apt/sources.list.d/. /tmp/nanohpc-original-sources/
sudo rm -f /etc/apt/sources.list.d/*
sudo find /var/lib/apt/lists -maxdepth 1 -type f ! -name lock -delete
if [ -f /etc/apt/sources.list ]; then
  sudo cp /etc/apt/sources.list /tmp/nanohpc-original-sources-list
  sudo rm /etc/apt/sources.list
fi
mkdir -p /tmp/nanohpc-update-repo
cp /tmp/nanohpc-update-fixture_2_all.deb /tmp/openssh-nanohpc-fixture_2_all.deb /tmp/nanohpc-update-repo/
cd /tmp/nanohpc-update-repo
dpkg-scanpackages . /dev/null > Packages
gzip -kf Packages
echo 'deb [trusted=yes] file:/tmp/nanohpc-update-repo ./' | sudo tee /etc/apt/sources.list.d/nanohpc-update-fixture.list >/dev/null
for pocket in security updates; do
  mkdir -p /tmp/nanohpc-$pocket-repo
  version=2
  if [ "$pocket" = updates ]; then version=3; fi
  cp "/tmp/nanohpc-security-fixture_${version}_all.deb" "/tmp/nanohpc-$pocket-repo/"
  cd "/tmp/nanohpc-$pocket-repo"
  dpkg-scanpackages . /dev/null > Packages
  gzip -kf Packages
  printf 'Origin: Ubuntu\nLabel: Ubuntu\nSuite: noble-%s\nCodename: noble-%s\nArchitectures: %s\nComponents: main\n' "$pocket" "$pocket" "$(dpkg --print-architecture)" > Release
  echo "deb [trusted=yes] file:/tmp/nanohpc-$pocket-repo ./" | sudo tee "/etc/apt/sources.list.d/nanohpc-$pocket-fixture.list" >/dev/null
done
"""
        prepared = self.ssh("cpu1", "bash -se", stdin=setup)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        admin_config = self.ssh_config_for("alice")
        environment = {
            **os.environ,
            "SSH_AUTH_SOCK": self.agent_socket,
            "XDG_STATE_HOME": str(self.keys / "state"),
        }
        command = [
            "uv",
            "run",
            "nanohpc",
            "update",
            str(cluster),
            "cpu1",
            "--include",
            "extra",
            "--ssh-config",
            str(admin_config),
        ]
        with maintenance_lock.acquire("front", self.state / "ssh_config", None):
            blocked = subprocess.run(
                [*command, "--dry-run"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("maintenance", blocked.stderr)
        preview = subprocess.run(
            [*command, "--dry-run"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn("nanohpc-update-fixture=2", preview.stdout)
        self.assertIn("nanohpc-security-fixture=3", preview.stdout)
        snapshot = update.read_snapshot("cpu1", admin_config)
        self.assertIn("nanohpc-security-fixture", snapshot["security_backlog"])
        security_candidate = next(pkg for pkg in snapshot["packages"] if pkg["name"] == "nanohpc-security-fixture")
        self.assertFalse(security_candidate["security"])
        applied = subprocess.run(
            [*command, "--confirm", "cpu1"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        active = self.ssh("cpu1", "apt-config shell enabled APT::Periodic::Unattended-Upgrade")
        self.assertEqual(active.stdout.strip(), "enabled='1'", active.stdout + active.stderr)
        policy = self.ssh("cpu1", "sudo /usr/local/sbin/nanohpc-auto-updates-check")
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)
        after = update.read_snapshot("cpu1", admin_config)
        self.assertNotIn("nanohpc-security-fixture", after["security_backlog"])
        version = self.ssh("cpu1", "dpkg-query -W -f='${Version}' nanohpc-update-fixture")
        self.assertEqual(version.stdout.strip(), "2", version.stdout + version.stderr)
        security_version = self.ssh("cpu1", "dpkg-query -W -f='${Version}' nanohpc-security-fixture")
        self.assertEqual(security_version.stdout.strip(), "3", security_version.stdout + security_version.stderr)
        self.assertNotIn("systemctl reboot", applied.stdout)
        state = self.on_front("scontrol show node cpu1 -o")
        self.assertNotIn("DRAIN", state, applied.stdout + applied.stderr)

        ssh_command = command.copy()
        ssh_command[ssh_command.index("extra")] = "ssh,extra"
        ssh_preview = subprocess.run(
            [*ssh_command, "--dry-run"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(ssh_preview.returncode, 0, ssh_preview.stdout + ssh_preview.stderr)
        self.assertIn("openssh-nanohpc-fixture=2", ssh_preview.stdout)
        ssh_applied = subprocess.run(
            [*ssh_command, "--confirm", "cpu1"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(ssh_applied.returncode, 0, ssh_applied.stdout + ssh_applied.stderr)
        ssh_version = self.ssh("cpu1", "dpkg-query -W -f='${Version}' openssh-nanohpc-fixture")
        self.assertEqual(ssh_version.stdout.strip(), "2", ssh_version.stdout + ssh_version.stderr)
        cleared = self.ssh("cpu1", "sudo test ! -e /root/nanohpc-ssh-update-undo")
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        for user in ("root", "alice"):
            login = self.run_command("ssh", "-F", str(admin_config), "-o", "BatchMode=yes", "-l", user, "cpu1", "true")
            self.assertEqual(login.returncode, 0, f"{user}: {login.stdout}{login.stderr}")

        automatic_fixture = """
set -eu
for name in nanohpc-auto-fixture nvidia-nanohpc-fixture; do
  for version in 1 2; do
    folder=/tmp/$name-$version
    mkdir -p "$folder/DEBIAN"
    printf 'Package: %s\\nVersion: %s\\nArchitecture: all\\nMaintainer: nanoHPC test <test@example.invalid>\\nDescription: disposable security update test\\n' "$name" "$version" > "$folder/DEBIAN/control"
    dpkg-deb --build "$folder" "/tmp/${name}_${version}_all.deb" >/dev/null
  done
  sudo dpkg -i "/tmp/${name}_1_all.deb"
  cp "/tmp/${name}_2_all.deb" /tmp/nanohpc-security-repo/
done
cd /tmp/nanohpc-security-repo
dpkg-scanpackages . /dev/null > Packages
gzip -kf Packages
# This disposable Release file has no package hash or changing date. Refresh its index explicitly.
sudo rm -f /var/lib/apt/lists/_tmp_nanohpc-security-repo_._Packages
sudo apt-get update -o APT::Update::Error-Mode=any
sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l unattended-upgrade
"""
        automatic = self.ssh("cpu1", "bash -se", stdin=automatic_fixture)
        self.assertEqual(automatic.returncode, 0, automatic.stdout + automatic.stderr)
        updated_automatically = self.ssh("cpu1", "dpkg-query -W -f='${Version}' nanohpc-auto-fixture")
        self.assertEqual(updated_automatically.stdout.strip(), "2", automatic.stdout + automatic.stderr)
        driver_held = self.ssh("cpu1", "dpkg-query -W -f='${Version}' nvidia-nanohpc-fixture")
        self.assertEqual(driver_held.stdout.strip(), "1", automatic.stdout + automatic.stderr)

        restore_sources = self.ssh(
            "cpu1",
            "sudo rm -f /etc/apt/sources.list.d/nanohpc-*fixture.list; "
            "sudo cp -a /tmp/nanohpc-original-sources/. /etc/apt/sources.list.d/; "
            "if test -f /tmp/nanohpc-original-sources-list; then "
            "sudo cp /tmp/nanohpc-original-sources-list /etc/apt/sources.list; fi; "
            "sudo apt-get update -o APT::Update::Error-Mode=any",
        )
        self.assertEqual(restore_sources.returncode, 0, restore_sources.stdout + restore_sources.stderr)
        self.check_ssh_undo_on_vm(admin_config, environment, command)

    def check_ssh_undo_on_vm(self, admin_config: Path, environment: dict[str, str], command: list[str]) -> None:
        """Exercise the real timer, backup, restored logins, and a separate manual package repair."""
        instance = "nanohpc-kernel-quota-cpu1"
        holds: list[subprocess.Popen[str]] = []
        self.addCleanup(ssh_update.close_holds, holds)
        holds.append(ssh_update.hold_root("cpu1", admin_config))
        holds.append(ssh_update.hold_root("cpu1", admin_config))
        ssh_update.require_holds("cpu1", holds)

        def on_vm(*arguments: str) -> subprocess.CompletedProcess[str]:
            return self.run_command("limactl", "shell", instance, "--", *arguments)

        source = ssh_update.PREPARE_PROGRAM.replace("UNDO_SOURCE", repr(ssh_update.UNDO_PROGRAM))
        prepared = on_vm("sudo", "/usr/bin/python3", "-c", source)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        before_binary = on_vm("sudo", "sha256sum", "/usr/sbin/sshd")
        self.assertEqual(before_binary.returncode, 0, before_binary.stderr)
        timer = on_vm("sudo", "systemctl", "is-active", ssh_update.TIMER + ".timer")
        self.assertEqual(timer.stdout.strip(), "active", timer.stdout + timer.stderr)
        canceled = on_vm("sudo", "/usr/bin/python3", "-c", ssh_update.CANCEL_PROGRAM)
        self.assertEqual(canceled.returncode, 0, canceled.stdout + canceled.stderr)
        cleared = on_vm("sudo", "test", "!", "-e", ssh_update.BACKUP)
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        prepared = on_vm("sudo", "/usr/bin/python3", "-c", source)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        upgraded = on_vm(
            "sudo",
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "NEEDRESTART_MODE=l",
            "apt-get",
            "install",
            "-y",
            "--reinstall",
            "--only-upgrade",
            "-o",
            "Dpkg::Options::=--force-confold",
            "openssh-server",
            "openssh-client",
            "openssh-sftp-server",
        )
        self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
        upgraded_binary = on_vm("sudo", "sha256sum", "/usr/sbin/sshd")
        self.assertEqual(upgraded_binary.returncode, 0, upgraded_binary.stderr)
        damage = """
import os
from pathlib import Path
target = Path('/usr/sbin/sshd')
replacement = Path('/usr/sbin/.sshd-nanohpc-test-broken')
replacement.write_bytes(b'broken OpenSSH test binary\\n')
replacement.chmod(0o755)
os.replace(replacement, target)
"""
        damaged = on_vm("sudo", "/usr/bin/python3", "-c", damage)
        self.assertEqual(damaged.returncode, 0, damaged.stdout + damaged.stderr)
        damaged_binary = on_vm("sudo", "sha256sum", "/usr/sbin/sshd")
        self.assertNotEqual(damaged_binary.stdout, before_binary.stdout)
        failed_start = on_vm("sudo", "systemctl", "restart", "ssh.service")
        self.assertNotEqual(failed_start.returncode, 0, "damaged sshd unexpectedly started")
        broken = on_vm(
            "sudo",
            "sh",
            "-c",
            "printf 'bad-key\\n' > /etc/ssh/authorized_keys/root; printf 'bad-key\\n' > /etc/ssh/authorized_keys/alice",
        )
        self.assertEqual(broken.returncode, 0, broken.stdout + broken.stderr)
        denied = self.run_command("ssh", "-F", str(admin_config), "-o", "BatchMode=yes", "-l", "root", "cpu1", "true")
        self.assertNotEqual(denied.returncode, 0, "the recovery test did not break fresh root login")
        ssh_update.require_holds("cpu1", holds)
        restored = on_vm("sudo", "systemctl", "start", ssh_update.TIMER + ".service")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        for _ in range(120):
            service = on_vm("sudo", "systemctl", "show", "-p", "ActiveState", "--value", ssh_update.TIMER + ".service")
            self.assertEqual(service.returncode, 0, service.stderr)
            if service.stdout.strip() in ("inactive", "failed"):
                break
            time.sleep(1)
        if service.stdout.strip() != "inactive":
            journal = on_vm("sudo", "journalctl", "-u", ssh_update.TIMER + ".service", "-n", "80", "--no-pager")
            self.fail(f"SSH undo service {service.stdout.strip()}: {journal.stdout}{journal.stderr}")
        stopped = on_vm("sudo", "systemctl", "stop", ssh_update.TIMER + ".timer")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        marker = on_vm("sudo", "test", "-f", ssh_update.BACKUP + "/needs-review")
        self.assertEqual(marker.returncode, 0, marker.stdout + marker.stderr)
        restored_binary = on_vm("sudo", "sha256sum", "/usr/sbin/sshd")
        self.assertEqual(restored_binary.stdout, before_binary.stdout)
        valid = on_vm("sudo", "sshd", "-t")
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        for user in ("root", "alice"):
            login = self.run_command("ssh", "-F", str(admin_config), "-o", "BatchMode=yes", "-l", user, "cpu1", "true")
            self.assertEqual(login.returncode, 0, f"{user}: {login.stdout}{login.stderr}")
        ssh_update.require_holds("cpu1", holds)
        blocked = subprocess.run(
            [*command, "--dry-run"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("SSH undo backup", blocked.stderr)
        repaired = on_vm(
            "sudo",
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "NEEDRESTART_MODE=l",
            "apt-get",
            "install",
            "-y",
            "--reinstall",
            "--only-upgrade",
            "-o",
            "Dpkg::Options::=--force-confold",
            "openssh-server",
            "openssh-client",
            "openssh-sftp-server",
        )
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        repaired_binary = on_vm("sudo", "sha256sum", "/usr/sbin/sshd")
        self.assertEqual(repaired_binary.stdout, upgraded_binary.stdout)
        for user in ("root", "alice"):
            login = self.run_command("ssh", "-F", str(admin_config), "-o", "BatchMode=yes", "-l", user, "cpu1", "true")
            self.assertEqual(login.returncode, 0, f"after package repair, {user}: {login.stdout}{login.stderr}")
        removed = on_vm("sudo", "rm", "-rf", ssh_update.BACKUP)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimStorageUpdateTest(SimUsersBase):
    """A configured backup VM can complete the manual gate for automatic security updates."""

    def test_backup_vm_enables_security_updates_after_manual_check(self) -> None:
        cluster, _, _ = self.up_and_deploy()
        before = self.ssh("store", "apt-config shell enabled APT::Periodic::Unattended-Upgrade")
        self.assertEqual(before.stdout.strip(), "enabled='0'", before.stdout + before.stderr)
        # Lima's base /boot entry and cloud-init Netplan timestamps fail the production restart check.
        baseline = r"""
import os, pathlib, time
fstab = pathlib.Path('/etc/fstab')
lines = []
for line in fstab.read_text().splitlines():
    fields = line.split()
    if len(fields) >= 4 and fields[1] == '/boot' and 'nofail' not in fields[3].split(','):
        fields[3] += ',nofail'
        line = '\t'.join(fields)
    lines.append(line)
fstab.write_text('\n'.join(lines) + '\n')
boot_time = time.time() - float(pathlib.Path('/proc/uptime').read_text().split()[0])
for path in pathlib.Path('/etc/netplan').glob('*.yaml'):
    os.utime(path, (boot_time - 10, boot_time - 10))
"""
        safe = self.ssh("store", "sudo /usr/bin/python3 -c " + shlex.quote(baseline))
        self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
        fixture = """
set -eu
sudo apt-get install -y dpkg-dev
for version in 1 2; do
  folder=/tmp/nanohpc-store-fixture-$version
  mkdir -p "$folder/DEBIAN"
  printf 'Package: nanohpc-store-fixture\\nVersion: %s\\nArchitecture: all\\nMaintainer: nanoHPC test <test@example.invalid>\\nDescription: disposable storage update test\\n' "$version" > "$folder/DEBIAN/control"
  dpkg-deb --build "$folder" "/tmp/nanohpc-store-fixture_${version}_all.deb" >/dev/null
done
sudo dpkg -i /tmp/nanohpc-store-fixture_1_all.deb
sudo rm -f /etc/apt/sources.list.d/*
sudo rm -f /etc/apt/sources.list
sudo find /var/lib/apt/lists -maxdepth 1 -type f ! -name lock -delete
mkdir -p /tmp/nanohpc-store-repo
cp /tmp/nanohpc-store-fixture_2_all.deb /tmp/nanohpc-store-repo/
cd /tmp/nanohpc-store-repo
dpkg-scanpackages . /dev/null > Packages
gzip -kf Packages
echo 'deb [trusted=yes] file:/tmp/nanohpc-store-repo ./' | sudo tee /etc/apt/sources.list.d/nanohpc-store-fixture.list >/dev/null
"""
        prepared = self.ssh("store", "bash -se", stdin=fixture)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        admin_config = self.ssh_config_for("alice")
        environment = {**os.environ, "SSH_AUTH_SOCK": self.agent_socket, "XDG_STATE_HOME": str(self.keys / "state")}
        command = [
            "uv",
            "run",
            "nanohpc",
            "update",
            str(cluster),
            "store",
            "--include",
            "extra",
            "--ssh-config",
            str(admin_config),
        ]
        preview = subprocess.run(
            [*command, "--dry-run"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        applied = subprocess.run(
            [*command, "--confirm", "store"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        version = self.ssh("store", "dpkg-query -W -f='${Version}' nanohpc-store-fixture")
        self.assertEqual(version.stdout.strip(), "2", version.stdout + version.stderr)
        after = self.ssh("store", "apt-config shell enabled APT::Periodic::Unattended-Upgrade")
        self.assertEqual(after.stdout.strip(), "enabled='1'", after.stdout + after.stderr)
        policy = self.ssh("store", "sudo /usr/local/sbin/nanohpc-auto-updates-check")
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)
        state = self.on_front("scontrol show node cpu1 -o")
        self.assertNotIn("DRAIN", state, applied.stdout + applied.stderr)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimFrontUpdateTest(SimUsersBase):
    """A confirmed care-group update on the Ubuntu 24.04 front node protects Slurm jobs."""

    def setUp(self) -> None:
        self.sim = SIM / "kernel-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "kernel-quota"
        super().setUp()

    def test_front_update_on_vm(self) -> None:
        cluster, config, _ = self.up_and_deploy()
        baseline = r"""
import os, pathlib, time
fstab = pathlib.Path('/etc/fstab')
lines = []
for line in fstab.read_text().splitlines():
    fields = line.split()
    if len(fields) >= 4 and fields[1] == '/boot' and 'nofail' not in fields[3].split(','):
        fields[3] += ',nofail'
        line = '\t'.join(fields)
    lines.append(line)
fstab.write_text('\n'.join(lines) + '\n')
boot_time = time.time() - float(pathlib.Path('/proc/uptime').read_text().split()[0])
for path in pathlib.Path('/etc/netplan').glob('*.yaml'):
    os.utime(path, (boot_time - 10, boot_time - 10))
"""
        safe = self.ssh("front", "sudo /usr/bin/python3 -c " + shlex.quote(baseline))
        self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
        setup = """
set -eu
sudo apt-get install -y dpkg-dev
for version in 1 2; do
  folder=/tmp/nanohpc-front-update-fixture-$version
  mkdir -p "$folder/DEBIAN"
  printf 'Package: nanohpc-front-update-fixture\nVersion: %s\nArchitecture: all\nMaintainer: nanoHPC test <test@example.invalid>\nDescription: disposable front update test\n' "$version" > "$folder/DEBIAN/control"
  dpkg-deb --build "$folder" "/tmp/nanohpc-front-update-fixture_${version}_all.deb" >/dev/null
done
sudo dpkg -i /tmp/nanohpc-front-update-fixture_1_all.deb
printf 'Package: *\nPin: release o=Ubuntu\nPin-Priority: -1\n' | sudo tee /etc/apt/preferences.d/nanohpc-front-update-fixture >/dev/null
mkdir -p /tmp/nanohpc-front-update-repo
cp /tmp/nanohpc-front-update-fixture_2_all.deb /tmp/nanohpc-front-update-repo/
cd /tmp/nanohpc-front-update-repo
dpkg-scanpackages . /dev/null > Packages
gzip -kf Packages
echo 'deb [trusted=yes] file:/tmp/nanohpc-front-update-repo ./' | sudo tee /etc/apt/sources.list.d/nanohpc-front-update-fixture.list >/dev/null
"""
        prepared = self.ssh("front", "bash -se", stdin=setup)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        admin_config = self.ssh_config_for("alice")
        environment = {
            **os.environ,
            "SSH_AUTH_SOCK": self.agent_socket,
            "XDG_STATE_HOME": str(self.keys / "state"),
        }
        command = [
            "uv",
            "run",
            "nanohpc",
            "update",
            str(cluster),
            "front",
            "--include",
            "extra",
            "--ssh-config",
            str(admin_config),
        ]
        preview = subprocess.run(
            [*command, "--dry-run"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn("nanohpc-front-update-fixture=2", preview.stdout)
        applied = subprocess.run(
            [*command, "--confirm", "front"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        version = self.ssh("front", "dpkg-query -W -f='${Version}' nanohpc-front-update-fixture")
        self.assertEqual(version.stdout.strip(), "2", version.stdout + version.stderr)
        for name, machine in config["machines"].items():
            if "compute" not in machine["roles"]:
                continue
            state = self.on_front(f"scontrol show node {name} -o")
            self.assertNotIn("DRAIN", state, applied.stdout + applied.stderr)
        for user in ("root", "alice"):
            login = self.run_command("ssh", "-F", str(admin_config), "-o", "BatchMode=yes", "-l", user, "front", "true")
            self.assertEqual(login.returncode, 0, f"{user}: {login.stdout}{login.stderr}")


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimRestartCheckTest(SimUsersBase):
    """The read-only restart check reaches every Ubuntu 24.04 VM and reports a real fstab problem."""

    def setUp(self) -> None:
        self.sim = SIM / "kernel-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "kernel-quota"
        super().setUp()

    def test_restart_check_on_vms(self) -> None:
        cluster, config, _ = self.up_with_test_key()
        names = list(config["machines"])
        changed = self.ssh(
            "front", "echo 'UUID=missing /restart-check-example ext4 defaults 0 2' | sudo tee -a /etc/fstab"
        )
        self.assertEqual(changed.returncode, 0, changed.stderr)
        before = {name: self.ssh(name, "sha256sum /etc/fstab").stdout for name in names}
        result = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "check",
            str(cluster),
            "--before-restart",
            "--ssh-config",
            str(self.state / "ssh_config"),
        )
        self.assertIn("machine", result.stdout, result.stdout + result.stderr)
        for name in names:
            self.assertRegex(result.stdout, rf"(?m)^{name}\s+ok\s+")
            self.assertEqual(self.ssh(name, "sha256sum /etc/fstab").stdout, before[name])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("front: fstab: /restart-check-example has no nofail", result.stdout)
        self.assertNotIn("restart check could not run", result.stdout)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimComputeRestartTest(SimUsersBase):
    """A confirmed restart uses real Slurm and SSH on Ubuntu 24.04 VMs."""

    def setUp(self) -> None:
        self.sim = SIM / "kernel-quota.yml"
        self.state = ROOT / ".nanohpc-sim" / "kernel-quota"
        super().setUp()

    def test_failed_precheck_leaves_cpu_node_drained(self) -> None:
        cluster, _, _ = self.up_and_deploy()
        changed = self.ssh("cpu1", "echo 'UUID=missing /unsafe ext4 defaults 0 2' | sudo tee -a /etc/fstab")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        before = self.ssh("cpu1", "cat /proc/sys/kernel/random/boot_id").stdout
        ssh_config = self.ssh_config_for("alice")
        result = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "restart",
            str(cluster),
            "cpu1",
            "--confirm",
            "cpu1",
            "--ssh-config",
            str(ssh_config),
            agent=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("/unsafe has no nofail", result.stderr)
        self.assertEqual(self.ssh("cpu1", "cat /proc/sys/kernel/random/boot_id").stdout, before)
        state = self.on_front("scontrol show node cpu1 -o")
        self.assertIn("DRAIN", state)

    def test_confirmed_cpu_restart_runs_slurm_job(self) -> None:
        cluster, _, _ = self.up_and_deploy()
        ssh_config = self.ssh_config_for("alice")
        before = self.ssh("cpu1", "cat /proc/sys/kernel/random/boot_id").stdout.strip()
        job = self.on_front(
            "sudo -u alice sbatch --parsable --partition=main --nodelist=cpu1 "
            "--cpus-per-task=1 --mem=1G --time=00:01:00 --chdir=/tmp --output=/dev/null --wrap='sleep 15'"
        ).strip()
        self.assertTrue(job.isdigit(), job)
        self.on_front(
            f"for i in $(seq 20); do state=$(squeue -h -j {job} -o %T); "
            '[ "$state" = RUNNING ] && exit 0; sleep 1; done; exit 1'
        )
        # Lima's base image changes saved network and boot files after boot. The separate
        # failed-precheck VM test covers that refusal; this test exercises the real reboot,
        # SSH, storage, Slurm reservation, and job path with only that precheck bypassed.
        script = (
            "from nanohpc import restart; "
            "restart.POLL_JOBS_SECONDS=1; "
            "restart.before_restart=lambda machine, ssh_config, gpu: "
            "restart.read(machine, ssh_config, 'uname -r', 30); "
            "from nanohpc.cli import main; main()"
        )
        result = self.run_command(
            "uv",
            "run",
            "python",
            "-c",
            script,
            "restart",
            str(cluster),
            "cpu1",
            "--confirm",
            "cpu1",
            "--ssh-config",
            str(ssh_config),
            agent=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"waiting for running jobs: {job}", result.stdout)
        self.assertEqual(self.finished_job(job), ("COMPLETED", "cpu1"))
        after = self.ssh("cpu1", "cat /proc/sys/kernel/random/boot_id").stdout.strip()
        self.assertNotEqual(after, before)
        state = self.on_front("scontrol show node cpu1 -o")
        self.assertNotIn("DRAIN", state)
        self.assertNotIn("MAINT", state)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimSharedDatasetTest(SimUsersBase):
    """Deploy a separate shared datasets server and stage one dataset on a compute VM."""

    sim = SIM / "shared.yml"
    state = ROOT / ".nanohpc-sim" / "shared"

    def test_shared_dataset_from_server_to_local_scratch(self) -> None:
        cluster, config, _ = self.up_with_test_key()
        initial = yaml.safe_load(yaml.safe_dump(config))
        initial["machines"].pop("cpu1")
        cluster.write_text(yaml.safe_dump(initial))
        deployed = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(deployed.returncode, 0, self.failure(deployed))
        cluster.write_text(yaml.safe_dump(config))
        added = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--only", "node", "cpu1")
        self.assertEqual(added.returncode, 0, self.failure(added))
        self.assertRegex(added.stdout, r"\nDry run: cpu1 would change \d+ things: ")
        self.assertEqual(self.ssh("store", "findmnt -n -o SOURCE --mountpoint /shared").stdout.strip(), "/dev/vdb")
        source = yaml.safe_load((self.state / "cluster.yml").read_text())["machines"]["store"]["address"]
        for machine in ("gpu4", "cpu1"):
            mount = self.ssh(machine, "findmnt -n -o SOURCE,FSTYPE,OPTIONS --mountpoint /shared/datasets")
            self.assertEqual(mount.returncode, 0, mount.stderr)
            mounted_source, filesystem, options = mount.stdout.split()
            self.assertEqual(mounted_source, f"{source}:/shared/datasets")
            self.assertEqual(filesystem, "nfs4")
            self.assertTrue({"ro", "nosuid", "nodev"} <= set(options.split(",")))
        created = self.ssh(
            "store",
            "sudo mkdir -p /shared/datasets/example && "
            "printf 'v1\\n' | sudo tee /shared/datasets/example/.dataset-version >/dev/null && "
            "printf 'dataset\\n' | sudo tee /shared/datasets/example/data.txt >/dev/null && "
            "sudo chmod -R a+rX /shared/datasets/example",
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        refused = self.ssh("gpu4", "sudo -u alice touch /shared/datasets/example/forbidden")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        staged = self.as_user("alice", "gpu4", "stage-dataset --shared example", agent=False)
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        destination = staged.stdout.strip()
        self.assertTrue(destination.startswith("/scratch/staged/shared/2000/example/v1"))
        self.assertEqual(
            self.ssh("gpu4", f"sudo -u alice cat {shlex.quote(destination)}/data.txt").stdout.strip(), "dataset"
        )
        again = self.as_user("alice", "gpu4", "stage-dataset --shared example", agent=False)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(again.stdout.strip(), destination)
        self.assertNotEqual(self.ssh("gpu4", f"sudo -u bob ls {shlex.quote(destination)}").returncode, 0)
        for machine in ("store", "gpu4", "cpu1"):
            health = self.ssh(machine, "sudo cluster-health")
            self.assertEqual(health.returncode, 0, f"{machine}: {health.stdout}{health.stderr}")


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimHomeOnStorageTest(SimUsersBase):
    """/home served by the storage machine instead of the front node. Real Lima VMs."""

    def setUp(self) -> None:
        self.sim = SIM / "home-on-storage.yml"
        self.state = ROOT / ".nanohpc-sim" / "home-on-storage"
        super().setUp()

    def test_nfs_option_change_waits_for_compute_job(self) -> None:
        """A compute node drains before /home switches and returns after its job ends."""
        self.up_and_deploy()
        machine = "gpu4"
        admin_ssh = self.ssh_config_for("alice")
        if "DRAIN" in self.on_front(f"sudo scontrol show node {machine} -o"):
            self.on_front(f"sudo scontrol update nodename={machine} state=resume")
        self.addCleanup(self.ssh, "front", f"sudo scontrol update nodename={machine} state=resume")
        old = self.ssh(
            machine,
            "cd / && sudo sed -i 's/timeo=600/timeo=50/' /etc/fstab && sudo umount /home && sudo mount /home",
        )
        self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
        self.assertIn("timeo=50", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout)

        # This job keeps /home busy until Slurm marks its machine as drained.
        script = (
            "cd /home/alice; "
            "while ! scontrol show node gpu4 -o | grep -Eq 'State=[^ ]*DRAIN'; do sleep 1; done; "
            "sleep 5"
        )
        job = self.on_front(
            "cd /tmp && sudo -u alice sbatch --parsable -p main -w gpu4 -t 20 -o /dev/null --wrap "
            + shlex.quote(script)
        ).strip()
        self.addCleanup(self.ssh, "front", f"sudo scancel {job}")
        for _ in range(30):
            state = self.on_front(f"sudo squeue -h -j {job} -o '%T %N'").strip()
            if state == "RUNNING gpu4":
                break
            time.sleep(2)
        self.assertEqual(state, "RUNNING gpu4")

        changed = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "sim",
            "deploy",
            str(self.sim),
            "--only",
            "node",
            machine,
            "--ssh-config",
            str(admin_ssh),
            agent=True,
        )
        self.assertEqual(changed.returncode, 0, self.failure(changed))
        self.assertEqual(self.finished_job(job), ("COMPLETED", machine))
        self.assertIn("timeo=600", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout)
        self.assertNotIn("DRAIN", self.on_front(f"sudo scontrol show node {machine} -o"))

    def test_nfs_option_change_uses_new_mount(self) -> None:
        """A deploy applies an NFS-specific /home option without rebooting the client."""
        self.up_and_deploy()
        machine = "gpu4"
        admin_ssh = self.ssh_config_for("alice")
        self.addCleanup(self.ssh, "front", f"sudo scontrol update nodename={machine} state=resume")
        self.on_front("sudo -u alice sh -c 'echo before > /home/alice/check'")
        boot = self.ssh(machine, "cat /proc/sys/kernel/random/boot_id").stdout.strip()
        old = self.ssh(
            machine,
            "cd / && sudo sed -i 's/timeo=600/timeo=50/' /etc/fstab && sudo umount /home && sudo mount /home",
        )
        self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
        options = self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout.strip().split(",")
        self.assertIn("timeo=50", options)
        before = self.ssh(machine, "cat /etc/fstab; findmnt -n -o OPTIONS --mountpoint /home").stdout

        dry = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "sim",
            "deploy",
            str(self.sim),
            "--dry-run",
            "--ssh-config",
            str(admin_ssh),
            agent=True,
        )
        self.assertEqual(dry.returncode, 0, self.failure(dry))
        self.assertEqual(self.ssh(machine, "cat /etc/fstab; findmnt -n -o OPTIONS --mountpoint /home").stdout, before)

        result = self.deploy(admin_ssh, agent=True)
        self.assertEqual(result.returncode, 0, self.failure(result))
        options = self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout.strip().split(",")
        self.assertIn("timeo=600", options)
        self.assertIn("nosuid", options)
        self.assertIn("nodev", options)
        self.assertEqual(self.ssh(machine, "cat /proc/sys/kernel/random/boot_id").stdout.strip(), boot)
        self.assertEqual(self.ssh(machine, "sudo -u alice cat /home/alice/check").returncode, 0)

        # A busy /home stays mounted with the old options. Once the user process exits,
        # a later deploy must retry even if the previous run touched /etc/fstab.
        old = self.ssh(
            machine,
            "cd / && sudo sed -i 's/timeo=600/timeo=50/' /etc/fstab && sudo umount /home && sudo mount /home",
        )
        self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
        held = self.ssh(
            machine,
            "sudo systemd-run --unit=nanohpc-home-busy --property=WorkingDirectory=/home/alice /bin/sleep 1800",
        )
        self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
        self.addCleanup(self.ssh, machine, "sudo systemctl stop nanohpc-home-busy.service")
        for _ in range(20):
            holder = self.ssh(
                machine,
                "sudo readlink /proc/$(systemctl show -p MainPID --value nanohpc-home-busy.service)/cwd",
            )
            if holder.stdout.strip() == "/home/alice":
                break
            time.sleep(0.5)
        self.assertEqual(holder.stdout.strip(), "/home/alice", holder.stdout + holder.stderr)
        busy = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "sim",
            "deploy",
            str(self.sim),
            "--only",
            "node",
            machine,
            "--ssh-config",
            str(admin_ssh),
            agent=True,
        )
        self.assertNotEqual(busy.returncode, 0, self.failure(busy))
        self.assertIn("needs a fresh NFS mount", busy.stdout)
        self.assertIn("DRAIN", self.on_front(f"sudo scontrol show node {machine} -o"))
        self.assertIn("timeo=50", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout)
        self.assertIn("timeo=50", self.ssh(machine, "awk '$2 == \"/home\" { print }' /etc/fstab").stdout)
        self.assertEqual(self.ssh(machine, "sudo systemctl stop nanohpc-home-busy.service").returncode, 0)

        retried = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "sim",
            "deploy",
            str(self.sim),
            "--only",
            "node",
            machine,
            "--ssh-config",
            str(admin_ssh),
            agent=True,
        )
        self.assertEqual(retried.returncode, 0, self.failure(retried))
        self.assertIn("timeo=600", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout)
        self.assertIn("DRAIN", self.on_front(f"sudo scontrol show node {machine} -o"))

        # Fail the new mount once. The rescue must restore the old fstab entry and mount.
        old = self.ssh(
            machine,
            "cd / && sudo sed -i 's/timeo=600/timeo=50/' /etc/fstab && sudo umount /home && sudo mount /home",
        )
        self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
        wrapper = """#!/bin/sh
if [ "$1" = /home ] && [ -e /tmp/nanohpc-fail-new-home-mount ] &&
   grep -q ' /home nfs4 .*timeo=600' /etc/fstab; then
    rm /tmp/nanohpc-fail-new-home-mount
    echo 'injected new mount failure' >&2
    exit 42
fi
exec /usr/bin/mount "$@"
"""
        installed = self.ssh(machine, "sudo tee /usr/local/bin/mount >/dev/null", stdin=wrapper)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.addCleanup(self.ssh, machine, "sudo rm -f /usr/local/bin/mount /tmp/nanohpc-fail-new-home-mount")
        prepared = self.ssh(
            machine, "sudo chmod 755 /usr/local/bin/mount && sudo touch /tmp/nanohpc-fail-new-home-mount"
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        self.assertEqual(self.ssh(machine, "sudo sh -c 'command -v mount'").stdout.strip(), "/usr/local/bin/mount")
        failed_mount = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "sim",
            "deploy",
            str(self.sim),
            "--only",
            "node",
            machine,
            "--ssh-config",
            str(admin_ssh),
            agent=True,
        )
        self.assertNotEqual(failed_mount.returncode, 0, self.failure(failed_mount))
        self.assertIn("injected new mount failure", failed_mount.stdout)
        self.assertIn("timeo=50", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout)
        self.assertIn("timeo=50", self.ssh(machine, "awk '$2 == \"/home\" { print }' /etc/fstab").stdout)
        self.assertIn("DRAIN", self.on_front(f"sudo scontrol show node {machine} -o"))

        self.assertEqual(self.ssh(machine, "sudo rm /usr/local/bin/mount").returncode, 0)
        recovered = self.run_command(
            "uv",
            "run",
            "nanohpc",
            "sim",
            "deploy",
            str(self.sim),
            "--only",
            "node",
            machine,
            "--ssh-config",
            str(admin_ssh),
            agent=True,
        )
        self.assertEqual(recovered.returncode, 0, self.failure(recovered))
        self.assertIn("timeo=600", self.ssh(machine, "findmnt -n -o OPTIONS --mountpoint /home").stdout)
        self.assertIn("DRAIN", self.on_front(f"sudo scontrol show node {machine} -o"))

    def test_home_on_storage_machine(self) -> None:
        # A machine whose local /home holds data stops before the shared /home could hide it.
        self.remove_at_end()
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(self.sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ssh("gpu2", "sudo mkdir /home/olddata").returncode, 0)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assert_left_out(result, "gpu2")
        self.assertIn("gpu2: /home holds olddata", result.stdout)
        self.assertNotIn("nfs", self.ssh("gpu2", "findmnt -n -o FSTYPE --target /home").stdout)
        self.assertEqual(self.ssh("gpu2", "sudo rmdir /home/olddata").returncode, 0)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        store = yaml.safe_load((self.state / "cluster.yml").read_text())["machines"]["store"]["address"]
        for machine in ("front", "gpu4", "gpu2"):
            self.assertEqual(
                self.ssh(machine, "findmnt -n -o SOURCE --mountpoint /home").stdout.strip(), f"{store}:/home", machine
            )
        self.assertEqual(self.ssh("store", "findmnt -n -o SOURCE --mountpoint /home").stdout.strip(), "/dev/vdb")
        self.on_front("sudo -u alice sh -c 'echo shared > /home/alice/check'")
        self.assertEqual(self.ssh("gpu4", "sudo -u alice cat /home/alice/check").stdout.strip(), "shared")
        # The VM's default account still logs in on the front node, where its own home is now hidden.
        self.assertEqual(self.ssh("front", "true").returncode, 0)

        # These are the existing mounts that a later deploy must update without a reboot.
        mounts = [("store", "/home"), ("front", "/home"), ("gpu4", "/scratch"), ("gpu2", "/scratch")]
        for machine, target in mounts:
            edit = (
                "from pathlib import Path; p = Path('/etc/fstab'); "
                "p.write_text(''.join(line.replace(',nodev', '').replace(',nosuid', '') "
                f"if ' {target} ' in line else line for line in p.read_text().splitlines(keepends=True)))"
            )
            changed = self.ssh(
                machine, f"sudo python3 -c {shlex.quote(edit)} && sudo mount -o remount,suid,dev {target}"
            )
            self.assertEqual(changed.returncode, 0, f"{machine} {target}: {changed.stdout}{changed.stderr}")
            options = self.ssh(machine, f"findmnt -n -o OPTIONS --mountpoint {target}").stdout.strip().split(",")
            self.assertNotIn("nodev", options, (machine, target, options))
            self.assertNotIn("nosuid", options, (machine, target, options))

        before = {
            (machine, target): self.ssh(machine, f"cat /etc/fstab; findmnt -n -o OPTIONS --mountpoint {target}").stdout
            for machine, target in mounts
        }
        dry = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim), "--dry-run")
        self.assertEqual(dry.returncode, 0, self.failure(dry))
        for machine, target in mounts:
            self.assertEqual(
                self.ssh(machine, f"cat /etc/fstab; findmnt -n -o OPTIONS --mountpoint {target}").stdout,
                before[machine, target],
                f"dry run changed {machine} {target}",
            )

        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, self.failure(result))
        for machine, target in mounts:
            options = self.ssh(machine, f"findmnt -n -o OPTIONS --mountpoint {target}").stdout.strip().split(",")
            self.assertIn("nodev", options, (machine, target, options))
            self.assertIn("nosuid", options, (machine, target, options))
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assert_no_changes(result, 6)
        # The quotas read on the storage machine reach the front node's status snapshot.
        for _ in range(30):
            status = json.loads(self.on_front("cat /var/lib/nanohpc/monitor/status.json"))
            alice = next(card for card in status["users"] if card["user"] == "alice")
            if alice["home"] is not None:
                break
            time.sleep(5)
        self.assertIsNotNone(alice["home"], status["users"])
        self.assertGreater(alice["home"]["soft_bytes"], 0)
        self.assertEqual({node["name"]: node["role"] for node in status["nodes"]}["store"], "Storage")


if __name__ == "__main__":
    unittest.main()
