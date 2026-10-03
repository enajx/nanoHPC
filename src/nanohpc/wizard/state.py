"""What the setup wizard keeps while it runs, and the plain rules it uses (no widgets here).

The wizard edits one cluster.yml (a `ClusterFile`) in memory, and remembers what it learned from the machines
(probe facts, UID checks) for this session only: none of that is written to the file.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanohpc.clusterfile import ClusterFile
from nanohpc.config import MACHINE_FIELDS, is_ipv4
from nanohpc.fixuid import FixPlan
from nanohpc.probe import Disk, MachineFacts, suggest_gpu_type

AGENTS_URL = "https://github.com/enajx/nanoHPC/blob/main/SETUP-for-AGENTS.md"

# The wizard's steps, in order: (id of the step's widget, title).
STEPS: tuple[tuple[str, str], ...] = (
    ("machines", "Machines"),
    ("storage", "Storage"),
    ("users", "Users"),
    ("partitions", "Partitions"),
    ("website", "Website"),
    ("extras", "Extras"),
    ("review", "Review"),
)
MACHINES, STORAGE, USERS, PARTITIONS, WEBSITE, EXTRAS, REVIEW = range(len(STEPS))
FIRST_UID = 2000
COMPUTE_ONLY = ("cpu", "memory_mb", "gpu", "partitions", "scratch")


@dataclass(frozen=True)
class Dependencies:
    """The functions the wizard uses to reach the machines. The CLI passes the real ones (probe.py and
    fixuid.py); tests pass fakes."""

    probe_machine: Callable[[str, Path | None], MachineFacts]
    user_ids: Callable[[str, Path | None, list[str]], dict[str, tuple[int, int] | None]]
    plan_fix: Callable[[str, Path | None, str, int], FixPlan]
    apply_fix: Callable[[str, Path | None, FixPlan], int]


@dataclass
class WizardState:
    """The file being edited and what the wizard learned about the machines in this session."""

    path: Path
    ssh_config: Path | None
    deps: Dependencies
    file: ClusterFile
    # How the wizard reaches each machine with ssh: what the administrator typed, or the machine's name
    # (which is what `nanohpc deploy` uses).
    targets: dict[str, str] = field(default_factory=dict)
    facts: dict[str, MachineFacts] = field(default_factory=dict)
    probing: set[str] = field(default_factory=set)
    # UID check: machine -> user -> (UID, GID) on that machine, or None when the user has no account there.
    uid_checks: dict[str, dict[str, tuple[int, int] | None]] = field(default_factory=dict)
    uid_errors: dict[str, str] = field(default_factory=dict)

    def target(self, machine: str) -> str:
        """Return the ssh target for a machine: the one typed when it was added, or its name."""
        return self.targets.get(machine, machine)


def error_step(error: str) -> int:
    """Return the step where a validation error from clusterfile.validate() is fixed."""
    head = error.split(" ", 1)[0].rstrip(":")
    parts = head.split(".")
    section = parts[0].split("[")[0]
    if section == "cluster":
        if len(parts) > 1 and parts[1].startswith("admins"):
            return USERS
        return WEBSITE
    if section == "machines":
        if "backup.to" in error:
            return EXTRAS
        if len(parts) == 1:
            return STORAGE if "home role" in error else MACHINES
        if len(parts) > 2 and parts[2] in ("home", "scratch"):
            return STORAGE
        if len(parts) > 2 and parts[2].startswith("partitions"):
            return PARTITIONS
        return MACHINES
    if section == "users":
        return USERS
    if section in ("partitions", "policy"):
        return PARTITIONS
    if section in ("home", "scratch"):
        return STORAGE
    if section in ("backup", "alerts", "auto_deploy", "nanohpc_version"):
        return EXTRAS
    return REVIEW


def next_free_uid(users: list[dict[str, Any]]) -> int:
    """Return the lowest UID from 2000 up that no user in the file has."""
    used = {user.get("uid") for user in users}
    uid = FIRST_UID
    while uid in used:
        uid += 1
    return uid


def default_partition(partitions: dict[str, dict[str, Any]]) -> str | None:
    """Return the partition with default: true, else the first one, else None."""
    for name, partition in partitions.items():
        if partition is not None and partition.get("default") is True:
            return name
    return next(iter(partitions), None)


def suggested_memory_mb(facts: MachineFacts) -> int:
    """Return memory_mb to write for a machine: a little below its total (95%), as Slurm needs it to be
    no more than what the machine reports."""
    return facts.memory_mb * 95 // 100


def cpu_from_facts(facts: MachineFacts) -> dict[str, int]:
    """Return the cpu field for a machine from lscpu; without lscpu's numbers, one socket of nproc cores."""
    if facts.sockets and facts.cores_per_socket and facts.threads_per_core:
        return {
            "sockets": facts.sockets,
            "cores_per_socket": facts.cores_per_socket,
            "threads_per_core": facts.threads_per_core,
        }
    return {"sockets": 1, "cores_per_socket": facts.cpus, "threads_per_core": 1}


