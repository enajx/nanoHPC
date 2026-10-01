"""Tests for the simulated test cluster (`nanohpc sim up/down`).

The unit tests need no VMs. `SimClusterTest` starts real Lima VMs and runs only when NANOHPC_SIM=1 is set,
because it takes minutes and several GB of memory.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
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

    def up_with_test_key(self) -> tuple[Path, dict[str, Any], str]:
        """Bring the sim cluster up and give its users the test key. Return the cluster file, its config,
        and the test public key. The VMs are removed when the test ends."""
        # Registered first, so VMs are removed even when `sim up` itself fails.
        self.addCleanup(self.run_command, "uv", "run", "nanohpc", "sim", "down", str(self.sim))
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
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr)
        return cluster, config, public_key

    def finished_job(self, job: str) -> tuple[str, str]:
        """Return the state and node of a job, once accounting has recorded it."""
        command = (
            f"for i in $(seq 20); do sacct -X -n -P -j {job} -o State | grep -q COMPLETED && break; sleep 1; done;"
            f" sacct -X -n -P -j {job} -o State,NodeList"
        )
        state, node = self.on_front(command).strip().split("|")
        return state, node

    def assert_no_changes(self, result: subprocess.CompletedProcess[str], machines: int) -> None:
        """Require a successful deploy that changed nothing on any machine."""
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)
        recap = [line for line in result.stdout.splitlines() if " : ok=" in line]
        self.assertEqual(len(recap), machines, result.stdout[-4000:])
        for line in recap:
            self.assertIn("changed=0 ", line)


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
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpu4: /dev/vdb has no filesystem. nanoHPC never formats a disk.", result.stdout)
        self.assertIn("mkfs.ext4 /dev/vdb", result.stdout)
        self.assertEqual(self.ssh("gpu4", "sudo blkid /dev/vdb").returncode, 2)
        self.assertEqual(self.ssh("gpu4", "sudo mkfs.ext4 -q /dev/vdb").returncode, 0)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr)
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
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assert_no_changes(result, len(config["machines"]))
        result = self.deploy(self.ssh_config_for("alice"), agent=True)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimDeployTest(SimUsersBase):
    """End to end: `nanohpc sim deploy` sets up Slurm, users, SSH access, sudo, and Munge on the everyday
    cluster. Real Lima VMs. The first run builds Slurm on the front VM (tens of minutes); later runs use the
    cached packages. The users get a key generated for the test, so real logins can be tried."""

    def test_deploy(self) -> None:
        cluster, config, public_key = self.up_and_deploy()
        machines = config["machines"]

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
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)
            recap = [line for line in result.stdout.splitlines() if " : ok=" in line]
            self.assertEqual(len(recap), len(machines), result.stdout[-4000:])
            for line in recap:
                self.assertIn("changed=0 ", line)

        with self.subTest("a drained node is a warning in the health report, not a failed deploy"):
            self.on_front("sudo scontrol update nodename=cpu1 state=drain reason=maintenance-test")
            result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)
            self.assertIn(
                "WARN  no Slurm node is down, drained, not responding, or in maintenance (cpu1", result.stdout
            )
            self.on_front("sudo scontrol update nodename=cpu1 state=resume")

        with self.subTest("a later deploy by an administrator through the forwarded key, without a password"):
            result = self.deploy(self.ssh_config_for("alice"), agent=True)
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)
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
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)
            self.assertNotEqual(self.as_user("bob", "front", "true", agent=False).returncode, 0)
            self.assertEqual(self.ssh("front", "test -e /etc/ssh/authorized_keys/bob").returncode, 1)

        with self.subTest("a UID conflict stops that machine only"):
            self.assertEqual(self.ssh("gpu2", "sudo useradd -u 3005 carol").returncode, 0)
            changed = yaml.safe_load(cluster.read_text())
            changed["users"] += [
                {"name": "carol", "uid": 2005, "ssh_keys": [public_key]},
                {"name": "dave", "uid": 2006, "ssh_keys": [public_key]},
            ]
            cluster.write_text(yaml.safe_dump(changed))
            result = self.deploy(self.state / "ssh_config", agent=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "gpu2: user carol has UID 3005 and primary group ID 3005, but cluster.yml says 2005", result.stdout
            )
            self.assertIn("carol:x:3005:", self.ssh("gpu2", "getent passwd carol").stdout)
            # gpu2 was left unchanged: the other new user was not created there, but was elsewhere.
            self.assertEqual(self.ssh("gpu2", "getent passwd dave").returncode, 2)
            self.assertIn("dave:x:2006:", self.ssh("gpu4", "getent passwd dave").stdout)


@unittest.skipUnless(os.environ.get("NANOHPC_SIM") == "1", "starts real Lima VMs: set NANOHPC_SIM=1 to run")
class SimHomeOnStorageTest(SimUsersBase):
    """/home served by the storage machine instead of the front node. Real Lima VMs."""

    def setUp(self) -> None:
        self.sim = SIM / "home-on-storage.yml"
        self.state = ROOT / ".nanohpc-sim" / "home-on-storage"
        super().setUp()

    def test_home_on_storage_machine(self) -> None:
        # A machine whose local /home holds data stops before the shared /home could hide it.
        self.addCleanup(self.run_command, "uv", "run", "nanohpc", "sim", "down", str(self.sim))
        result = self.run_command("uv", "run", "nanohpc", "sim", "up", str(self.sim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ssh("gpu2", "sudo mkdir /home/olddata").returncode, 0)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpu2: /home holds olddata", result.stdout)
        self.assertNotIn("nfs", self.ssh("gpu2", "findmnt -n -o FSTYPE --target /home").stdout)
        self.assertEqual(self.ssh("gpu2", "sudo rmdir /home/olddata").returncode, 0)
        result = self.run_command("uv", "run", "nanohpc", "sim", "deploy", str(self.sim))
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr)
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


if __name__ == "__main__":
    unittest.main()
