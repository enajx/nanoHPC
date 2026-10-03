"""Tests for what the setup wizard learns about a machine over SSH (nanohpc.probe).

These tests use fakes: a fake `ssh` program put first on PATH runs the remote command locally with `sh -c`, and fake
machine tools (hostname, nproc, lscpu, ip, lsblk, nvidia-smi, sudo, getent, pgrep, who, findmnt, find, usermod,
groupmod, unshare, umount, df) read and write a fake machine folder (FAKE_ROOT). The only seam is in the fake ssh:
it rewrites the paths /etc/os-release, /proc/meminfo, and /var/lib/nanohpc in the remote command to the fake machine
folder. Timeouts are tested by patching the timeout constants down to one second.
The real check on machines is the simulated cluster test (tests/test_sim.py).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nanohpc.config import GPU_TYPE
from nanohpc.probe import Disk, probe_machine, suggest_gpu_type, uid_owner, uid_problems, user_ids

FAKE_SSH = f"""#!{sys.executable}
# Fake ssh (test only): runs the remote command locally. A target named "unreachable" fails like a refused connection,
# "newhost" like an unknown host key, and "slow" never answers.
import os, subprocess, sys, time
args = sys.argv[1:]
index = 0
while args[index].startswith("-"):
    index += 2 if args[index] in ("-F", "-o") else 1
target, command = args[index], args[index + 1]
root = os.environ["FAKE_ROOT"]
with open(os.path.join(root, "ssh.log"), "a") as log:
    log.write(" ".join(args[:index]) + " | " + target + "\\n")
if target == "unreachable":
    sys.stderr.write(f"ssh: connect to host {{target}} port 22: Connection refused\\n")
    sys.exit(255)
if target == "newhost":
    sys.stderr.write("Host key verification failed.\\n")
    sys.exit(255)
if target == "slow":
    time.sleep(30)
for path in ("/etc/os-release", "/proc/meminfo", "/var/lib/nanohpc"):
    command = command.replace(path, root + path)
sys.exit(subprocess.run(["sh", "-c", command]).returncode)
"""

FAKE_SUDO = f"""#!{sys.executable}
# Fake sudo (test only): logs the command it runs as root to sudo.log (one JSON string per line), then runs it.
# With a sudo_password file in the fake machine folder, it fails like sudo asking for a password with no terminal.
import json, os, shlex, sys
root = os.environ["FAKE_ROOT"]
args = sys.argv[1:]
if os.path.exists(os.path.join(root, "sudo_password")):
    sys.stderr.write("sudo: a password is required\\n")
    sys.exit(1)
assert args[:3] == ["-S", "-p", ""], args
with open(os.path.join(root, "sudo.log"), "a") as log:
    log.write(json.dumps(shlex.join(args[3:])) + "\\n")
os.execvp(args[3], args[3:])
"""

FAKE_GETENT = """#!/bin/sh
# Fake getent (test only): looks up names or numbers in the fake machine's passwd or group file, and then in
# passwd.ldap or group.ldap (accounts from a directory) unless called with -s files.
sources=all
if [ "$1" = -s ]; then sources=$2; shift 2; fi
files="$FAKE_ROOT/etc/$1"
if [ "$sources" = all ] && [ -e "$FAKE_ROOT/etc/$1.ldap" ]; then files="$files $FAKE_ROOT/etc/$1.ldap"; fi
shift
status=0
for key in "$@"; do
  line=$(cat $files | awk -F: -v k="$key" '$1 == k || $3 == k { print; exit }')
  if [ -n "$line" ]; then echo "$line"; else status=2; fi
done
exit $status
"""

FAKE_IDMOD = f"""#!{sys.executable}
# Fake usermod -u NEW USER and groupmod -g NEW GROUP (test only): change the fake passwd and group files.
import os, sys
root = os.environ["FAKE_ROOT"]
program = os.path.basename(sys.argv[0])
new, name = sys.argv[2], sys.argv[3]
with open(os.path.join(root, "log"), "a") as log:
    log.write(" ".join([program, *sys.argv[1:]]) + "\\n")
def rewrite(database, change):
    path = os.path.join(root, "etc", database)
    lines = [line.split(":") for line in open(path).read().splitlines()]
    open(path, "w").write("".join(":".join(change(fields)) + "\\n" for fields in lines))
if program == "usermod":
    rewrite("passwd", lambda f: f[:2] + [new] + f[3:] if f[0] == name else f)