def gpu_from_facts(facts: MachineFacts) -> dict[str, Any] | None:
    """Return the gpu field for a machine (suggested type of its first GPU, and the count), or None."""
    if not facts.gpus:
        return None
    return {"type": suggest_gpu_type(facts.gpus[0]), "count": len(facts.gpus)}


def ordered(machine: dict[str, Any]) -> dict[str, Any]:
    """Return a machine's fields in the order of the examples (new fields are added in this order)."""
    known = {key: machine[key] for key in MACHINE_FIELDS if key in machine}
    return {**known, **{key: value for key, value in machine.items() if key not in known}}


def fill_from_facts(machine: dict[str, Any], facts: MachineFacts, partition: str | None) -> dict[str, Any]:
    """Return the machine with what the probe found filled in where the file has nothing yet: the address
    (the first probed one), and for a compute machine cpu, memory_mb, gpu, and partitions. Values already
    in the file are kept."""
    result = dict(machine)
    if facts.error is not None:
        return result
    address = result.get("address")
    if facts.addresses and not (isinstance(address, str) and is_ipv4(address)):
        result["address"] = facts.addresses[0]
    if "compute" in (result.get("roles") or []):
        result.setdefault("cpu", cpu_from_facts(facts))
        result.setdefault("memory_mb", suggested_memory_mb(facts))
        gpu = gpu_from_facts(facts)
        if gpu is not None:
            result.setdefault("gpu", gpu)
        if partition is not None:
            result.setdefault("partitions", [partition])
    return ordered(result)


def with_roles(machine: dict[str, Any], roles: list[str]) -> dict[str, Any]:
    """Return the machine with new roles, without the fields its roles no longer use."""
    result = {**machine, "roles": roles}
    if "compute" not in roles:
        for key in COMPUTE_ONLY:
            result.pop(key, None)
    if "home" not in roles:
        result.pop("home", None)
    if "backup" not in roles:
        result.pop("backup", None)
    return result


def probe_summary(facts: MachineFacts | None, probing: bool) -> str:
    """Return the probe column of the machines table."""
    if probing:
        return "… probing"
    if facts is None:
        return "not probed"
    if facts.error is not None:
        return f"✗ {facts.error}"
    parts = [f"✓ {facts.cpus}c", f"{facts.memory_mb // 1024}G"]
    if facts.gpus:
        parts.append(f"{len(facts.gpus)} GPU")
    parts.append(f"Ubuntu {facts.ubuntu}" if facts.ubuntu else f"not Ubuntu ({facts.os_id or 'unknown'})")
    if not facts.sudo_ok:
        parts.append("! sudo asks for a password")
    return " · ".join(parts)


