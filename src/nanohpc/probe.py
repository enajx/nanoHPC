"""What the setup wizard learns about a machine over SSH. Everything here only reads; nothing is changed.

Each machine is reached with plain `ssh` (optionally `-F ssh_config`), as `nanohpc deploy` does, but without
forwarding the SSH agent: only `nanohpc fix-uid` needs sudo through it. The facts come from one SSH call that runs a
small Python program on the machine (python3 is part of Ubuntu); it runs the usual tools (`hostname`, `nproc`,
`lscpu`, `ip`, `lsblk`, `df`, `nvidia-smi`, `sudo`) in the C locale and prints their raw output as JSON, which is
parsed here. Every SSH call has a time limit, so a machine that does not answer gives an error, not a hang.
"""

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nanohpc.config import GPU_TYPE

PROBE_TIMEOUT = 60  # seconds, for the probe and for each quick lookup (getent, pgrep, ...)
TIMED_OUT = 124  # the exit code run_remote gives a command that ran out of time (as timeout(1) does)
SUPPORTED_UBUNTU = ("22.04", "24.04", "26.04")


class SSHError(RuntimeError):
    """`ssh` could not reach the machine, or the command ran out of time."""


def ssh_args(ssh_config: Path | None, target: str, command: str, forward_agent: bool) -> list[str]:
    """Return the ssh command line that runs `command` on a machine without prompts (deploy's options). With
    `forward_agent`, the SSH agent is forwarded (-A), so an administrator's key can unlock sudo."""
    config = ["-F", str(ssh_config)] if ssh_config else []
    agent = ["-A"] if forward_agent else []
    return ["ssh", *config, *agent, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", target, command]


def run_remote(
    target: str, ssh_config: Path | None, command: str, forward_agent: bool, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a machine over SSH and return the result. Exit code 255: SSH failed; TIMED_OUT: no
    answer within `timeout` seconds (ssh is stopped)."""
    arguments = ssh_args(ssh_config, target, command, forward_agent)
    try:
        return subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A time limit is reported like a failed command, so callers report it instead of hanging.
        return subprocess.CompletedProcess(arguments, TIMED_OUT, "", f"no answer within {timeout} seconds")


def run_remote_input(
    target: str, ssh_config: Path | None, command: str, forward_agent: bool, timeout: int, input_text: str
) -> subprocess.CompletedProcess[str]:
    """Run a remote command with text on stdin, with the same SSH and timeout behavior as run_remote."""
    arguments = ssh_args(ssh_config, target, command, forward_agent)
    try:
        return subprocess.run(arguments, input=input_text, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(arguments, TIMED_OUT, "", f"no answer within {timeout} seconds")


def ssh_failure(target: str, result: subprocess.CompletedProcess[str]) -> str | None:
    """Return why SSH failed (it could not connect, an unknown host key, no answer in time), or None."""
    if result.returncode not in (255, TIMED_OUT):
        return None
    message = result.stderr.strip() or "no output"
    if "Host key verification failed" in message:
        message = (
            f"Host key verification failed. Run `ssh {target}` once in a terminal to check and accept the machine's "
            "host key."
        )
    return f"SSH failed: `ssh {target}`: {message}"


def remote(
    target: str, ssh_config: Path | None, command: str, forward_agent: bool, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a machine over SSH; raise SSHError when SSH fails or the command runs out of time."""
    result = run_remote(target, ssh_config, command, forward_agent, timeout)
    failure = ssh_failure(target, result)
    if failure is not None:
        raise SSHError(failure)
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
    pttype: str | None  # partition table (gpt, dos), None when there is none
    children: list[str]  # paths of its partitions and of the devices built on it (LVM, RAID, LUKS)
    in_use: bool  # it or a child has a filesystem, is mounted or swap, or is part of LVM, RAID, or LUKS


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
    sudo_ok: bool  # sudo works without a password (the probe does not forward the SSH agent)
    notes: list[str]  # things the administrator should know (sudo asks for a password, nvidia-smi fails)
    error: str | None  # why the machine could not be probed (SSH failed, no answer, the probe failed), or None
    free_gb: float  # free space on the root filesystem in GB (10^9 bytes), one decimal
    supported: bool  # Ubuntu in a version nanoHPC supports (SUPPORTED_UBUNTU)


# Runs on the machine. Required tools fail loudly (check=True); nvidia-smi is optional. The sudo check is the
# one `nanohpc deploy` uses: an empty stdin, so a forwarded key unlocks it and a password prompt fails at once.
PROBE_PROGRAM = r"""
import json, os, shutil, subprocess

ENVIRONMENT = dict(os.environ, LC_ALL="C")

def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=True, env=ENVIRONMENT).stdout

def optional(command):
    result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=ENVIRONMENT)
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
    "lsblk": run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,MOUNTPOINT,MODEL,TYPE,PTTYPE"]),
    "df": run(["df", "-B1", "--output=avail", "/"]),
    "nvidia_smi": None,
    "sudo": optional(["sudo", "-S", "-p", "", "true"]),
}
if shutil.which("nvidia-smi"):
    facts["nvidia_smi"] = optional(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
print(json.dumps(facts))
"""


def empty_facts(target: str, error: str) -> MachineFacts:
    """Return the facts of a machine that could not be probed."""
    return MachineFacts(target, "", [], 0, None, None, None, 0, [], [], "", "", False, [], error, 0.0, False)


def probe_machine(target: str, ssh_config: Path | None) -> MachineFacts:
    """Probe one machine over SSH (one call, read-only) and return what it is."""
    result = run_remote(target, ssh_config, f"python3 -c {shlex.quote(PROBE_PROGRAM)}", False, PROBE_TIMEOUT)
    failure = ssh_failure(target, result)
    if failure is not None:
        return empty_facts(target, failure)
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
    ubuntu = os_release.get("VERSION_ID", "") if os_id == "ubuntu" else ""
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
        ubuntu=ubuntu,
        sudo_ok=sudo_ok,
        notes=notes,
        error=None,
        free_gb=round(int(raw["df"].split()[-1]) / 1e9, 1),
        supported=ubuntu in SUPPORTED_UBUNTU,
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


def device_in_use(device: dict[str, object]) -> bool:
    """Whether an lsblk device holds something: a filesystem or any other signature (swap, LVM2_member,
    linux_raid_member, crypto_LUKS), a mount, a device built on it (LVM, RAID, LUKS), or a partition that does."""
    if device.get("fstype") or device.get("mountpoint"):
        return True
    children = device.get("children")
    if not isinstance(children, list):
        return False
    return any(child["type"] != "part" or device_in_use(child) for child in children)


def parse_disks(lsblk_json: str) -> list[Disk]:
    """Return the block devices from `lsblk -J -b`, children after their parent, without loop, rom, and zram
    devices (nor anything on top of them). A device under several parents (RAID, LVM) is listed once."""
    disks: list[Disk] = []
    seen: set[str] = set()

    def visit(device: dict[str, object]) -> None:
        kind = str(device["type"])
        path = str(device["path"])
        if kind in ("loop", "rom") or str(device["name"]).startswith("zram") or path in seen:
            return
        seen.add(path)
        model = device.get("model")
        fstype = device.get("fstype")
        mountpoint = device.get("mountpoint")
        pttype = device.get("pttype")
        children = device.get("children")
        children = children if isinstance(children, list) else []
        disks.append(
            Disk(
                path=path,
                size_gb=round(int(str(device["size"])) / 1e9, 1),
                fstype=str(fstype) if fstype else None,
                mountpoint=str(mountpoint) if mountpoint else None,
                model=str(model).strip() if model else None,
                kind=kind,
                pttype=str(pttype) if pttype else None,
                children=[str(child["path"]) for child in children],
                in_use=device_in_use(device),
            )
        )
        for child in children:
            visit(child)

    for device in json.loads(lsblk_json)["blockdevices"]:
        visit(device)
    return disks


def getent(target: str, ssh_config: Path | None, database: str, keys: list[str], local_only: bool) -> list[list[str]]:
    """Return the entries (split on ':') `getent DATABASE KEYS...` finds on a machine; missing keys are left out.
    `local_only`: only the local files (/etc/passwd, /etc/group), not LDAP or another directory."""
    sources = ["-s", "files"] if local_only else []
    result = remote(target, ssh_config, shlex.join(["getent", *sources, database, *keys]), False, PROBE_TIMEOUT)
    # getent: 0 all found, 2 some key not found.
    if result.returncode not in (0, 2):
        raise RuntimeError(f"getent {database} on {target} failed: {result.stderr.strip() or 'no output'}")
    return [line.split(":") for line in result.stdout.splitlines() if line]


def user_ids(target: str, ssh_config: Path | None, names: list[str]) -> dict[str, tuple[int, int] | None]:
    """Return each user's (UID, primary GID) on a machine, or None for a user that has no account there."""
    entries = getent(target, ssh_config, "passwd", names, False)
    found = {entry[0]: (int(entry[2]), int(entry[3])) for entry in entries}
    return {name: found.get(name) for name in names}


def uid_owner(target: str, ssh_config: Path | None, uid: int) -> str | None:
    """Return the name of the account with this UID on a machine, or None when no account has it."""
    entries = getent(target, ssh_config, "passwd", [str(uid)], False)
    return entries[0][0] if entries else None


def uid_problems(target: str, ssh_config: Path | None, users: list[tuple[str, int]]) -> list[str]:
    """Return what would stop `nanohpc deploy` on a machine for these (name, UID) users from cluster.yml, in plain
    English: the same conditions as the deploy's preflight checks. An empty list means no problem.

    Per user: the account has another UID or primary group ID; the UID belongs to another account; the group named
    after the user has another group ID; the group ID (the same number as the UID) belongs to another group."""
    keys = [name for name, _ in users] + [str(uid) for _, uid in users]
    accounts = getent(target, ssh_config, "passwd", keys, False)
    groups = getent(target, ssh_config, "group", keys, False)
    return account_problems(target, users, accounts, groups)


def account_problems(
    target: str, users: list[tuple[str, int]], accounts: list[list[str]], groups: list[list[str]]
) -> list[str]:
    """Return uid_problems' list from the passwd and group entries (split on ':') that `getent` found on a machine
    for the users' names and UIDs."""
    account_by_name = {entry[0]: entry for entry in accounts}
    group_by_name = {entry[0]: entry for entry in groups}
    problems: list[str] = []
    for name, uid in users:
        account = account_by_name.get(name)
        if account is not None and (int(account[2]), int(account[3])) != (uid, uid):
            problems.append(
                f"{name} has UID {account[2]} and primary group ID {account[3]} on {target}, "
                f"but cluster.yml says {uid} for both"
            )
        owners = sorted({entry[0] for entry in accounts if int(entry[2]) == uid and entry[0] != name})
        if owners:
            problems.append(f"UID {uid} (for {name} in cluster.yml) already belongs to {', '.join(owners)} on {target}")
        group = group_by_name.get(name)
        if group is not None and int(group[2]) != uid:
            problems.append(
                f"the group {name} has group ID {group[2]} on {target}, but nanoHPC gives it {uid} (the user's UID)"
            )
        group_owners = sorted({entry[0] for entry in groups if int(entry[2]) == uid and entry[0] != name})
        if group_owners:
            problems.append(
                f"group ID {uid} (for {name}) already belongs to the group {', '.join(group_owners)} on {target}"
            )
    return problems


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
