"""Tests for `nanohpc check`: a read-only report on every machine of a cluster against cluster.yml.

These tests use fakes (see test_probe.py): a fake `ssh` runs the remote command locally against a fake machine
folder per machine name, with fake tools first on PATH: sudo (logs each command it runs as root), cluster-health,
getent, findmnt, nvidia-smi, sinfo, and timeout (macOS has none; the fake runs the command without a time limit).
The only seam is in the fake ssh: it rewrites the paths /etc/nanohpc/version and "/dev" in the remote command to the
machine's folder. The real check on machines is the simulated cluster test (tests/test_sim.py, SimDeployTest).
"""

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

from tests.test_probe import FAKE_GETENT, write_command  # pyrefly: ignore[missing-import]

ROOT = Path(__file__).resolve().parents[1]
VERSION = metadata.version("nanohpc")
LOGIN = pwd.getpwuid(os.getuid()).pw_name  # the fake machines run as this test's user

FAKE_SSH = f"""#!{sys.executable}
# Fake ssh (test only): runs the remote command locally in the folder of the machine named by the target, with that
# machine's fake tools first on PATH. A machine folder holding a file named "unreachable" fails like a refused
# connection.
import os, subprocess, sys
args = sys.argv[1:]
index = 0
while args[index].startswith("-"):
    index += 2 if args[index] in ("-F", "-o") else 1
target, command = args[index], args[index + 1]
root = os.path.join(os.environ["FAKE_CLUSTER"], target)
with open(os.path.join(os.environ["FAKE_CLUSTER"], "ssh.log"), "a") as log:
    log.write(" ".join(args[:index]) + " | " + target + "\\n")
if os.path.exists(os.path.join(root, "unreachable")):
    sys.stderr.write(f"ssh: connect to host {{target}} port 22: Connection refused\\n")
    sys.exit(255)
command = command.replace("/etc/nanohpc/version", root + "/etc/nanohpc/version").replace("/etc/nanohpc/monitor-gpus.json", root + "/etc/nanohpc/monitor-gpus.json").replace("/var/lib/nanohpc/monitor/status.json", root + "/var/lib/nanohpc/monitor/status.json").replace('"/dev"', f'"{{root}}/dev"')
environment = dict(os.environ, FAKE_ROOT=root, PATH=os.path.join(root, "bin") + ":" + os.environ["PATH"])
sys.exit(subprocess.run(["sh", "-c", command], env=environment).returncode)
"""

FAKE_SUDO = f"""#!{sys.executable}
# Fake sudo (test only): logs the command it runs as root to the cluster's sudo.log ("machine: command"), then runs
# it. With a sudo_password file in the machine folder, it fails like sudo asking for a password with no terminal.
import os, sys
root = os.environ["FAKE_ROOT"]
args = sys.argv[1:]
if os.path.exists(os.path.join(root, "sudo_password")):
    sys.stderr.write("sudo: a password is required\\n")
    sys.exit(1)
assert args[:3] == ["-S", "-p", ""], args
with open(os.path.join(os.path.dirname(root), "sudo.log"), "a") as log:
    log.write(os.path.basename(root) + ": " + " ".join(os.path.basename(arg) for arg in args[3:]) + "\\n")
os.execvp(args[3], args[3:])
"""

COMMANDS = {
    "timeout": 'shift\nexec "$@"\n',
    "getent": FAKE_GETENT.removeprefix("#!/bin/sh\n"),
    # findmnt -r -n -o TARGET,SOURCE,FSTYPE: the fake mount list.
    "findmnt": 'cat "$FAKE_ROOT/mounts"\n',
}
HEALTH = 'cat "$FAKE_ROOT/health.txt"\nexit "$(cat "$FAKE_ROOT/health.code")"\n'
HEALTHY_REPORT = "ok    nanoHPC roles are recorded\nok    service munge\n"
SINFO = "cpu1 idle\ngpu2 mixed\ngpu4 idle\ngpu4 idle\ngpu4i idle\n"
HOME_CLIENT = "/home 192.168.104.10:/home nfs4\n"


