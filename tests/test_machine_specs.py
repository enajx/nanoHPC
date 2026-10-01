"""Exercise the machine specs command with labelled fake hardware and package boundaries."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-machine-specs"

PROBE = """import os,sys,json
from pathlib import Path
name=Path(sys.argv[0]).name
if os.environ.get('FAIL') == name: sys.exit(1)
if name == 'lscpu':
    fields = {'Model name':'Test CPU','CPU(s)':'32','Socket(s)':'1','Core(s) per socket':'16','Thread(s) per core':'2'}
    if os.environ.get('LSCPU_ARM'):
        # ARM machines and VMs report clusters, not sockets, and may have no model name.
        fields = {'Model name':'-','Vendor ID':'Apple','CPU(s)':'4','Socket(s)':'-','Core(s) per cluster':'4','Thread(s) per core':'1'}
    print(json.dumps({'lscpu':[{'field': k+':','data':v} for k,v in fields.items()]}))
elif name == 'nvidia-smi':
    print('<nvidia_smi_log><driver_version>580.178.04</driver_version><cuda_version>13.0</cuda_version>'
          '<gpu><minor_number>0</minor_number><product_name>RTX A6000</product_name>'
          '<fb_memory_usage><total>49140 MiB</total></fb_memory_usage></gpu></nvidia_smi_log>')
"""

FAKE_APT = """import os
class P: is_upgradable=True
def Cache():
    if os.environ.get("FAIL") == "apt": raise RuntimeError("test failure")
    return [P(),P()]
"""


class MachineSpecsTests(unittest.TestCase):
    """Only complete readings replace the exporter file; machines without GPUs still report."""

    def make_root(self, directory: str, commands: list[str]) -> tuple[Path, dict[str, str]]:
        """Build a temporary OS filesystem and a PATH holding only the given fake commands (labelled fakes)."""
        root = Path(directory)
        for relative in ["etc", "run", "var/lib/apt/periodic", "commands"]:
            (root / relative).mkdir(parents=True)
        (root / "etc/os-release").write_text('ID=ubuntu\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\n')
        (root / "var/lib/apt/periodic/update-success-stamp").touch()
        probe = root / "commands/probe"
        probe.write_text(f"#!{sys.executable}\n" + PROBE)
        probe.chmod(0o700)
        for name in commands:
            (root / "commands" / name).symlink_to(probe)
        (root / "commands/apt.py").write_text(FAKE_APT)
        # The system PATH is kept for python3, but must not provide a system nvidia-smi or lscpu.
        system_path = os.pathsep.join(
            entry
            for entry in os.environ["PATH"].split(os.pathsep)
            if not (Path(entry) / "nvidia-smi").exists() and not (Path(entry) / "lscpu").exists()
        )
        env = dict(
            os.environ,
            PATH=str(root / "commands") + os.pathsep + system_path,
            PYTHONPATH=str(root / "commands"),
        )
        return root, env

    def test_spec_collection_and_failure(self) -> None:
        """Run the actual command on a GPU machine with fake commands and a temporary OS filesystem."""
        with tempfile.TemporaryDirectory() as directory:
            root, env = self.make_root(directory, ["lscpu", "nvidia-smi"])
            (root / "usr/local/cuda-13.0").mkdir(parents=True)
            (root / "usr/local/cuda-13.0/version.json").write_text('{"cuda":{"version":"13.0.3"}}')
            output = root / "specs.prom"
            args = [sys.executable, str(SCRIPT), "--root", str(root), "--output", str(output)]
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = output.read_text()
            for expected in [
                'os="Ubuntu 24.04.3 LTS"',
                'driver="580.178.04"',
                'cuda_driver="13.0"',
                'cuda_toolkits="13.0.3"',
                "cluster_machine_cpu_cores 16",
                "cluster_machine_cpu_threads 32",
                "cluster_machine_gpu_count 1",
                "cluster_machine_pending_updates 2",
                "cluster_machine_needs_restart 0",
                'cluster_machine_gpu_memory_bytes{gpu="0",model="RTX A6000"} 51527024640',
                "cluster_machine_updates_checked_timestamp_seconds ",
                "cluster_machine_specs_timestamp_seconds ",
            ]:
                self.assertIn(expected, content)
            self.assertEqual(oct(output.stat().st_mode & 0o777), oct(0o644))
            for source in ["apt", "nvidia-smi", "lscpu"]:
                result = subprocess.run(args, env=dict(env, FAIL=source), capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_text(), content)
            (root / "run/reboot-required").touch()
            (root / "var/lib/apt/periodic/update-success-stamp").unlink()
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cluster_machine_needs_restart 1", output.read_text())
            self.assertNotIn("cluster_machine_pending_updates ", output.read_text())

    def test_machine_without_gpus_or_driver(self) -> None:
        """Without nvidia-smi or CUDA, the machine reports zero GPUs and 'none' versions."""
        with tempfile.TemporaryDirectory() as directory:
            root, env = self.make_root(directory, ["lscpu"])
            output = root / "specs.prom"
            args = [sys.executable, str(SCRIPT), "--root", str(root), "--output", str(output)]
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = output.read_text()
            for expected in [
                'driver="none"',
                'cuda_driver="none"',
                'cuda_toolkits="none"',
                "cluster_machine_gpu_count 0",
                "cluster_machine_cpu_cores 16",
                "cluster_machine_pending_updates 2",
                "cluster_machine_specs_timestamp_seconds ",
            ]:
                self.assertIn(expected, content)
            self.assertNotIn("cluster_machine_gpu_memory_bytes", content)

    def test_arm_machine(self) -> None:
        """ARM lscpu reports clusters instead of sockets: cores come from CPUs and threads per core."""
        with tempfile.TemporaryDirectory() as directory:
            root, env = self.make_root(directory, ["lscpu"])
            env["LSCPU_ARM"] = "1"
            output = root / "specs.prom"
            args = [sys.executable, str(SCRIPT), "--root", str(root), "--output", str(output)]
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = output.read_text()
            self.assertIn("cluster_machine_cpu_cores 4", content)
            self.assertIn("cluster_machine_cpu_threads 4", content)
            self.assertIn('cpu_model="Apple"', content)

    def test_machine_with_fake_gpus(self) -> None:
        """On a simulated machine, the fake GPU count and type stand in for nvidia-smi."""
        with tempfile.TemporaryDirectory() as directory:
            root, env = self.make_root(directory, ["lscpu"])
            output = root / "specs.prom"
            args = [sys.executable, str(SCRIPT), "--root", str(root), "--output", str(output)]
            fake = ["--fake-gpu-count", "2", "--fake-gpu-type", "a6000"]
            result = subprocess.run(args + fake, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = output.read_text()
            self.assertIn('driver="none"', content)
            self.assertIn("cluster_machine_gpu_count 2", content)
            self.assertIn('cluster_machine_gpu_memory_bytes{gpu="0",model="a6000"} ', content)
            self.assertIn('cluster_machine_gpu_memory_bytes{gpu="1",model="a6000"} ', content)
            for partial in (["--fake-gpu-count", "2"], ["--fake-gpu-type", "a6000"], ["--fake-gpu-count", "0"]):
                result = subprocess.run(args + partial, env=env, capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_text(), content)


if __name__ == "__main__":
    unittest.main()
