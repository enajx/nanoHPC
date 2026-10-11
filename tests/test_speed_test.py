"""Run the installed speed-test command against local files and inspect its exporter output."""

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-speed-test"
ROLE = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/machine_metrics/tasks/main.yml"


class SpeedTestCommandTests(unittest.TestCase):
    """Check the real command entry point, without a remote download."""

    def test_timer_time_survives_a_new_machine(self) -> None:
        """Adding a node does not reschedule speed tests on existing nodes."""
        tasks = yaml.safe_load(ROLE.read_text())
        content = next(
            task["ansible.builtin.copy"]["content"]
            for task in tasks
            if task["name"] == "Install the nightly speed-test timer"
        )
        environment = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
        environment.filters["hash"] = lambda value, algorithm: hashlib.new(algorithm, value.encode()).hexdigest()
        template = environment.from_string(content)
        original = ["cpu1", "front", "gpu2", "gpu4", "store"]
        expanded = ["cpu1", "front", "gpu2", "gpu4", "gpu4i", "store"]
        for machine in original:
            with self.subTest(machine):
                before = template.render(inventory_hostname=machine, groups={"all": original})
                after = template.render(inventory_hostname=machine, groups={"all": expanded})
                self.assertEqual(before, after)

        # Both names can occur in supported clusters and share the same /home link.
        schedules = {
            machine: next(
                line
                for line in template.render(inventory_hostname=machine, groups={"all": expanded}).splitlines()
                if line.startswith("OnCalendar=")
            )
            for machine in [*expanded, "b200"]
        }
        self.assertEqual(len(set(schedules.values())), len(schedules), schedules)

    def test_systemd_service_has_separate_settings_for_each_role(self) -> None:
        """The rendered service keeps MemoryMax separate from its writable paths."""
        tasks = yaml.safe_load(ROLE.read_text())
        content = next(
            task["ansible.builtin.copy"]["content"]
            for task in tasks
            if task["name"] == "Install the nightly speed-test service"
        )
        template = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined).from_string(content)
        groups = {"role_compute": ["cpu1"], "all": ["front", "cpu1"]}
        for machine, home in (("front", False), ("cpu1", True)):
            with self.subTest(machine):
                lines = template.render(
                    inventory_hostname=machine, groups=groups, nanohpc={"mode": "slurm"}
                ).splitlines()
                self.assertIn("MemoryMax=256M", lines)
                paths = next(line for line in lines if line.startswith("ReadWritePaths="))
                self.assertEqual("/home/.nanohpc-speed-test" in paths, home)
                self.assertEqual("RequiresMountsFor=/home/.nanohpc-speed-test" in lines, home)

    def test_home_and_internet_results(self) -> None:
        """A completed run publishes five measurements and removes its test files."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            folder = base / "home"
            folder.mkdir()
            source = base / "download.bin"
            source.write_bytes(b"a" * (4 * 1024 * 1024))
            output = base / "speed.prom"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--folder",
                    str(folder),
                    "--output",
                    str(output),
                    "--url",
                    source.as_uri(),
                    "--large-mb",
                    "4",
                    "--small-files",
                    "2",
                    "--internet-seconds",
                    "1",
                ],
                check=True,
            )
            data = output.read_text()
            for name in (
                "home_large_read",
                "home_large_write",
                "home_small_read",
                "home_small_write",
                "internet_download",
            ):
                self.assertIn(f'test="{name}"', data)
            self.assertIn("cluster_machine_speed_timestamp_seconds", data)
            self.assertEqual(list(folder.iterdir()), [])

    def test_internet_only_for_other_machine_roles(self) -> None:
        """Front, storage, and monitor machines publish only the download reading."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "download.bin"
            source.write_bytes(b"a" * (4 * 1024 * 1024))
            output = base / "speed.prom"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--internet-only",
                    "--output",
                    str(output),
                    "--url",
                    source.as_uri(),
                    "--internet-seconds",
                    "1",
                ],
                check=True,
            )
            data = output.read_text()
            self.assertIn('test="internet_download"', data)
            self.assertNotIn('test="home_', data)


if __name__ == "__main__":
    unittest.main()
