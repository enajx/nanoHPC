"""Test the GPU metrics commands: the nvidia-smi collector with a labelled fake nvidia-smi, and the fake-GPU collector."""

import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FILES = Path(__file__).resolve().parents[1] / "src/nanohpc/files"
SCRIPT = FILES / "cluster-gpu-metrics"
FAKE_SCRIPT = FILES / "cluster-fake-gpu-metrics"
SAMPLE = re.compile(r"^(\w+)(?:\{(.*)\})? (\S+)$")


def samples(text: str) -> list[tuple[str, str, float]]:
    """Parse textfile lines into (metric name, labels, value), skipping comments."""
    parsed: list[tuple[str, str, float]] = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        match = SAMPLE.match(line)
        if match is None:
            raise AssertionError(f"Not a sample line: {line!r}")
        parsed.append((match[1], match[2] or "", float(match[3])))
    return parsed


def label_names(labels: str) -> list[str]:
    """Return the label names of one sample, in order."""
    return re.findall(r'(\w+)="', labels)


class GpuMetricsTests(unittest.TestCase):
    """GPU readings must keep units and never replace failed readings with zeros."""

    def test_values_and_failure(self) -> None:
        """Exercise units, unsupported fields, malformed input, and command failure (fake nvidia-smi)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "nvidia-smi"
            fake.write_text(
                f"#!{sys.executable}\nimport os,sys\n"
                "print(os.environ['GPU_FIXTURE'])\n"
                "sys.exit(int(os.environ.get('GPU_EXIT','0')))\n"
            )
            fake.chmod(0o700)
            output = root / "gpu.prom"
            args = [sys.executable, str(SCRIPT), "--output", str(output)]
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                GPU_FIXTURE="0, GPU-aaaa, 25, 1024, 49140, 34, 120.50\n1, GPU-bbbb, 0, 1, 49140, 29, [N/A]",
            )
            result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = output.read_text()
            self.assertIn('cluster_gpu_memory_used_bytes{gpu="0",uuid="GPU-aaaa"} 1073741824', text)
            self.assertIn('cluster_gpu_utilization_percent{gpu="0",uuid="GPU-aaaa"} 25', text)
            self.assertIn('cluster_gpu_power_watts{gpu="0",uuid="GPU-aaaa"} 120.5', text)
            self.assertNotIn('cluster_gpu_power_watts{gpu="1"', text)
            self.assertIn("cluster_gpu_collection_timestamp_seconds ", text)
            self.assertEqual(oct(output.stat().st_mode & 0o777), oct(0o644))
            for bad in (
                {"GPU_EXIT": "1"},
                {"GPU_FIXTURE": "invalid"},
                {"GPU_FIXTURE": "0, GPU-aaaa, NaN, 1, 2, 30, 10"},
            ):
                result = subprocess.run(args, env=dict(env, **bad), capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_text(), text)


class FakeGpuMetricsTests(unittest.TestCase):
    """Simulated machines without a GPU driver publish the same series as machines with NVIDIA GPUs."""

    def run_fake(self, output: Path, count: str, gpu_type: str) -> subprocess.CompletedProcess[str]:
        """Run the fake collector command."""
        return subprocess.run(
            [sys.executable, str(FAKE_SCRIPT), "--count", count, "--type", gpu_type, "--output", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_same_series_as_real_collector(self) -> None:
        """Every nvidia-smi collector metric name appears per GPU with the same labels, and values are plausible."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gpu.prom"
            result = self.run_fake(output, "2", "a6000")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(oct(output.stat().st_mode & 0o777), oct(0o644))
            text = output.read_text()
            parsed = samples(text)
            per_gpu = [
                "cluster_gpu_utilization_percent",
                "cluster_gpu_memory_used_bytes",
                "cluster_gpu_memory_total_bytes",
                "cluster_gpu_temperature_celsius",
                "cluster_gpu_power_watts",
            ]
            for name in per_gpu:
                self.assertIn(f"# TYPE {name} gauge", text)
                found = [(labels, value) for metric, labels, value in parsed if metric == name]
                self.assertEqual(len(found), 2, name)
                for gpu, (labels, value) in enumerate(found):
                    self.assertEqual(label_names(labels), ["gpu", "uuid"])
                    self.assertIn(f'gpu="{gpu}"', labels)
                    self.assertIn("a6000", labels)
                    self.assertGreaterEqual(value, 0)
            values = {(metric, labels): value for metric, labels, value in parsed}
            for gpu in range(2):
                labels = f'gpu="{gpu}",uuid="GPU-fake-a6000-{gpu}"'
                self.assertLessEqual(values[("cluster_gpu_utilization_percent", labels)], 100)
                self.assertLessEqual(
                    values[("cluster_gpu_memory_used_bytes", labels)],
                    values[("cluster_gpu_memory_total_bytes", labels)],
                )
                self.assertTrue(20 <= values[("cluster_gpu_temperature_celsius", labels)] <= 90)
                self.assertTrue(0 < values[("cluster_gpu_power_watts", labels)] <= 400)
            self.assertIn("# TYPE cluster_gpu_collection_timestamp_seconds gauge", text)
            self.assertIn(("cluster_gpu_collection_timestamp_seconds", ""), values)

    def test_values_vary_over_time_and_between_gpus(self) -> None:
        """Utilization depends on the time and the GPU index, so dashboards show movement."""
        implementation = runpy.run_path(str(FAKE_SCRIPT))
        reading = implementation["reading"]
        self.assertEqual(reading(0, 1000.0), reading(0, 1000.0))
        self.assertNotEqual(reading(0, 1000.0), reading(0, 1300.0))
        self.assertNotEqual(reading(0, 1000.0), reading(1, 1000.0))
        utilizations = [reading(0, float(moment))["utilization_percent"] for moment in range(0, 3600, 30)]
        self.assertTrue(all(0 <= value <= 100 for value in utilizations))
        self.assertGreater(max(utilizations) - min(utilizations), 50)

    def test_bad_arguments_keep_previous_file(self) -> None:
        """A bad GPU count fails and leaves the previous file alone."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gpu.prom"
            output.write_text("previous\n")
            for count in ("0", "-1", "two"):
                result = self.run_fake(output, count, "a6000")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_text(), "previous\n")


if __name__ == "__main__":
    unittest.main()
