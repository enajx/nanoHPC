"""What the setup wizard keeps while it runs, and the plain rules it uses (no widgets here).

The wizard edits one cluster.yml (a `ClusterFile`) in memory, and remembers what it learned from the machines
(probe facts, UID checks, skipped preparation items) for this session only: none of that is written to the file.
"""

import datetime
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanohpc.clusterfile import ClusterFile
from nanohpc.config import MACHINE_FIELDS, SCRATCH_DEFAULTS, is_ipv4
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
# Filesystem types that mean a disk belongs to something else (swap, LVM, software RAID, encryption, ZFS).
MEMBER_FSTYPES = ("swap", "LVM2_member", "linux_raid_member", "crypto_LUKS", "zfs_member", "bcache")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Dependencies:
    """The functions the wizard uses to reach the machines. The CLI passes the real ones (probe.py and
    fixuid.py); tests pass fakes. Only apply_fix changes a machine."""

    probe_machine: Callable[[str, Path | None], MachineFacts]
    user_ids: Callable[[str, Path | None, list[str]], dict[str, tuple[int, int] | None]]
    uid_problems: Callable[[str, Path | None, list[tuple[str, int]]], list[str]]
    uid_owner: Callable[[str, Path | None, int], str | None]
    plan_fix: Callable[[str, Path | None, str, int], FixPlan]
    apply_fix: Callable[[str, Path | None, FixPlan], list[str]]


@dataclass(frozen=True)
class UidCheck:
    """What the UID check found on one machine: each user's (UID, GID) there, or None when the user has no
    account there; and the other problems (a UID owned by another account, group conflicts)."""

    ids: dict[str, tuple[int, int] | None]
    problems: list[str]


@dataclass
class WizardState:
    """The file being edited and what the wizard learned about the machines in this session."""

    path: Path
    ssh_config: Path | None
    deps: Dependencies
    file: ClusterFile
    saved_text: str | None  # the text on disk; None for a new file not saved yet
    # How the wizard reaches each machine with ssh: what the administrator typed, or the machine's name
    # (which is what `nanohpc deploy` uses).
    targets: dict[str, str]
    facts: dict[str, MachineFacts]
    probing: set[str]
    uid_checks: dict[str, UidCheck]
    uid_errors: dict[str, str]
    skipped: set[tuple[str, str]]  # (machine, preparation item) the administrator will do later

    def target(self, machine: str) -> str:
        """Return the ssh target for a machine: the one typed when it was added, or its name."""
        return self.targets.get(machine, machine)

    def dirty(self) -> bool:
        """Return whether the file in memory differs from the file on disk."""
        return self.file.as_text() != self.saved_text

    def probed(self) -> list[str]:
        """Return the machines in the file that were probed without an error."""
        return [name for name in self.file.machines() if name in self.facts and self.facts[name].error is None]

    def forget(self, machine: str) -> None:
        """Drop everything the session learned about a machine (after it is removed from the file)."""
        for store in (self.targets, self.facts, self.uid_checks, self.uid_errors):
            store.pop(machine, None)
        self.probing.discard(machine)
        self.skipped = {item for item in self.skipped if item[0] != machine}


def new_state(
    path: Path, ssh_config: Path | None, deps: Dependencies, file: ClusterFile, saved: str | None
) -> WizardState:
    """Return the state of a wizard that has just opened `file`."""
    return WizardState(path, ssh_config, deps, file, saved, {}, {}, set(), {}, {}, set())


def widget_id(prefix: str, name: str) -> str:
    """Return a widget id for a machine, user, or partition name; names with other characters than letters,
    digits, _ and - (allowed in machine names in a hand-written file) are written in hex."""
    return f"{prefix}-{name}" if SAFE_ID.fullmatch(name) else f"{prefix}-x{name.encode().hex()}"


