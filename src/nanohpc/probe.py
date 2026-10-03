"""What the setup wizard learns about a machine over SSH. Everything here only reads; nothing is changed.

Each machine is reached with plain `ssh` (optionally `-F ssh_config`), as `nanohpc deploy` does. The facts come from
one SSH call that runs a small Python program on the machine (python3 is part of Ubuntu); it runs the usual tools
(`hostname`, `nproc`, `lscpu`, `ip`, `lsblk`, `nvidia-smi`, `sudo`) and prints their raw output as JSON, which is
parsed here.
"""

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nanohpc.config import GPU_TYPE
from nanohpc.deploy import ssh_command


class SSHError(RuntimeError):
    """`ssh` could not reach the machine (it exited with 255)."""


def run_remote(target: str, ssh_config: Path | None, command: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a machine over SSH and return the result (exit code 255: SSH failed)."""
    return subprocess.run(ssh_command(ssh_config, target, command), capture_output=True, text=True, check=False)


def remote(target: str, ssh_config: Path | None, command: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a machine over SSH; raise SSHError when SSH itself fails."""
    result = run_remote(target, ssh_config, command)
    if result.returncode == 255:
        raise SSHError(f"cannot connect with `ssh {target}`: {result.stderr.strip() or 'no output'}")
    return result


@dataclass(frozen=True)
class Disk:
    """A whole disk or a partition (or another block device on top of one, such as LVM), from lsblk."""

    path: str  # /dev/sda, /dev/nvme0n1p1
    size_gb: float  # in GB (10^9 bytes), one decimal
    fstype: str | None  # filesystem on it, None when there is none
    mountpoint: str | None  # where it is mounted, None when it is not
    model: str | None  # the disk's model (whole disks only)
    kind: str  # lsblk's TYPE: disk, part, lvm, raid1, crypt, ...


@dataclass(frozen=True)
class MachineFacts:
    """What one machine is, as read over SSH. When `error` is set, the other fields are empty."""

    target: str  # what the administrator typed: a name, an address, or a host from ~/.ssh/config
    hostname: str  # short hostname (`hostname -s`)
    addresses: list[str]  # IPv4 addresses, without loopback
    cpus: int  # logical CPUs (`nproc`)
    sockets: int | None  # from lscpu; None when lscpu does not say (some ARM machines)
    cores_per_socket: int | None
    threads_per_core: int | None
    memory_mb: int  # MemTotal in MiB, rounded down
    gpus: list[str]  # NVIDIA GPU model names, one per GPU; empty without nvidia-smi
    disks: list[Disk]  # whole disks and partitions, without loop, rom, and zram devices
    os_id: str  # ID from /etc/os-release: ubuntu, debian, ...
    ubuntu: str  # VERSION_ID from /etc/os-release (24.04), or "" when the machine is not Ubuntu
    sudo_ok: bool  # sudo works without a password (passwordless, or unlocked by the forwarded SSH key)
    notes: list[str]  # things the administrator should know (sudo asks for a password, nvidia-smi fails)
    error: str | None  # why the machine could not be probed (SSH failed, the probe failed), or None


# Runs on the machine. Required tools fail loudly (check=True); nvidia-smi is optional. The sudo check is the
# one `nanohpc deploy` uses: an empty stdin, so a forwarded key unlocks it and a password prompt fails at once.
PROBE_PROGRAM = r"""
import json, shutil, subprocess

def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout

def optional(command):
    result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return {"code": result.returncode, "out": result.stdout, "err": result.stderr}

def read(path):
    with open(path) as file:
        return file.read()

facts = {
    "hostname": run(["hostname", "-s"]),
    "nproc": run(["nproc"]),
    "lscpu": run(["lscpu"]),
    "meminfo": read("/proc/meminfo"),
    "os_release": read("/etc/os-release"),
    "ip": run(["ip", "-4", "-o", "addr", "show"]),
    "lsblk": run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,MOUNTPOINT,MODEL,TYPE"]),
    "nvidia_smi": None,
    "sudo": optional(["sudo", "-S", "-p", "", "true"]),
}
if shutil.which("nvidia-smi"):
    facts["nvidia_smi"] = optional(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
print(json.dumps(facts))
"""


def empty_facts(target: str, error: str) -> MachineFacts:
    """Return the facts of a machine that could not be probed."""
    return MachineFacts(target, "", [], 0, None, None, None, 0, [], [], "", "", False, [], error)


def probe_machine(target: str, ssh_config: Path | None) -> MachineFacts:
    """Probe one machine over SSH (one call, read-only) and return what it is."""
    result = run_remote(target, ssh_config, f"python3 -c {shlex.quote(PROBE_PROGRAM)}")
    if result.returncode == 255:
        return empty_facts(target, f"SSH failed: `ssh {target}`: {result.stderr.strip() or 'no output'}")
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return empty_facts(target, f"the probe failed on {target}: {lines[-1] if lines else 'no output'}")
    raw = json.loads(result.stdout)
    os_release = parse_os_release(raw["os_release"])
    os_id = os_release.get("ID", "")
    notes: list[str] = []
    sudo = raw["sudo"]
    sudo_ok = sudo["code"] == 0
    if not sudo_ok:
        text = sudo["out"] + sudo["err"]
        if "sudoers" in text or "not allowed" in text:
            notes.append(f"the account `ssh {target}` logs in with is not allowed to use sudo")
        else:
            notes.append("sudo asks for a password")
    gpus: list[str] = []
    smi = raw["nvidia_smi"]
    if smi is not None:
        if smi["code"] == 0:
            gpus = [line.strip() for line in smi["out"].splitlines() if line.strip()]
        else:
            message = (smi["out"] + smi["err"]).strip().splitlines()
            notes.append(f"nvidia-smi is installed but failed: {message[0] if message else 'no output'}")
    cpu = parse_lscpu(raw["lscpu"])
    return MachineFacts(
        target=target,
        hostname=raw["hostname"].strip(),
        addresses=parse_addresses(raw["ip"]),
        cpus=int(raw["nproc"]),
        sockets=cpu["Socket(s)"],
        cores_per_socket=cpu["Core(s) per socket"],
        threads_per_core=cpu["Thread(s) per core"],
        memory_mb=parse_memory_mb(raw["meminfo"]),
        gpus=gpus,
        disks=parse_disks(raw["lsblk"]),
        os_id=os_id,
        ubuntu=os_release.get("VERSION_ID", "") if os_id == "ubuntu" else "",
        sudo_ok=sudo_ok,
        notes=notes,
        error=None,
    )


def parse_os_release(text: str) -> dict[str, str]:
    """Return the KEY=value pairs of /etc/os-release, without quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def parse_lscpu(text: str) -> dict[str, int | None]:
    """Return lscpu's socket, core, and thread counts (None when a count is missing or not a number)."""
    counts: dict[str, int | None] = {"Socket(s)": None, "Core(s) per socket": None, "Thread(s) per core": None}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in counts and value.strip().isdigit():
            counts[key.strip()] = int(value.strip())
    return counts


def parse_memory_mb(meminfo: str) -> int:
    """Return MemTotal from /proc/meminfo in MiB, rounded down."""
    match = re.search(r"^MemTotal:\s+(\d+) kB$", meminfo, re.MULTILINE)
    if match is None:
        raise ValueError("no MemTotal line in /proc/meminfo")
    return int(match.group(1)) // 1024


def parse_addresses(ip_output: str) -> list[str]:
    """Return the IPv4 addresses from `ip -4 -o addr show`, without loopback ones."""
    addresses: list[str] = []
    for line in ip_output.splitlines():
        fields = line.split()
        if "inet" not in fields:
            continue
        address = fields[fields.index("inet") + 1].split("/")[0]
        if not address.startswith("127."):
            addresses.append(address)
    return addresses


def parse_disks(lsblk_json: str) -> list[Disk]:
    """Return the block devices from `lsblk -J -b`, children after their parent, without loop, rom, and zram
    devices (nor anything on top of them)."""
    disks: list[Disk] = []

    def visit(device: dict[str, object]) -> None:
        kind = str(device["type"])
        if kind in ("loop", "rom") or str(device["name"]).startswith("zram"):
            return
        model = device.get("model")
        fstype = device.get("fstype")
        mountpoint = device.get("mountpoint")
        disks.append(
            Disk(
                path=str(device["path"]),
                size_gb=round(int(str(device["size"])) / 1e9, 1),
                fstype=str(fstype) if fstype else None,
                mountpoint=str(mountpoint) if mountpoint else None,
                model=str(model).strip() if model else None,
                kind=kind,
            )
        )
        children = device.get("children")
        if isinstance(children, list):
            for child in children:
                visit(child)

    for device in json.loads(lsblk_json)["blockdevices"]:
        visit(device)
    return disks


def getent(target: str, ssh_config: Path | None, database: str, keys: list[str]) -> list[list[str]]:
    """Return the entries (split on ':') `getent DATABASE KEYS...` finds on a machine; missing keys are left out."""
    result = remote(target, ssh_config, shlex.join(["getent", database, *keys]))
    # getent: 0 all found, 2 some key not found.
    if result.returncode not in (0, 2):
        raise RuntimeError(f"getent {database} on {target} failed: {result.stderr.strip() or 'no output'}")
    return [line.split(":") for line in result.stdout.splitlines() if line]


def user_ids(target: str, ssh_config: Path | None, names: list[str]) -> dict[str, tuple[int, int] | None]:
    """Return each user's (UID, primary GID) on a machine, or None for a user that has no account there."""
    found = {entry[0]: (int(entry[2]), int(entry[3])) for entry in getent(target, ssh_config, "passwd", names)}
    return {name: found.get(name) for name in names}


def uid_owner(target: str, ssh_config: Path | None, uid: int) -> str | None:
    """Return the name of the account with this UID on a machine, or None when no account has it."""
    entries = getent(target, ssh_config, "passwd", [str(uid)])
    return entries[0][0] if entries else None


# Words in nvidia-smi names that are brands, not models.
GPU_BRANDS = ("nvidia", "geforce", "tesla", "quadro")
# Words after the model that tell variants apart and are kept: rtx4070ti, rtx4080super, rtx6000ada.
GPU_VARIANTS = ("ti", "super", "ada")


def suggest_gpu_type(model: str) -> str:
    """Suggest a cluster.yml GPU type (a Slurm GPU type) for an nvidia-smi GPU name. Rules:

    - lowercase; drop the brand words NVIDIA, GeForce, Tesla, Quadro;
    - RTX/GTX followed by a number joins it: "RTX 4090" -> rtx4090, "GTX 1080 Ti" -> gtx1080ti; followed by a
      letter and digits, RTX is dropped: "RTX A6000" -> a6000;
    - a model written with dashes keeps the model and its memory size and drops the form factor:
      "A100-SXM4-80GB" -> a100-80gb, "V100-PCIE-32GB" -> v100-32gb;
    - words after the model are dropped (memory size and type, PCIe, NVL), except Ti, SUPER, and Ada, which are
      appended: "H100 80GB HBM3" -> h100, "RTX 6000 Ada Generation" -> rtx6000ada;
    - anything else left that is not a valid type gives "gpu".
    The wizard shows the suggestion so the administrator can change it.
    """
    words = [word for word in model.lower().split() if word not in GPU_BRANDS]
    if not words:
        return "gpu"
    if words[0] in ("rtx", "gtx") and len(words) > 1:
        series, number = words[0], words[1]
        head = number if re.fullmatch(r"[a-z]\d+", number) else series + number
        rest = words[2:]
    else:
        head, rest = words[0], words[1:]
    parts = head.split("-")
    gpu_type = "-".join([parts[0], *[part for part in parts[1:] if re.fullmatch(r"\d+gb", part)]])
    gpu_type += "".join(word for word in rest if word in GPU_VARIANTS)
    gpu_type = re.sub(r"[^a-z0-9_-]", "", gpu_type)
    return gpu_type if GPU_TYPE.fullmatch(gpu_type) else "gpu"