def facts_details(facts: MachineFacts) -> list[str]:
    """Return the lines that describe a probed machine in full."""
    if facts.error is not None:
        return [f"✗ {facts.target}: {facts.error}"]
    lines = [
        f"{facts.target}: hostname {facts.hostname}, addresses {', '.join(facts.addresses) or 'none'}",
        f"CPUs {facts.cpus}, memory {facts.memory_mb} MiB, Ubuntu {facts.ubuntu or 'no'}",
        f"GPUs: {', '.join(facts.gpus) if facts.gpus else 'none'}",
        "Disks: " + ("; ".join(disk_label(disk) for disk in facts.disks) or "none"),
    ]
    if not facts.sudo_ok:
        lines.append(
            "! sudo asks for a password here: load your key with ssh-add (sudo by forwarded key), "
            "or nanohpc deploy will ask for the password once"
        )
    lines += [f"! {note}" for note in facts.notes]
    return lines


def disk_label(disk: Disk) -> str:
    """Return a disk as shown in the wizard: path, size, filesystem, and mount point."""
    filesystem = disk.fstype or "no filesystem"
    mounted = f"mounted at {disk.mountpoint}" if disk.mountpoint else "not mounted"
    model = f", {disk.model}" if disk.model else ""
    return f"{disk.path} ({disk.size_gb:g} GB, {filesystem}, {mounted}{model})"


def mkfs_command(device: str, home: bool) -> str:
    """Return the command that makes the filesystem nanoHPC needs on an empty disk (with quotas for /home)."""
    return f"sudo mkfs.ext4 -O quota {device}" if home else f"sudo mkfs.ext4 {device}"


def disk_advice(facts: MachineFacts | None, device: str, home: bool) -> str | None:
    """Return what the administrator must do before the deploy for the chosen disk, or None. The wizard
    never formats a disk: it only shows the command."""
    if facts is None or facts.error is not None:
        return None
    disk = next((disk for disk in facts.disks if disk.path == device), None)
    if disk is None:
        return f"{device} was not found on {facts.target}"
    if disk.mountpoint is not None:
        return f"{device} is mounted at {disk.mountpoint}: choose a spare disk"
    if disk.fstype is None:
        return (
            f"{device} has no filesystem. On {facts.target}, run: {mkfs_command(device, home)}\n"
            "The wizard never formats a disk; run this yourself, or skip and do it later (before the deploy)."
        )
    return None


def field_text(value: Any) -> str:
    """Return a value from the file as the text of an input field."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def parse_field(text: str, kind: str) -> Any:
    """Return the value to write for an input field's text. `kind`: text, int (a whole number, or the
    text as typed so validation names it), or list (comma-separated)."""
    if kind == "list":
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind == "int":
        return int(text) if text.isdigit() else text
    return text


def remove_value(file: ClusterFile, path: list[str]) -> None:
    """Remove the setting at `path` (so nanoHPC's default applies); a top-level one is set to null."""
    if len(path) == 1:
        if file.get(path) is not None:
            file.set_value(path, None)
        return
    parent = file.get(path[:-1])
    if isinstance(parent, dict) and path[-1] in parent:
        del parent[path[-1]]
        file.set_value(path[:-1], parent)


def write_field(file: ClusterFile, path: list[str], text: str, kind: str) -> bool:
    """Write an input field's text to the file; empty text removes the setting. Return whether the file
    changed (text equal to what the file has already changes nothing)."""
    if text == field_text(file.get(path)):
        return False
    if text == "":
        remove_value(file, path)
    else:
        file.set_value(path, parse_field(text, kind))
    return True


def uid_conflicts(state: WizardState) -> list[tuple[str, str, tuple[int, int]]]:
    """Return (machine, user, (UID, GID) there) for each checked user whose UID or GID on a machine differs
    from the UID in cluster.yml. A missing account is not a conflict: the deploy creates it."""
    expected = {user["name"]: user["uid"] for user in state.file.users() if "name" in user and "uid" in user}
    conflicts = []
    for machine, found in state.uid_checks.items():
        for user, ids in found.items():
            if ids is not None and user in expected and ids != (expected[user], expected[user]):
                conflicts.append((machine, user, ids))
    return conflicts