def unsupported_values(value: Any, path: str) -> list[str]:
    """Return the places in the file holding values the wizard cannot edit (such as dates)."""
    if isinstance(value, dict):
        return [problem for key, item in value.items() for problem in unsupported_values(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [problem for index, item in enumerate(value) for problem in unsupported_values(item, f"{path}[{index}]")]
    if value is None or isinstance(value, (str, int, float, bool)) and not isinstance(value, datetime.date):
        return []
    return [f"{path.lstrip('.')}: {value!r} is not text, a number, or true/false (write it in quotes)"]


def shape_problems(data: Any) -> list[str]:
    """Return why the wizard cannot show this file (sections or fields of the wrong kind, or values such as
    dates); an empty list when it can. Other mistakes are shown in the steps as validation errors."""
    if not isinstance(data, dict):
        return ["the file must be a mapping with the sections of cluster.yml"]
    problems = unsupported_values(data, "")

    def expect(value: Any, kind: type, path: str) -> bool:
        if value is None or isinstance(value, kind):
            return True
        problems.append(f"{path} must be a {'mapping' if kind is dict else 'list'}")
        return False

    for section in (
        "cluster",
        "machines",
        "partitions",
        "policy",
        "home",
        "scratch",
        "backup",
        "alerts",
        "auto_deploy",
    ):
        expect(data.get(section), dict, section)
    expect(data.get("users"), list, "users")
    cluster = data.get("cluster")
    if isinstance(cluster, dict):
        expect(cluster.get("website"), dict, "cluster.website")
        expect(cluster.get("admins"), list, "cluster.admins")
    machines = data.get("machines")
    for name, machine in machines.items() if isinstance(machines, dict) else []:
        if expect(machine, dict, f"machines.{name}") and isinstance(machine, dict):
            for key in ("cpu", "gpu", "home", "scratch", "backup"):
                expect(machine.get(key), dict, f"machines.{name}.{key}")
            for key in ("roles", "partitions", "aliases"):
                expect(machine.get(key), list, f"machines.{name}.{key}")
    users = data.get("users")
    for index, user in enumerate(users if isinstance(users, list) else []):
        if expect(user, dict, f"users[{index}]") and isinstance(user, dict):
            expect(user.get("ssh_keys"), list, f"users[{index}].ssh_keys")
    partitions = data.get("partitions")
    for name, partition in partitions.items() if isinstance(partitions, dict) else []:
        expect(partition, dict, f"partitions.{name}")
    return problems


def error_step(error: str) -> int:
    """Return the step where a validation error is fixed."""
    if ".env" in error or error.startswith("NANOHPC_"):
        return EXTRAS
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


def wizard_errors(state: WizardState) -> list[str]:
    """Return every error that makes `nanohpc validate` fail for the file as it would be saved (website
    files and .env secrets included), and the placeholder website hostname of a new file."""
    errors = state.file.validate_in(state.path.parent)
    name = state.file.get(["cluster", "name"])
    hostname = state.file.get(["cluster", "website", "hostname"])
    if isinstance(name, str) and hostname == f"{name}.example.org":
        errors.append(f"cluster.website.hostname {hostname} is a placeholder: write the website's real hostname")
    for machine, values in state.file.machines().items():
        image = ((values or {}).get("scratch") or {}).get("image_gb")
        free = free_gb(state.facts.get(machine))
        if isinstance(image, int) and free is not None and image > free:
            errors.append(
                f"machines.{machine}.scratch.image_gb {image} GB is more than the {free:g} GB free on its root disk"
            )
    return errors


def free_gb(facts: MachineFacts | None) -> float | None:
    """Return the free space on a probed machine's root disk in GB, or None when it is not known."""
    if facts is None or facts.error is not None:
        return None
    return facts.free_gb


def next_free_uid(users: list[dict[str, Any]]) -> int:
    """Return the lowest UID from 2000 up that no user in the file has."""
    used = {user.get("uid") for user in users}
    uid = FIRST_UID
    while uid in used:
        uid += 1
    return uid


def suggest_uid(users: list[dict[str, Any]], found: list[tuple[int, int] | None], taken: Callable[[int], bool]) -> int:
    """Return the UID to suggest for a new user: the UID the user already has on the probed machines (`found`,
    one entry per machine) when it is the same on every machine that has the account and no other user in the
    file has it; else the lowest UID from 2000 up that is free in the file and, by `taken`, on the machines."""
    used = {user.get("uid") for user in users}
    existing = {ids[0] for ids in found if ids is not None}
    if len(existing) == 1:
        uid = existing.pop()
        if uid not in used:
            return uid
    uid = FIRST_UID
    while uid in used or taken(uid):
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


def needs_address(machine: dict[str, Any], facts: MachineFacts) -> bool:
    """Return whether the administrator must choose the machine's address among several probed ones."""
    address = machine.get("address")
    has_address = isinstance(address, str) and is_ipv4(address)
    return facts.error is None and not has_address and len(facts.addresses) > 1


def fill_from_facts(machine: dict[str, Any], facts: MachineFacts, partition: str | None) -> dict[str, Any]:
    """Return the machine with what the probe found filled in where the file has nothing yet: the address
    (when the probe found exactly one; with several, the administrator chooses), and for a compute machine
    cpu, memory_mb, gpu, and partitions. Values already in the file are kept."""
    result = dict(machine)
    if facts.error is not None:
        return result
    address = result.get("address")
    if len(facts.addresses) == 1 and not (isinstance(address, str) and is_ipv4(address)):
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


def ssh_hint(error: str, target: str) -> str:
    """Return what to do when ssh to a machine failed."""
    if "Host key verification failed" in error or "REMOTE HOST IDENTIFICATION HAS CHANGED" in error:
        return (
            f"Accept the machine's host key once: run ssh {target} in a terminal, check the fingerprint, answer "
            "yes; then probe again (p)."
        )
    if "Permission denied" in error:
        return f"Put your public key on the machine (ssh-copy-id {target}) and load it (ssh-add); then probe again (p)."
    return f"Check that ssh {target} works in a terminal (address, the machine is on, your key); then probe again (p)."


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
    if not facts.supported:
        parts.append(f"! {facts.os_id or 'unknown OS'} {facts.ubuntu} not supported".replace("  ", " "))
    else:
        parts.append(f"Ubuntu {facts.ubuntu}")
    if not facts.sudo_ok:
        parts.append("sudo asks for a password")
    return " · ".join(parts)


def facts_details(facts: MachineFacts, target: str) -> list[str]:
    """Return the lines that describe a probed machine in full."""
    if facts.error is not None:
        return [f"✗ {facts.target}: {facts.error}", ssh_hint(facts.error, target)]
    lines = [
        f"{facts.target}: hostname {facts.hostname}, addresses {', '.join(facts.addresses) or 'none'}",
        (
            f"CPUs {facts.cpus}, memory {facts.memory_mb} MiB, {facts.os_id} {facts.ubuntu}, "
            f"{facts.free_gb:g} GB free on the root disk"
        ),
        f"GPUs: {', '.join(facts.gpus) if facts.gpus else 'none'}",
        "Disks: " + ("; ".join(disk_label(disk) for disk in facts.disks) or "none"),
    ]
    lines += [f"! {note}" for note in facts.notes]
    return lines


def disk_in_use(disk: Disk) -> str | None:
    """Return why a disk is in use (so it cannot become /home or scratch), or None when it is free."""
    if disk.mountpoint is not None:
        return f"mounted at {disk.mountpoint}"
    if disk.fstype in MEMBER_FSTYPES:
        return f"part of {disk.fstype}"
    if disk.pttype is not None:
        return f"has a partition table ({disk.pttype})"
    if disk.children:
        return f"{', '.join(disk.children)} on it"
    if disk.in_use and disk.fstype is None:
        return "in use"
    return None


def disk_label(disk: Disk) -> str:
    """Return a disk as shown in the wizard: path, size, filesystem, and mount point (or why it is in use)."""
    filesystem = disk.fstype or "no filesystem"
    use = disk_in_use(disk)
    state = f"in use: {use}" if use else "free"
    model = f", {disk.model}" if disk.model else ""
    return f"{disk.path} ({disk.size_gb:g} GB, {filesystem}, {state}{model})"


def mkfs_command(device: str, home: bool) -> str:
    """Return the command that makes the filesystem nanoHPC needs on an empty disk (with quotas for /home)."""
    return f"sudo mkfs.ext4 -O quota {device}" if home else f"sudo mkfs.ext4 {device}"


def disk_problem(facts: MachineFacts | None, device: str, home: bool) -> str | None:
    """Return what keeps the chosen disk from being ready for /home or scratch (with the command to run for
    an empty disk), or None when it is ready or the machine was not probed. The wizard never formats a disk:
    it only shows the command."""
    if facts is None or facts.error is not None:
        return None
    disk = next((disk for disk in facts.disks if disk.path == device), None)
    if disk is None:
        return f"{device} was not found on {facts.target}"
    use = disk_in_use(disk)
    if use is not None:
        return f"{device} is in use ({use}), so it cannot be used for /home or scratch: choose a spare disk."
    if disk.fstype is None:
        return (
            f"{device} has no filesystem. On {facts.target}, run: {mkfs_command(device, home)}\n"
            "The wizard never formats a disk; run this yourself, or skip and do it later (before the deploy)."
        )
    if disk.fstype not in ("ext4", "xfs"):
        return f"{device} has a {disk.fstype} filesystem: nanoHPC needs ext4 or XFS. Choose another disk."
    return None


def scratch_warning(facts: MachineFacts | None, device: str, cleanup_days: Any, job_retention_days: Any) -> str | None:
    """Return a warning for a scratch disk that already has a filesystem: its old files will be deleted."""
    if facts is None or facts.error is not None:
        return None
    disk = next((disk for disk in facts.disks if disk.path == device), None)
    if disk is None or disk.fstype not in ("ext4", "xfs") or disk_in_use(disk) is not None:
        return None
    days = cleanup_days if cleanup_days is not None else SCRATCH_DEFAULTS["cleanup_days"]
    job_days = job_retention_days if job_retention_days is not None else SCRATCH_DEFAULTS["job_retention_days"]
    return (
        f"{device} already has a filesystem ({disk.fstype}). It becomes /scratch. The daily cleanup removes staged "
        f"data unused for {days} days (scratch.cleanup_days) and kept job copies {job_days} days after their jobs "
        "ended (scratch.job_retention_days). Copy off anything you need first."
    )


@dataclass(frozen=True)
class CheckItem:
    """One item of a machine's preparation checklist. An `info` item is something to know, not to fix."""

    key: str
    label: str
    ok: bool
    hint: str
    info: bool


def checklist(state: WizardState, machine: str) -> list[CheckItem]:
    """Return the preparation checklist of a probed machine (empty when it was not probed): SSH, sudo through
    the administrator's agent, a supported Ubuntu, the NVIDIA driver when GPUs are expected, and the disks for
    /home and scratch."""
    facts = state.facts.get(machine)
    if facts is None:
        return []
    target = state.target(machine)
    items = [
        CheckItem(
            "ssh", "SSH works (host key accepted)", facts.error is None, ssh_hint(facts.error or "", target), False
        )
    ]
    if facts.error is not None:
        return items
    values = state.file.machines().get(machine) or {}
    if not facts.sudo_ok:
        items.append(
            CheckItem(
                "sudo",
                "sudo asks for a password: the first nanohpc deploy asks for it once, in a terminal; after that, "
                "administrators' SSH keys unlock sudo",
                True,
                "",
                True,
            )
        )
    items.append(
        CheckItem(
            "ubuntu",
            "Ubuntu 22.04, 24.04, or 26.04",
            facts.supported,
            f"This machine runs {facts.os_id} {facts.ubuntu}: install Ubuntu 22.04, 24.04, or 26.04.",
            False,
        )
    )
    if values.get("gpu") is not None:
        items.append(
            CheckItem(
                "nvidia",
                "NVIDIA driver works (nvidia-smi lists the GPUs)",
                bool(facts.gpus),
                "Install the NVIDIA driver (for example sudo ubuntu-drivers install), reboot, then probe again (p).",
                False,
            )
        )
    roles = values.get("roles") or []
    home = (values.get("home") or {}).get("device") if "home" in roles else None
    scratch = (values.get("scratch") or {}).get("device") if "compute" in roles else None
    for key, device, is_home in (("home-disk", home, True), ("scratch-disk", scratch, False)):
        if isinstance(device, str):
            problem = disk_problem(facts, device, is_home)
            what = "/home" if is_home else "scratch"
            items.append(CheckItem(key, f"{what} disk {device} is ready", problem is None, problem or "", False))
    return items


def preparation(state: WizardState) -> list[str]:
    """Return what is left to prepare on the machines, for the review: open items, skipped items, and
    machines not probed."""
    lines = []
    for machine in state.file.machines():
        if machine not in state.facts:
            lines.append(f"{machine}: not probed (step 1)")
            continue
        for item in checklist(state, machine):
            if not item.ok:
                done_later = (machine, item.key) in state.skipped
                lines.append(f"{machine}: {item.label}: {'skipped, to do later' if done_later else 'to do'}")
    return lines


def field_text(value: Any) -> str:
    """Return a value from the file as the text of an input field."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def parse_field(text: str, kind: str) -> Any:
    """Return the value to write for an input field's text. `kind`: int (a whole number, or the text as typed
    so validation names it), list (comma-separated), or any other kind for text."""
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
    """Write an input field's text to the file and return whether the file changed (text equal to what the
    file has changes nothing). Empty text: a `required` setting is written empty (it keeps its place, and
    validation names it), an `optional` one becomes null (it keeps its place), any other is removed so
    nanoHPC's default applies."""
    current = file.get(path)
    if text == field_text(current):
        return False
    if current == "":
        file.set_value(path, None)  # so the new text is not written in the quotes of the empty value
    if text == "" and kind == "required":
        file.set_value(path, "")
    elif text == "" and kind == "optional":
        file.set_value(path, None)
    elif text == "":
        remove_value(file, path)
    else:
        file.set_value(path, parse_field(text, kind))
    return True


def save_text(path: Path, text: str) -> None:
    """Write the file safely: through a symlink to its target, keeping the target's permissions (0644 for a
    new file), first to a temporary file next to it and then renamed over it."""
    target = path.resolve()
    mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text)
    os.chmod(temporary, mode)
    os.replace(temporary, target)


def uid_conflicts(state: WizardState) -> list[tuple[str, str, tuple[int, int]]]:
    """Return (machine, user, (UID, GID) there) for each checked user whose UID or GID on a machine differs
    from the UID in cluster.yml. A missing account is not a conflict: the deploy creates it."""
    expected = {user["name"]: user["uid"] for user in state.file.users() if "name" in user and "uid" in user}
    conflicts = []
    for machine, check in state.uid_checks.items():
        for user, ids in check.ids.items():
            if ids is not None and user in expected and ids != (expected[user], expected[user]):
                conflicts.append((machine, user, ids))
    return conflicts
