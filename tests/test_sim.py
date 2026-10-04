"""Tests for the simulated test cluster (`nanohpc sim up/down`).

The unit tests need no VMs. `SimClusterTest` starts real Lima VMs and runs only when NANOHPC_SIM=1 is set,
because it takes minutes and several GB of memory.
"""

import asyncio
import json
import os
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

from nanohpc import fixuid, probe, wizard
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
        for name, machines in [("everyday", 6), ("home-on-storage", 6), ("large", 21)]:
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
        text = (self.state / "ssh_config").read_text()
        lines = []
        for line in text.splitlines():
            if line.strip().startswith("User "):
                line = f"  User {user}"
            elif line.strip().startswith("IdentityFile "):
                line = f"  IdentityFile {self.keys / 'id'}\n  ForwardAgent yes"
            lines.append(line)
        path = self.keys / f"ssh_config_{user}"
        path.write_text("\n".join(lines) + "\n")
        return path

    def as_user(self, user: str, machine: str, command: str, agent: bool) -> subprocess.CompletedProcess[str]:
        """Log in as a cluster user with the test key and run a command."""
        config = self.ssh_config_for(user)
        return self.run_command("ssh", "-F", str(config), "-o", "BatchMode=yes", machine, command, agent=agent)

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
            systemctl list-units --type=service --state=running --no-legend --no-pager --plain | awk '{print $1}' | sort
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
    and no terminal, a removed user's login taken away, and a UID conflict that leaves that machine out after its dry run. Run
    when accounts, SSH, sudo, preflight, deploy.py, or the certificates change, and before the release. Real
    Lima VMs."""

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

        with self.subTest("removing a user from cluster.yml takes away their login"):
            self.assertEqual(self.as_user("bob", "front", "true", agent=False).returncode, 0)
            without_bob = yaml.safe_load(cluster.read_text())
            without_bob["users"] = [user for user in without_bob["users"] if user["name"] != "bob"]
            cluster.write_text(yaml.safe_dump(without_bob))
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertNotEqual(self.as_user("bob", "front", "true", agent=False).returncode, 0)
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

        with self.subTest("root logs in only from the front node, with the front node's key"):
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
            self.assertEqual(process.returncode, 4, output[-4000:])
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
            self.assertEqual(self.ssh("cpu1", "sudo test -e /etc/ssh/authorized_keys/root").returncode, 0)
            cluster.write_text(yaml.safe_dump(off, sort_keys=False))
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, self.failure(result))
            self.assertEqual(self.ssh("cpu1", "sudo test -e /etc/ssh/authorized_keys/root").returncode, 1)
            self.assertNotIn("root", self.ssh("cpu1", "sudo sshd -T | grep -i ^allowusers").stdout)
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
            self.assertIn("alice:x:3005:3005:", self.ssh("gpu4", "getent passwd alice").stdout)
            applied = self.run_command(*command, "--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertIn("alice:x:2000:2000:", self.ssh("gpu4", "getent passwd alice").stdout)
            self.assertEqual(
                self.ssh("gpu4", "stat -c %u:%g /tmp/alice-file /home/alice").stdout.split(), ["2000:2000", "2000:2000"]
            )


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimHomeOnStorageTest(SimUsersBase):
    """/home served by the storage machine instead of the front node. Real Lima VMs."""

    def setUp(self) -> None:
        self.sim = SIM / "home-on-storage.yml"
        self.state = ROOT / ".nanohpc-sim" / "home-on-storage"
        super().setUp()

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
