"""Run the installed speed-test command against local files and inspect its exporter output."""

import functools
import hashlib
import http.server
import importlib.machinery
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

import jinja2
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-speed-test"
ROLE = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/machine_metrics/tasks/main.yml"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serve files from a folder without logging each request."""

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(test: unittest.TestCase, folder: Path) -> int:
    """Serve the folder over HTTP on 127.0.0.1 for the length of the test; return the port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return server.server_address[1]


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
                    f"http://127.0.0.1:{serve(self, base)}/download.bin",
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
                    f"http://127.0.0.1:{serve(self, base)}/download.bin",
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


class DownloadTests(unittest.TestCase):
    """The download moves past server addresses that never answer."""

    def test_address_that_never_answers_is_skipped(self) -> None:
        """A network that drops one of a server's addresses must not make the download read 0.

        Injected: the name resolves to an address that never answers (192.0.2.1, a reserved test address), then to
        a local server. The download must move on within a few seconds and report a speed above 0.
        """
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "download.bin").write_bytes(b"a" * (4 * 1024 * 1024))
            port = serve(self, base)
            loader = importlib.machinery.SourceFileLoader("cluster_speed_test", str(SCRIPT))
            module = types.ModuleType(loader.name)
            loader.exec_module(module)
            real_getaddrinfo = socket.getaddrinfo

            def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
                if host != "speed-test.invalid":
                    return real_getaddrinfo(host, *args, **kwargs)
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
                ]

            with unittest.mock.patch("socket.getaddrinfo", getaddrinfo):
                start = time.monotonic()
                speed = module.download(f"http://speed-test.invalid:{port}/download.bin", 3)
            self.assertGreater(speed, 0)
            self.assertLess(time.monotonic() - start, 12)