class CheckTest(unittest.TestCase):
    """`nanohpc check` on the example cluster (examples/cluster.yml), with one fake machine per machine name."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.cluster = self.folder / "cluster"
        self.cluster.mkdir()
        shutil.copy(ROOT / "examples" / "cluster.yml", self.folder / "cluster.yml")
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        write_command(bin_folder, "ssh", FAKE_SSH)
        self.environment = {
            **os.environ,
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "FAKE_CLUSTER": str(self.cluster),
        }
        scratch = "/scratch /dev/loop0 ext4\n"
        self.machine("front", "/home /dev/vdb ext4\n")
        self.machine("gpu4", HOME_CLIENT + "/scratch /dev/vdb ext4\n")
        self.machine("gpu2", HOME_CLIENT + scratch)
        self.machine("cpu1", HOME_CLIENT + scratch)
        self.machine("gpu4i", HOME_CLIENT + scratch)
        self.machine("store", "/srv /dev/vda1 ext4\n")
        # Real GPUs on gpu4 and gpu4i; gpu2 has the simulated cluster's placeholder devices and no nvidia-smi.
        self.gpus("gpu4", 4)
        self.gpus("gpu4i", 4)
        for index in range(2):
            (self.cluster / "gpu2" / "dev" / f"nvidia{index}").touch()
        (self.cluster / "gpu2" / "dev" / "nvidiactl").touch()
        (self.cluster / "front" / "sinfo").write_text(SINFO)
        write_command(self.cluster / "front" / "bin", "sinfo", 'cat "$FAKE_ROOT/sinfo"\n')

    def machine(self, name: str, mounts: str) -> None:
        """Make a deployed, healthy fake machine: alice and bob with their UIDs, the mounts given, sudo working."""
        root = self.cluster / name
        (root / "bin").mkdir(parents=True)
        (root / "dev").mkdir()
        (root / "etc" / "nanohpc").mkdir(parents=True)
        write_command(root / "bin", "sudo", FAKE_SUDO)
        write_command(root / "bin", "cluster-health", HEALTH)
        (root / "bin" / "python3").symlink_to(sys.executable)
        for command, text in COMMANDS.items():
            write_command(root / "bin", command, text)
        (root / "etc" / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\nubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
            "alice:x:2000:2000::/home/alice:/bin/bash\nbob:x:2001:2001::/home/bob:/bin/bash\n"
        )
        (root / "etc" / "group").write_text("root:x:0:\nubuntu:x:1000:\nalice:x:2000:\nbob:x:2001:\n")
        (root / "etc" / "nanohpc" / "version").write_text(f"{VERSION}\n")
        # A short line (a field left empty) is read without failing.
        (root / "mounts").write_text("/ /dev/vda1 ext4\n/sys/fs/bpf bpf\n" + mounts)
        (root / "health.txt").write_text(HEALTHY_REPORT)
        (root / "health.code").write_text("0\n")

    def gpus(self, name: str, count: int) -> None:
        """Give a machine nvidia-smi with `count` GPUs."""
        root = self.cluster / name
        (root / "gpus").write_text("NVIDIA RTX A6000\n" * count)
        write_command(root / "bin", "nvidia-smi", 'cat "$FAKE_ROOT/gpus"\n')

    def check(self) -> subprocess.CompletedProcess[str]:
        """Run `nanohpc check` through its command line, against the fake machines."""
        return subprocess.run(
            [sys.executable, "-c", "from nanohpc.cli import main; main()", "check", str(self.folder / "cluster.yml")],
            capture_output=True, text=True, check=False, env=self.environment, cwd=self.folder,
        )  # fmt: skip

    def rows(self, output: str) -> list[list[str]]:
        """Return the table rows (header first), split on runs of two or more spaces."""
        lines = output.split("\n\n")[0].splitlines()
        return [[cell for cell in line.split("  ") if cell.strip()] for line in lines]

    def row(self, output: str, machine: str) -> list[str]:
        """Return the table row of one machine, cells stripped."""
        return next([cell.strip() for cell in row] for row in self.rows(output) if row[0].strip() == machine)

    def assert_problem_first(self, result: subprocess.CompletedProcess[str], machine: str, problem: str) -> None:
        """Require exit code 1, `machine` in the table's first row, and `problem` in the problem list."""
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.rows(result.stdout)[1][0].strip(), machine, result.stdout)
        problems = result.stdout.partition("Problems")[2]
        self.assertIn(f"  {machine}: {problem}", problems, result.stdout)

    def test_healthy_cluster(self) -> None:
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        header = [cell.strip() for cell in self.rows(result.stdout)[0]]
        self.assertEqual(header, ["machine", "ssh", "health", "users", "mounts", "gpus", "slurm", "version"])
        self.assertEqual(
            [row[0].strip() for row in self.rows(result.stdout)[1:]],
            ["front", "gpu4", "gpu2", "cpu1", "gpu4i", "store"],
        )
        self.assertEqual(self.row(result.stdout, "front"), ["front", "ok", "ok (sudo)", "ok", "ok", "-", "-", VERSION])
        self.assertEqual(
            self.row(result.stdout, "gpu4"), ["gpu4", "ok", "ok (sudo)", "ok", "ok", "4/4", "idle", VERSION]
        )
        self.assertEqual(self.row(result.stdout, "gpu2")[5:7], ["2/2 (devices)", "mixed"])
        self.assertEqual(self.row(result.stdout, "cpu1")[5], "0/0")
        self.assertEqual(self.row(result.stdout, "store")[4:7], ["-", "-", "-"])
        self.assertIn("No problems.", result.stdout)
        self.assertNotIn("Problems", result.stdout)
        self.assertIn("gpu2: no nvidia-smi: counted the placeholder GPU devices /dev/nvidiaN", result.stdout)
        # Read-only: sudo ran only its own check and cluster-health; ssh never asks, and forwards the agent.
        sudo = sorted(set((self.cluster / "sudo.log").read_text().splitlines()))
        machines = ["cpu1", "front", "gpu2", "gpu4", "gpu4i", "store"]
        self.assertEqual(sudo, sorted([f"{m}: true" for m in machines] + [f"{m}: cluster-health" for m in machines]))
        for line in (self.cluster / "ssh.log").read_text().splitlines():
            self.assertIn("-A", line.split())
            self.assertIn("BatchMode=yes", line)

    def test_unreachable_machine(self) -> None:
        (self.cluster / "gpu2" / "unreachable").touch()
        result = self.check()
        self.assert_problem_first(result, "gpu2", "SSH failed: `ssh gpu2`: ssh: connect to host gpu2 port 22")
        self.assertEqual(self.row(result.stdout, "gpu2"), ["gpu2", "FAIL", "?", "?", "?", "?", "?", "?"])
        self.assertIn("Problems (1):", result.stdout)

    def test_wrong_uid(self) -> None:
        passwd = self.cluster / "cpu1" / "etc" / "passwd"
        passwd.write_text(passwd.read_text().replace("alice:x:2000:2000:", "alice:x:1001:1001:"))
        group = self.cluster / "cpu1" / "etc" / "group"
        group.write_text(group.read_text().replace("alice:x:2000:", "alice:x:1001:"))
        result = self.check()
        self.assert_problem_first(
            result, "cpu1", "alice has UID 1001 and primary group ID 1001 on cpu1, but cluster.yml says 2000 for both"
        )
        self.assertIn("  cpu1: the group alice has group ID 1001 on cpu1", result.stdout)
        self.assertEqual(self.row(result.stdout, "cpu1")[3], "FAIL")

    def test_missing_user(self) -> None:
        passwd = self.cluster / "gpu4i" / "etc" / "passwd"
        passwd.write_text(passwd.read_text().replace("bob:x:2001:2001::/home/bob:/bin/bash\n", ""))
        self.assert_problem_first(self.check(), "gpu4i", "bob has no account")

    def test_missing_mounts(self) -> None:
        (self.cluster / "gpu4" / "mounts").write_text("/ /dev/vda1 ext4\n" + HOME_CLIENT)
        (self.cluster / "cpu1" / "mounts").write_text("/ /dev/vda1 ext4\n/scratch /dev/loop0 ext4\n")
        result = self.check()
        self.assert_problem_first(result, "gpu4", "/scratch is not mounted")
        self.assertIn("  cpu1: /home is not mounted (cluster.yml: the NFS mount 192.168.104.10:/home)", result.stdout)
        self.assertEqual(self.row(result.stdout, "gpu4")[4], "FAIL")

    def test_home_server_with_nfs_home(self) -> None:
        (self.cluster / "front" / "mounts").write_text("/ /dev/vda1 ext4\n/home 192.168.104.20:/home nfs4\n")
        self.assert_problem_first(
            self.check(),
            "front",
            "/home is the NFS mount 192.168.104.20:/home, but cluster.yml says this machine serves /home",
        )

    def test_gpu_count_differs(self) -> None:
        self.gpus("gpu4i", 3)
        (self.cluster / "gpu2" / "dev" / "nvidia1").unlink()
        result = self.check()
        self.assert_problem_first(
            result,
            "gpu2",
            "no nvidia-smi (NVIDIA driver not installed?), 1 /dev/nvidiaN devices; cluster.yml says 2 GPUs",
        )
        self.assertIn("  gpu4i: nvidia-smi sees 3 GPUs, cluster.yml says 4", result.stdout)
        self.assertEqual(self.row(result.stdout, "gpu4i")[5], "3/4")

    def test_cluster_health_fail_lines(self) -> None:
        (self.cluster / "cpu1" / "health.txt").write_text(
            "ok    nanoHPC roles are recorded\nFAIL  service munge\nFAIL  service slurmd\nWARN  disk /: 95% space\n"
        )
        (self.cluster / "cpu1" / "health.code").write_text("1\n")
        result = self.check()
        self.assert_problem_first(result, "cpu1", "cluster-health: FAIL service munge")
        self.assertIn("  cpu1: cluster-health: FAIL service slurmd", result.stdout)
        self.assertIn("Problems (2):", result.stdout)
        # A WARN line is a warning, not a problem.
        self.assertIn("Warnings (1):\n  cpu1: cluster-health: WARN disk /: 95% space", result.stdout)
        self.assertEqual(self.row(result.stdout, "cpu1")[2], "FAIL (sudo)")

    def test_warnings_alone_exit_zero(self) -> None:
        (self.cluster / "gpu4" / "health.txt").write_text("WARN  disk /scratch: 93% space\n")
        (self.cluster / "front" / "sinfo").write_text(SINFO.replace("cpu1 idle", "cpu1 drained"))
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("  gpu4: cluster-health: WARN disk /scratch: 93% space", result.stdout)
        self.assertIn("  cpu1: the Slurm node is drained", result.stdout)
        self.assertEqual(self.row(result.stdout, "gpu4")[2], "WARN (sudo)")
        self.assertEqual(self.row(result.stdout, "cpu1")[6], "drained")

    def test_slurm_node_missing(self) -> None:
        (self.cluster / "front" / "sinfo").write_text(SINFO.replace("gpu4i idle\n", ""))
        result = self.check()
        self.assert_problem_first(result, "gpu4i", "not a Slurm node (sinfo on front does not list it)")
        self.assertEqual(self.row(result.stdout, "gpu4i")[6], "missing")

    def test_sinfo_fails(self) -> None:
        write_command(
            self.cluster / "front" / "bin",
            "sinfo",
            'echo "slurm_load_node: Unable to contact slurm controller" >&2\nexit 1\n',
        )
        result = self.check()
        self.assert_problem_first(result, "front", "sinfo failed: slurm_load_node: Unable to contact slurm controller")
        self.assertEqual(self.row(result.stdout, "gpu4")[6], "?")

    def test_not_deployed_yet(self) -> None:
        (self.cluster / "store" / "bin" / "cluster-health").unlink()
        (self.cluster / "store" / "etc" / "nanohpc" / "version").unlink()
        result = self.check()
        self.assert_problem_first(result, "store", "not deployed yet (cluster-health is not installed)")
        self.assertEqual(self.row(result.stdout, "store"), ["store", "ok", "not deployed", "?", "?", "?", "?", "?"])
        self.assertIn("Problems (1):", result.stdout)

    def test_without_sudo_cluster_health_runs_as_the_login_user(self) -> None:
        (self.cluster / "gpu4" / "sudo_password").touch()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.row(result.stdout, "gpu4")[2], f"ok (as {LOGIN})")
        self.assertIn(
            f"gpu4: sudo does not work without a password, so cluster-health ran as {LOGIN}: some checks may differ",
            result.stdout,
        )
        self.assertNotIn("gpu4: cluster-health", (self.cluster / "sudo.log").read_text())

    def test_no_gpus_and_nvidia_smi_finds_none(self) -> None:
        # nvidia-smi exits non-zero when it finds no GPU; that matches cluster.yml on a CPU-only machine.
        write_command(self.cluster / "cpu1" / "bin", "nvidia-smi", "echo 'No devices were found'\nexit 6\n")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.row(result.stdout, "cpu1")[5], "0/0")

    def test_unknown_version(self) -> None:
        (self.cluster / "gpu2" / "etc" / "nanohpc" / "version").unlink()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.row(result.stdout, "gpu2")[7], "unknown")

    def test_invalid_cluster_file(self) -> None:
        (self.folder / "cluster.yml").write_text("machines: {}\n")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("error(s)", result.stderr)
        self.assertFalse((self.cluster / "ssh.log").exists())


