"""Tests for the simulated test cluster (`nanohpc sim up/down`).

The unit tests need no VMs. `SimClusterTest` starts real Lima VMs and runs only when NANOHPC_SIM=1 is set,
because it takes minutes and several GB of memory.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import check_config
from nanohpc.sim import SimPlan, load_sim, render_cluster, render_ssh_config

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
        for name, machines in [("everyday", 6), ("home-on-storage", 6), ("large", 21)]:
            with self.subTest(name):
                plan, errors = load_sim(SIM / f"{name}.yml")
                self.assertEqual(errors, [])
                assert plan is not None
                self.assertEqual(len(plan.vms), machines)

    def test_vm_sizes_and_disks(self) -> None:
        plan = plan_of(SIM / "everyday.yml")
        vms = {vm.machine: vm for vm in plan.vms}
        self.assertEqual((vms["front"].cpus, vms["front"].memory_gb, vms["front"].disk_gb), (2, 2, 20))
        self.assertEqual((vms["cpu1"].cpus, vms["cpu1"].memory_gb), (1, 1))
        self.assertEqual(vms["front"].instance, "nanohpc-everyday-front")
        # One extra disk per device path, attached in device order (/dev/vdb, /dev/vdc, ...).
        self.assertEqual(vms["front"].disks, ["nanohpc-everyday-front-vdb"])
        self.assertEqual(vms["gpu4"].disks, ["nanohpc-everyday-gpu4-vdb"])
        self.assertEqual(vms["gpu2"].disks, [])
        self.assertEqual(plan.fake_gpus, ["gpu4", "gpu2", "gpu4i"])

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


class SimOutputTest(unittest.TestCase):
    """The files `sim up` writes: the cluster.yml with real VM addresses, and the SSH machine list."""

    def test_cluster_gets_vm_addresses_and_stays_valid(self) -> None:
        plan = plan_of(SIM / "everyday.yml")
        addresses = {vm.machine: f"192.168.104.{50 + index}" for index, vm in enumerate(plan.vms)}
        text = render_cluster(plan, addresses)
        config, errors = check_config(yaml.safe_load(text))
        self.assertEqual(errors, [])
        self.assertEqual({name: machine["address"] for name, machine in config["machines"].items()}, addresses)
        self.assertTrue(text.startswith("# Generated by nanohpc sim up"))

    def test_ssh_config_lists_every_machine(self) -> None:
        plan = plan_of(SIM / "everyday.yml")
        ports = {vm.machine: 60000 + index for index, vm in enumerate(plan.vms)}
        text = render_ssh_config(plan, ports, "enaj", Path("/home/enaj/.lima/_config/user"))
        for machine, port in ports.items():
            self.assertIn(f"Host {machine}\n  HostName 127.0.0.1\n  Port {port}\n  User enaj\n", text)
        self.assertIn("IdentityFile /home/enaj/.lima/_config/user", text)


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
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try_down = True
        self.addCleanup(lambda: try_down and self.run_command("uv", "run", "nanohpc", "sim", "down", str(sim)))

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


if __name__ == "__main__":
    unittest.main()