else:
    old = [f[2] for f in (line.split(":") for line in open(os.path.join(root, "etc", "group"))) if f[0] == name][0]
    rewrite("group", lambda f: f[:2] + [new] + f[3:] if f[0] == name else f)
    rewrite("passwd", lambda f: f[:3] + [new] + f[4:] if f[3] == old else f)
"""

COMMANDS = {
    "hostname": 'cat "$FAKE_ROOT/hostname"\n',
    "nproc": 'cat "$FAKE_ROOT/nproc"\n',
    # lscpu answers in German unless LC_ALL=C.
    "lscpu": 'if [ "$LC_ALL" = C ]; then cat "$FAKE_ROOT/lscpu"; else echo "Sockel:  9"; fi\n',
    "df": 'cat "$FAKE_ROOT/df"\n',
    "ip": 'cat "$FAKE_ROOT/ip"\n',
    "lsblk": 'cat "$FAKE_ROOT/lsblk.json"\n',
    # pgrep -l -u UID: lines "uid pid name" of the fake process list; exit 1 when none match.
    "pgrep": 'awk -v u="$3" \'$1 == u { print $2, $3; found = 1 } END { exit !found }\' "$FAKE_ROOT/processes"\n',
    "who": 'cat "$FAKE_ROOT/who"\n',
    "findmnt": 'cat "$FAKE_ROOT/mounts"\n',
    # find MOUNT ... -print0 lists the fake owned files under MOUNT (lines "mount<TAB>path"); with -exec it only
    # succeeds (the fake sudo has logged the command).
    "find": (
        'case " $* " in *" -print0 "*)\n'
        "  awk -F '\\t' -v m=\"$1\" '$1 == m { print $2 }' \"$FAKE_ROOT/owned\" | tr '\\n' '\\000' ;;\n"
        "esac\n"
    ),
    "unshare": 'echo "unshare $*" >> "$FAKE_ROOT/log"\nwhile [ "$1" != sh ]; do shift; done\nexec "$@"\n',
    "umount": 'echo "umount $*" >> "$FAKE_ROOT/log"\n',
    "getent": FAKE_GETENT.removeprefix("#!/bin/sh\n"),
}

UBUNTU = 'PRETTY_NAME="Ubuntu 24.04.3 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\n'
DEBIAN = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nID=debian\n'
IP = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
    "2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0\\       valid_lft forever\n"
    "3: tailscale0    inet 100.64.0.7/32 scope global tailscale0\\       valid_lft forever\n"
)
LSCPU = "Architecture:  x86_64\nCPU(s):  32\nThread(s) per core:  2\nCore(s) per socket:  8\nSocket(s):  2\n"
LSBLK = {
    "blockdevices": [
        {"name": "loop0", "path": "/dev/loop0", "size": 4096, "fstype": "squashfs", "mountpoint": "/snap/x",
         "model": None, "type": "loop", "pttype": None},
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 1000204886016, "fstype": None, "mountpoint": None,
         "model": "Samsung SSD 980 PRO 1TB             ", "type": "disk", "pttype": "gpt", "children": [
             {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "size": 1127219200, "fstype": "vfat",
              "mountpoint": "/boot/efi", "model": None, "type": "part", "pttype": "gpt"},
             {"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "size": 999073579008, "fstype": "ext4",
              "mountpoint": "/", "model": None, "type": "part", "pttype": "gpt"}]},
        # An LVM member partition with an unmounted logical volume.
        {"name": "sda", "path": "/dev/sda", "size": 4000787030016, "fstype": None, "mountpoint": None,
         "model": "WDC WD40EFZX", "type": "disk", "pttype": "gpt", "children": [
             {"name": "sda1", "path": "/dev/sda1", "size": 4000785960960, "fstype": "LVM2_member",
              "mountpoint": None, "model": None, "type": "part", "pttype": "gpt", "children": [
                  {"name": "data-lv", "path": "/dev/mapper/data-lv", "size": 4000000000000, "fstype": None,
                   "mountpoint": None, "model": None, "type": "lvm", "pttype": None}]}]},
        # An empty disk, and an empty partition on a disk with a partition table.
        {"name": "sdb", "path": "/dev/sdb", "size": 2000398934016, "fstype": None, "mountpoint": None,
         "model": "WD Red", "type": "disk", "pttype": None},
        {"name": "sdc", "path": "/dev/sdc", "size": 2000398934016, "fstype": None, "mountpoint": None,
         "model": "WD Red", "type": "disk", "pttype": "gpt", "children": [
             {"name": "sdc1", "path": "/dev/sdc1", "size": 2000397795328, "fstype": None, "mountpoint": None,
              "model": None, "type": "part", "pttype": "gpt"}]},
        # Two RAID members: the RAID device is listed once.
        {"name": "sdd", "path": "/dev/sdd", "size": 1000204886016, "fstype": "linux_raid_member", "mountpoint": None,
         "model": "ST1000", "type": "disk", "pttype": None, "children": [
             {"name": "md0", "path": "/dev/md0", "size": 1000069595136, "fstype": None, "mountpoint": None,
              "model": None, "type": "raid1", "pttype": None}]},
        {"name": "sde", "path": "/dev/sde", "size": 1000204886016, "fstype": "linux_raid_member", "mountpoint": None,
         "model": "ST1000", "type": "disk", "pttype": None, "children": [
             {"name": "md0", "path": "/dev/md0", "size": 1000069595136, "fstype": None, "mountpoint": None,
              "model": None, "type": "raid1", "pttype": None}]},
        {"name": "sdf", "path": "/dev/sdf", "size": 16000000000, "fstype": "swap", "mountpoint": "[SWAP]",
         "model": "Swap disk", "type": "disk", "pttype": None},
        {"name": "sr0", "path": "/dev/sr0", "size": 1073741312, "fstype": None, "mountpoint": None,
         "model": "DVD", "type": "rom", "pttype": None},
        {"name": "zram0", "path": "/dev/zram0", "size": 8589934592, "fstype": "swap", "mountpoint": "[SWAP]",
         "model": None, "type": "disk", "pttype": None},
    ]
}  # fmt: skip


def write_command(folder: Path, name: str, text: str) -> None:
    """Write an executable fake command (a shell script unless `text` starts with its own #! line)."""
    path = folder / name
    path.write_text(text if text.startswith("#!") else "#!/bin/sh\n" + text)
    path.chmod(0o755)


def fake_machine(folder: Path, gpus: list[str], os_release: str) -> dict[str, str]:
    """Make a fake machine in `folder` (Ubuntu or not, with these nvidia-smi GPU names, none: no nvidia-smi) and
    return the environment that reaches it through the fake ssh. Tests change its files to vary the machine."""
    bin_folder = folder / "bin"
    root = folder / "root"
    bin_folder.mkdir()
    (root / "etc").mkdir(parents=True)
    (root / "proc").mkdir()
    write_command(bin_folder, "ssh", FAKE_SSH)
    write_command(bin_folder, "sudo", FAKE_SUDO)
    write_command(bin_folder, "usermod", FAKE_IDMOD)
    write_command(bin_folder, "groupmod", FAKE_IDMOD)
    for name, text in COMMANDS.items():
        write_command(bin_folder, name, text)
    (bin_folder / "python3").symlink_to(sys.executable)
    if gpus:
        (root / "gpus").write_text("".join(f"{gpu}\n" for gpu in gpus))
        write_command(bin_folder, "nvidia-smi", 'cat "$FAKE_ROOT/gpus"\n')
    (root / "hostname").write_text("node7\n")
    (root / "nproc").write_text("32\n")
    (root / "lscpu").write_text(LSCPU)
    (root / "df").write_text("       Avail\n123456789012\n")
    (root / "ip").write_text(IP)
    (root / "lsblk.json").write_text(json.dumps(LSBLK))
    (root / "etc/os-release").write_text(os_release)
    (root / "proc/meminfo").write_text("MemTotal:       65843724 kB\nMemFree:         1000 kB\n")
    (root / "var/lib/nanohpc").mkdir(parents=True)
    (root / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "ubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
        "alice:x:1001:1001::/home/alice:/bin/bash\n"
        "carol:x:2002:2002::/home/carol:/bin/bash\n"
    )
    (root / "etc/group").write_text("root:x:0:\nubuntu:x:1000:\nalice:x:1001:\ncarol:x:2002:\n")
    (root / "processes").write_text("0 1 systemd\n1000 900 bash\n")
    (root / "who").write_text("ubuntu   pts/0        2026-10-03 09:00 (192.168.1.2)\n")
    (root / "mounts").write_text(
        "/ ext4 /dev/nvme0n1p2\n/proc proc proc\n/run tmpfs tmpfs\n/boot/efi vfat /dev/nvme0n1p1\n"
    )
    (root / "owned").write_text("/\t/home/alice\n/\t/home/alice/notes.txt\n/\t/var/spool/cron/crontabs/alice\n")
    return {**os.environ, "PATH": f"{bin_folder}:{os.environ['PATH']}", "FAKE_ROOT": str(root)}


class ProbeMachineTest(unittest.TestCase):
    """probe_machine reads a machine over one SSH call (fake ssh and fake tools)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_gpu_machine(self) -> None:
        environment = fake_machine(self.folder, ["NVIDIA RTX A6000", "NVIDIA RTX A6000"], UBUNTU)
        with mock.patch.dict(os.environ, {**environment, "LC_ALL": "de_DE.UTF-8"}):
            facts = probe_machine("node7.lab", self.folder / "ssh_config")
        self.assertIsNone(facts.error)
        self.assertEqual(facts.target, "node7.lab")
        self.assertEqual(facts.hostname, "node7")
        self.assertEqual(facts.addresses, ["192.168.1.20", "100.64.0.7"])
        self.assertEqual(facts.cpus, 32)
        self.assertEqual((facts.sockets, facts.cores_per_socket, facts.threads_per_core), (2, 8, 2))
        self.assertEqual(facts.memory_mb, 65843724 // 1024)
        self.assertEqual(facts.gpus, ["NVIDIA RTX A6000", "NVIDIA RTX A6000"])
        nvme = ["/dev/nvme0n1p1", "/dev/nvme0n1p2"]
        self.assertEqual(
            facts.disks,
            [
                Disk("/dev/nvme0n1", 1000.2, None, None, "Samsung SSD 980 PRO 1TB", "disk", "gpt", nvme, True),
                Disk("/dev/nvme0n1p1", 1.1, "vfat", "/boot/efi", None, "part", "gpt", [], True),
                Disk("/dev/nvme0n1p2", 999.1, "ext4", "/", None, "part", "gpt", [], True),
                Disk("/dev/sda", 4000.8, None, None, "WDC WD40EFZX", "disk", "gpt", ["/dev/sda1"], True),
                Disk("/dev/sda1", 4000.8, "LVM2_member", None, None, "part", "gpt", ["/dev/mapper/data-lv"], True),
                Disk("/dev/mapper/data-lv", 4000.0, None, None, None, "lvm", None, [], False),
                Disk("/dev/sdb", 2000.4, None, None, "WD Red", "disk", None, [], False),
                Disk("/dev/sdc", 2000.4, None, None, "WD Red", "disk", "gpt", ["/dev/sdc1"], False),
                Disk("/dev/sdc1", 2000.4, None, None, None, "part", "gpt", [], False),
                Disk("/dev/sdd", 1000.2, "linux_raid_member", None, "ST1000", "disk", None, ["/dev/md0"], True),
                Disk("/dev/md0", 1000.1, None, None, None, "raid1", None, [], False),
                Disk("/dev/sde", 1000.2, "linux_raid_member", None, "ST1000", "disk", None, ["/dev/md0"], True),
                Disk("/dev/sdf", 16.0, "swap", "[SWAP]", "Swap disk", "disk", None, [], True),
            ],
        )
        self.assertEqual(facts.free_gb, 123.5)
        self.assertEqual((facts.os_id, facts.ubuntu, facts.supported), ("ubuntu", "24.04", True))
        self.assertTrue(facts.sudo_ok)
        self.assertEqual(facts.notes, [])
        # One SSH call, with the SSH config and deploy's options, without forwarding the SSH agent.
        log = (self.folder / "root/ssh.log").read_text().splitlines()
        self.assertEqual(log, [f"-F {self.folder / 'ssh_config'} -o BatchMode=yes -o ConnectTimeout=15 | node7.lab"])

    def test_cpu_only_machine(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU)
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("cpu1", None)
        self.assertIsNone(facts.error)
        self.assertEqual(facts.gpus, [])
        self.assertEqual(facts.notes, [])

    def test_broken_nvidia_smi_is_noted(self) -> None:
        environment = fake_machine(self.folder, ["x"], UBUNTU)
        write_command(
            self.folder / "bin", "nvidia-smi", "echo 'NVIDIA-SMI has failed because it could not communicate'\nexit 9\n"
        )
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("gpu1", None)
        self.assertEqual(facts.gpus, [])
        self.assertEqual(
            facts.notes, ["nvidia-smi is installed but failed: NVIDIA-SMI has failed because it could not communicate"]
        )

    def test_non_ubuntu_machine(self) -> None:
        environment = fake_machine(self.folder, [], DEBIAN)
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("old", None)
        self.assertIsNone(facts.error)
        self.assertEqual((facts.os_id, facts.ubuntu, facts.supported), ("debian", "", False))

    def test_unsupported_ubuntu(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU.replace("24.04", "20.04"))
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("old", None)
        self.assertEqual((facts.os_id, facts.ubuntu, facts.supported), ("ubuntu", "20.04", False))

    def test_ssh_failure(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU)
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("unreachable", None)
        self.assertEqual(
            facts.error, "SSH failed: `ssh unreachable`: ssh: connect to host unreachable port 22: Connection refused"
        )
        self.assertEqual((facts.hostname, facts.cpus, facts.disks, facts.sudo_ok), ("", 0, [], False))

    def test_unknown_host_key(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU)
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("newhost", None)
        self.assertEqual(
            facts.error,
            "SSH failed: `ssh newhost`: Host key verification failed. Run `ssh newhost` once in a terminal to check "
            "and accept the machine's host key.",
        )

    def test_timeout_is_an_error(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU)
        with mock.patch.dict(os.environ, environment), mock.patch("nanohpc.probe.PROBE_TIMEOUT", 1):
            facts = probe_machine("slow", None)
        self.assertEqual(facts.error, "SSH failed: `ssh slow`: no answer within 1 seconds")

    def test_failing_tool_is_an_error(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU)
        write_command(self.folder / "bin", "lsblk", "echo 'lsblk: broken' >&2\nexit 1\n")
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("node7", None)
        self.assertIsNotNone(facts.error)
        self.assertTrue(str(facts.error).startswith("the probe failed on node7: "), facts.error)
        self.assertIn("lsblk", str(facts.error))

    def test_sudo_needs_password(self) -> None:
        environment = fake_machine(self.folder, [], UBUNTU)
        (self.folder / "root/sudo_password").write_text("")
        with mock.patch.dict(os.environ, environment):
            facts = probe_machine("node7", None)
        self.assertIsNone(facts.error)
        self.assertFalse(facts.sudo_ok)
        self.assertEqual(facts.notes, ["sudo asks for a password"])


class UserIdsTest(unittest.TestCase):
    """user_ids and uid_owner read accounts with getent over SSH (fake ssh and fake getent)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = fake_machine(Path(self.temporary.name), [], UBUNTU)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_user_ids(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            ids = user_ids("node7", None, ["alice", "bob", "carol"])
        self.assertEqual(ids, {"alice": (1001, 1001), "bob": None, "carol": (2002, 2002)})

    def test_uid_owner(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(uid_owner("node7", None, 2002), "carol")
            self.assertIsNone(uid_owner("node7", None, 2000))

    def test_uid_problems(self) -> None:
        root = Path(self.environment["FAKE_ROOT"])
        with (root / "etc/group").open("a") as group:
            group.write("erin:x:3000:\n")
        users = [("alice", 2000), ("carol", 2002), ("dave", 1000), ("erin", 2005)]
        with mock.patch.dict(os.environ, self.environment):
            problems = uid_problems("node7", None, users)
        self.assertEqual(
            problems,
            [
                "alice has UID 1001 and primary group ID 1001 on node7, but cluster.yml says 2000 for both",
                "the group alice has group ID 1001 on node7, but nanoHPC gives it 2000 (the user's UID)",
                "UID 1000 (for dave in cluster.yml) already belongs to ubuntu on node7",
                "group ID 1000 (for dave) already belongs to the group ubuntu on node7",
                "the group erin has group ID 3000 on node7, but nanoHPC gives it 2005 (the user's UID)",
            ],
        )


class SuggestGpuTypeTest(unittest.TestCase):
    """GPU types suggested from nvidia-smi names are valid cluster.yml types."""

    def test_suggestions(self) -> None:
        cases = {
            "NVIDIA RTX A6000": "a6000",
            "NVIDIA GeForce RTX 4090": "rtx4090",
            "NVIDIA A100-SXM4-80GB": "a100-80gb",
            "NVIDIA A100-PCIE-40GB": "a100-40gb",
            "Tesla V100-SXM2-32GB": "v100-32gb",
            "NVIDIA H100 80GB HBM3": "h100",
            "NVIDIA RTX 6000 Ada Generation": "rtx6000ada",
            "NVIDIA GeForce RTX 4070 Ti SUPER": "rtx4070tisuper",
            "NVIDIA GeForce GTX 1080 Ti": "gtx1080ti",
            "Tesla T4": "t4",
            "NVIDIA L40S": "l40s",
            "Quadro RTX 8000": "rtx8000",
            "NVIDIA": "gpu",
        }
        for name, expected in cases.items():
            suggestion = suggest_gpu_type(name)
            self.assertEqual(suggestion, expected, name)
            self.assertIsNotNone(GPU_TYPE.fullmatch(suggestion), name)


if __name__ == "__main__":
    unittest.main()