class MonitorCheckTest(unittest.TestCase):
    """The monitor check uses SSH but never asks about users, mounts, or Slurm."""

    check = CheckTest.check
    machine = CheckTest.machine
    gpus = CheckTest.gpus
    rows = CheckTest.rows
    row = CheckTest.row
    assert_problem_first = CheckTest.assert_problem_first

    def setUp(self) -> None:
        CheckTest.setUp(self)
        self.environment["PYTHONPATH"] = str(ROOT / "src")
        (self.folder / "cluster.yml").write_text(
            "cluster:\n  name: lab\n  mode: monitor\n  monitor_host: front\n"
            "  website:\n    hostname: lab.example.org\n    https: letsencrypt\n    path: /cluster/\n"
            "machines:\n  front:\n    address: 192.168.1.10\n  gpu4:\n    address: 192.168.1.11\n"
            "users: [alice]\n"
        )
        for name in ("front", "gpu4"):
            root = self.cluster / name
            (root / "etc" / "nanohpc" / "monitor-gpus.json").write_text(
                '{"count": 0, "models": [], "fake": false}'
                if name == "front"
                else '{"count": 4, "models": ["NVIDIA RTX A6000", "NVIDIA RTX A6000", "NVIDIA RTX A6000", "NVIDIA RTX A6000"], "fake": false}'
            )
            write_command(root / "bin", "systemctl", "echo active\n")
        (self.cluster / "front" / "var" / "lib" / "nanohpc" / "monitor").mkdir(parents=True)
        (self.cluster / "front" / "var" / "lib" / "nanohpc" / "monitor" / "status.json").write_text(
            '{"mode":"monitor","generated_at":"2099-01-01T00:00:00Z","refresh_seconds":30,"nodes":[]}'
        )

    def test_healthy_cluster(self) -> None:
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            [cell.strip() for cell in self.rows(result.stdout)[0]],
            ["machine", "ssh", "health", "gpus", "monitoring", "version"],
        )
        self.assertEqual(self.row(result.stdout, "gpu4")[3], "4/4")
        self.assertEqual(self.row(result.stdout, "front")[3], "0/0")
        self.assertIn("No problems.", result.stdout)
        self.assertNotIn("slurm", result.stdout.lower())
        self.assertNotIn("/home", result.stdout)
        self.assertNotIn("users", result.stdout.lower())
        sudo = (self.cluster / "sudo.log").read_text()
        self.assertNotIn("getent", sudo)
        self.assertNotIn("findmnt", sudo)

    def test_gpu_inventory_change(self) -> None:
        (self.cluster / "gpu4" / "gpus").write_text("NVIDIA RTX A6000\n" * 3)
        result = self.check()
        self.assert_problem_first(result, "gpu4", "GPU inventory changed: found 3 GPUs, recorded 4")

    def test_monitor_service_and_snapshot_failure(self) -> None:
        write_command(self.cluster / "front" / "bin", "systemctl", "echo inactive\nexit 3\n")
        result = self.check()
        self.assert_problem_first(result, "front", "monitoring service nanohpc-node-exporter.service is inactive")

    def test_stale_snapshot(self) -> None:
        (self.cluster / "front" / "var" / "lib" / "nanohpc" / "monitor" / "status.json").write_text(
            '{"mode":"monitor","generated_at":"2020-01-01T00:00:00Z","refresh_seconds":30,"nodes":[]}'
        )
        self.assert_problem_first(self.check(), "front", "monitoring snapshot is stale")

    def test_changed_gpu_model(self) -> None:
        (self.cluster / "gpu4" / "gpus").write_text("Other GPU\n" * 4)
        self.assert_problem_first(self.check(), "gpu4", "GPU inventory changed: models differ")
