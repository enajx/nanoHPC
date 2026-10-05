"""Safely install the approved updates on one confirmed compute machine."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from nanohpc import restart, restart_check, ssh_update
from nanohpc.config import load_config
from nanohpc.probe import run_remote, ssh_failure
from nanohpc.render import front_machine, gpu_count
from nanohpc.update_report import CARE_GROUPS, group_of

SNAPSHOT_PROGRAM = r"""
# nanohpc-update-snapshot
import apt, hashlib, json
from pathlib import Path

root = Path('/var/lib/apt/lists')
files = sorted(path for path in root.iterdir() if path.is_file() and path.name != 'lock')
if not any('_Packages' in path.name for path in files):
    raise RuntimeError('no saved APT package lists after apt-get update')
digest = hashlib.sha256()
for path in files:
    digest.update(path.name.encode() + b'\0')
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
packages = []
for package in apt.Cache():
    if not package.is_upgradable:
        continue
    origins = package.candidate.origins
    packages.append({'name': package.name, 'version': package.candidate.version,
                     'ubuntu': any(origin.origin.startswith('Ubuntu') for origin in origins),
                     'security': any(origin.origin.startswith('Ubuntu') and origin.archive.endswith('-security')
                                     for origin in origins)})
print(json.dumps({'lists_hash': digest.hexdigest(),
                  'dpkg_hash': hashlib.sha256(Path('/var/lib/dpkg/status').read_bytes()).hexdigest(),
                  'packages': packages}))
"""


class UpdateError(RuntimeError):
    """An update cannot continue safely."""


def state_path(path: Path, machine: str) -> Path:
    """Keep one local preview per cluster file and machine outside the repository."""
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    key = hashlib.sha256(f"{path.resolve()}\0{machine}".encode()).hexdigest()[:24]
    return root / "nanohpc" / "updates" / f"{key}.json"


def read_snapshot(machine: str, ssh_config: Path | None) -> dict[str, Any]:
    """Read exact candidate versions, package-list content, and installed-package state."""
    command = f"/usr/bin/python3 -c {shlex.quote(SNAPSHOT_PROGRAM)}"
    result = run_remote(machine, ssh_config, command, False, 180)
    failure = ssh_failure(machine, result)
    if failure is not None:
        raise UpdateError(failure)
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise UpdateError(f"{machine}: APT snapshot failed: {detail[-1] if detail else result.returncode}")
    try:
        snapshot = json.loads(result.stdout)
        if not isinstance(snapshot["lists_hash"], str) or not isinstance(snapshot["dpkg_hash"], str):
            raise TypeError("invalid state hashes")
        if not isinstance(snapshot["packages"], list):
            raise TypeError("invalid package list")
        for package in snapshot["packages"]:
            if (
                not isinstance(package["name"], str)
                or not isinstance(package["version"], str)
                or not isinstance(package["ubuntu"], bool)
                or not isinstance(package["security"], bool)
            ):
                raise TypeError("invalid candidate package")
    except (ValueError, KeyError, TypeError) as error:
        raise UpdateError(f"{machine}: invalid APT snapshot: {error}") from error
    return snapshot


def package_plan(snapshot: dict[str, Any], include: list[str]) -> list[list[str]]:
    """Select ordinary Ubuntu updates and the explicitly named care and extra groups."""
    selected = [
        [package["name"], package["version"]]
        for package in snapshot["packages"]
        if group_of(package) in ("security", "rest", *include)
    ]
    return sorted(selected)


def simulation(machine: str, ssh_config: Path | None, packages: list[list[str]], include: list[str]) -> str:
    """Run APT's solver without installing anything."""
    if not packages:
        return ""
    arguments = ["apt-get", "-s", "install", "--no-remove"]
    if "kernel" not in include:
        arguments.append("--only-upgrade")
    arguments.extend(f"{name}={version}" for name, version in packages)
    return restart.read(machine, ssh_config, "LC_ALL=C " + shlex.join(arguments), 180)


def simulation_problems(
    output: str, snapshot: dict[str, Any], include: list[str], packages: list[list[str]]
) -> tuple[list[str], list[list[str]]]:
    """Refuse removals, unexpected installs, unapproved groups and package-version drift."""
    candidates = {package["name"]: package for package in snapshot["packages"]}
    planned = {name: version for name, version in packages}
    installed: set[str] = set()
    resolved: list[list[str]] = []
    problems: list[str] = []
    for line in output.splitlines():
        if line.startswith("Remv "):
            problems.append(f"APT would remove {line.split()[1]}")
        if not line.startswith("Inst "):
            continue
        match = re.match(r"^Inst (\S+)( \[[^]]+\])? \((\S+) (\S+)", line)
        if match is None:
            problems.append(f"cannot verify simulated install: {line}")
            continue
        name, old_version, version, origin = match.groups()
        installed.add(name)
        resolved.append([name, version])
        candidate = candidates.get(name)
        if candidate is not None and version != candidate["version"]:
            problems.append(f"unexpected package or version in simulation: {name}={version}")
            continue
        group = group_of(candidate or {"name": name, "ubuntu": origin.startswith("Ubuntu"), "security": False})
        if candidate is None and (old_version is not None or "kernel" not in include or group != "kernel"):
            problems.append(f"APT pulled in unplanned package {name}={version}")
        if old_version is None and ("kernel" not in include or group != "kernel" or not origin.startswith("Ubuntu")):
            problems.append(f"APT would install a new unapproved package {name}={version}")
        if group not in ("security", "rest", *include):
            problems.append(f"APT would install {name} from unnamed group {group}")
        if not origin.startswith("Ubuntu") and "extra" not in include:
            problems.append(f"APT would install {name} from extra source {origin}")
    for name in planned.keys() - installed:
        problems.append(f"APT did not plan the approved package {name}")
    return problems, sorted(resolved)


def save_preview(path: Path, preview: dict[str, Any]) -> None:
    """Write a private local preview atomically for the later real run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(preview, file, sort_keys=True)
        file.write("\n")
    os.replace(temporary, path)


def preview(config: dict[str, Any], path: Path, machine: str, include: list[str], ssh_config: Path | None) -> int:
    """Refresh APT lists, simulate exact upgrades, then save the reviewed plan."""
    restart.verify_address(config, machine, ssh_config)
    restart.invoking_admin(config, machine, ssh_config)
    ssh_update.require_clear(machine, ssh_config)
    print(f"{machine}: refreshing APT lists", flush=True)
    restart.root(machine, ssh_config, ["apt-get", "update", "-o", "APT::Update::Error-Mode=any"], 15 * 60)
    snapshot = read_snapshot(machine, ssh_config)
    packages = package_plan(snapshot, include)
    simulated = simulation(machine, ssh_config, packages, include)
    problems, resolved = simulation_problems(simulated, snapshot, include, packages)
    if problems:
        raise UpdateError(f"{machine}: unsafe APT plan: " + "; ".join(problems))
    record = {
        "machine": machine,
        "include": include,
        "packages": packages,
        "resolved": resolved,
        "lists_hash": snapshot["lists_hash"],
        "dpkg_hash": snapshot["dpkg_hash"],
        "config_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "simulation_hash": hashlib.sha256(simulated.encode()).hexdigest(),
    }
    save_preview(state_path(path, machine), record)
    print(f"{machine}: dry run passed, {len(packages)} packages approved")
    for name, version in packages:
        print(f"  {name}={version}")
    return 0


def approved_preview(
    config: dict[str, Any], path: Path, machine: str, include: list[str], ssh_config: Path | None
) -> dict[str, Any]:
    """Require the same machine, scope, lists, installed state and APT solver result as the dry run."""
    record_path = state_path(path, machine)
    if not record_path.is_file():
        raise UpdateError(f"{machine}: no matching dry run; run `nanohpc update {path} {machine} --dry-run` first")
    try:
        record = json.loads(record_path.read_text())
    except json.JSONDecodeError as error:
        raise UpdateError(f"{machine}: dry run record is invalid: {error}") from error
    if record.get("machine") != machine or record.get("include") != include:
        raise UpdateError(f"{machine}: dry run has a different machine or included groups; run it again")
    if record.get("config_hash") != hashlib.sha256(path.read_bytes()).hexdigest():
        raise UpdateError(f"{machine}: cluster.yml changed since the dry run; run it again")
    restart.verify_address(config, machine, ssh_config)
    restart.invoking_admin(config, machine, ssh_config)
    ssh_update.require_clear(machine, ssh_config)
    snapshot = read_snapshot(machine, ssh_config)
    packages = package_plan(snapshot, include)
    if (
        record.get("lists_hash") != snapshot["lists_hash"]
        or record.get("dpkg_hash") != snapshot["dpkg_hash"]
        or record.get("packages") != packages
    ):
        raise UpdateError(f"{machine}: APT lists or package versions changed since the dry run; run it again")
    simulated = simulation(machine, ssh_config, packages, include)
    problems, resolved = simulation_problems(simulated, snapshot, include, packages)
    if problems:
        raise UpdateError(f"{machine}: unsafe APT plan: " + "; ".join(problems))
    if record.get("simulation_hash") != hashlib.sha256(simulated.encode()).hexdigest():
        raise UpdateError(f"{machine}: APT's package plan changed since the dry run; run it again")
    if record.get("resolved") != resolved:
        raise UpdateError(f"{machine}: APT's resolved package versions changed since the dry run; run it again")
    return record


def failed_units(machine: str, ssh_config: Path | None) -> set[str]:
    """Read the names of failed systemd units for comparison after the install."""
    output = restart.root(machine, ssh_config, ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], 30)
    units: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if not fields or line.endswith("loaded units listed."):
            continue
        name = fields[1] if fields[0] == "●" and len(fields) > 1 else fields[0].lstrip("●")
        if "." not in name:
            raise UpdateError(f"{machine}: cannot read failed systemd unit: {line}")
        units.add(name)
    return units


def install(machine: str, ssh_config: Path | None, packages: list[list[str]], include: list[str]) -> None:
    """Install only the exact approved versions, keeping existing settings files."""
    arguments = [
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "NEEDRESTART_MODE=l",
        "apt-get",
        "install",
        "-y",
        "--no-remove",
        "-o",
        "Dpkg::Options::=--force-confold",
    ]
    if "kernel" not in include:
        arguments.append("--only-upgrade")
    arguments.extend(f"{name}={version}" for name, version in packages)
    restart.root(machine, ssh_config, arguments, 2 * 60 * 60)


def post_checks(
    config: dict[str, Any], machine: str, admin: str, ssh_config: Path | None, before: set[str], ssh_guard: bool
) -> bool:
    """Require fresh access, saved restart safety, GPUs, Slurm, and no newly failed units."""
    restart.verify_address(config, machine, ssh_config)
    restart.root(machine, ssh_config, ["sshd", "-t"], 30)
    restart.fresh_login(machine, "root", ssh_config)
    restart.fresh_login(machine, admin, ssh_config)
    if ssh_guard:
        ssh_update.cancel(machine, ssh_config)
    facts, error = restart_check.read_machine(machine, ssh_config)
    if error or facts is None:
        raise UpdateError(f"{machine}: saved restart checks could not run: {error or 'no facts returned'}")
    expected_gpus = gpu_count(config["machines"][machine])
    findings = restart_check.evaluate(facts, expected_gpus > 0)
    problems = [f"{label}: {reason}" for label, reasons in findings.items() for reason in reasons]
    if problems:
        raise UpdateError(f"{machine}: saved restart checks failed: " + "; ".join(problems))
    if expected_gpus:
        kernels = facts.get("kernels") or []
        modules = facts.get("module") or {}
        missing = [kernel for kernel in kernels if not modules.get(kernel)]
        if not kernels or missing:
            raise UpdateError(
                f"{machine}: missing NVIDIA modules for installed kernels: {', '.join(missing) or 'none found'}"
            )
        gpu_lines = restart.read(machine, ssh_config, "nvidia-smi -L", 30).splitlines()
        found = [line for line in gpu_lines if line.startswith("GPU ")]
        if len(found) != expected_gpus:
            raise UpdateError(f"{machine}: nvidia-smi saw {len(found)} GPUs, cluster.yml expects {expected_gpus}")
    if restart.read(machine, ssh_config, "systemctl is-active slurmd", 30) != "active":
        raise UpdateError(f"{machine}: slurmd is not active")
    new_failed = failed_units(machine, ssh_config) - before
    if new_failed:
        raise UpdateError(f"{machine}: new failed systemd units: {', '.join(sorted(new_failed))}")
    restart_status = restart.read(
        machine,
        ssh_config,
        "if test -e /run/reboot-required; then echo required; cat /run/reboot-required.pkgs 2>/dev/null || true; else echo none; fi",
        30,
    )
    return restart_status.splitlines()[0] == "required"


def apply(config: dict[str, Any], path: Path, machine: str, include: list[str], ssh_config: Path | None) -> int:
    """Drain, wait, install the matching approved plan, check it, then resume or keep drained for restart."""
    record = approved_preview(config, path, machine, include, ssh_config)
    packages = record["resolved"]
    if not packages:
        state_path(path, machine).unlink()
        print(f"{machine}: no approved packages to install")
        return 0
    print(f"{machine}: approved updates: " + " ".join(f"{name}={version}" for name, version in packages), flush=True)
    front = front_machine(config)[0]
    restart.verify_address(config, front, ssh_config)
    admin = restart.invoking_admin(config, machine, ssh_config)
    ssh_guard = "ssh" in include and any(
        any(re.match(pattern, name) for pattern in CARE_GROUPS["ssh"]) for name, _ in packages
    )
    restart.lock(front, ssh_config)
    locked = True
    drained = False
    success = False
    error: str | None = None
    holds: list[subprocess.Popen[str]] = []
    ssh_canceled = False
    needs_restart = False
    try:
        state = restart.node_state(front, machine, ssh_config).upper()
        if any(flag in state for flag in ("DRAIN", "DOWN", "FAIL", "MAINT")):
            raise UpdateError(f"{machine}: Slurm state is {state}; resolve that state before an update")
        restart.drain(front, machine, ssh_config, "update by nanohpc")
        drained = True
        restart.wait_for_jobs(front, machine, ssh_config)
        approved_preview(config, path, machine, include, ssh_config)
        before = failed_units(machine, ssh_config)
        if ssh_guard:
            holds.append(ssh_update.hold_root(machine, ssh_config))
            holds.append(ssh_update.hold_root(machine, ssh_config))
            ssh_update.require_holds(machine, holds)
            ssh_update.prepare(machine, ssh_config)
            ssh_update.require_holds(machine, holds)
        state_path(path, machine).unlink()
        install(machine, ssh_config, packages, include)
        if ssh_guard:
            ssh_update.require_holds(machine, holds)
            restart.root(machine, ssh_config, ["systemctl", "restart", "ssh.service"], 30)
            ssh_update.listener(machine, ssh_config)
        needs_restart = post_checks(config, machine, admin, ssh_config, before, ssh_guard)
        if ssh_guard:
            ssh_canceled = True
            ssh_update.close_holds(holds)
        if needs_restart:
            restart.drain(front, machine, ssh_config, restart.UPDATE_RESTART_REASON)
        success = True
    except (UpdateError, restart.RestartError, subprocess.TimeoutExpired) as failure:
        error = str(failure)
    finally:
        if holds and not ssh_canceled:
            print(f"{machine}: SSH undo remains armed; held root connections will close after 4 hours", file=sys.stderr)
        if locked:
            try:
                restart.unlock(front, ssh_config)
            except restart.RestartError as failure:
                success = False
                error = f"{error or 'update stopped'}; could not remove update lock: {failure}"
        if drained and not success:
            try:
                restart.drain(front, machine, ssh_config, "update by nanohpc failed; check by hand")
            except restart.RestartError as failure:
                error = f"{error or 'update stopped'}; could not leave {machine} drained: {failure}"
    if error:
        print(f"{machine}: {error}", file=sys.stderr)
        return 1
    if needs_restart:
        print(
            f"{machine}: update passed; restart required. Node stays drained; run `nanohpc restart {path} {machine} --confirm {machine}`"
        )
        return 0
    try:
        restart.resume(front, machine, ssh_config)
    except restart.RestartError as failure:
        error = str(failure)
        try:
            restart.drain(front, machine, ssh_config, "update by nanohpc failed; check by hand")
        except restart.RestartError as drain_failure:
            error += f"; could not leave {machine} drained: {drain_failure}"
        print(f"{machine}: {error}", file=sys.stderr)
        return 1
    print(f"{machine}: update passed; node resumed")
    return 0


def valid_target(config: dict[str, Any], machine: str, confirm: str | None, dry_run: bool) -> str | None:
    """Return why a request cannot update this one compute machine."""
    if config["cluster"].get("mode") == "monitor":
        return "update needs a Slurm cluster, not monitor mode"
    selected = config["machines"].get(machine)
    if selected is None:
        return f"{machine} is not in cluster.yml"
    if "front" in selected["roles"] or "compute" not in selected["roles"]:
        return f"{machine} is not one compute machine; the front node needs its own update procedure"
    if not dry_run and confirm != machine:
        return f"--confirm must equal the one machine name {machine}"
    return None


def parse_include(value: str) -> tuple[list[str], str | None]:
    """Return the explicitly named care groups and extra sources, or a usage error."""
    groups = [group.strip() for group in value.split(",") if group.strip()]
    known = {*CARE_GROUPS, "extra"}
    unknown = [group for group in groups if group not in known]
    if unknown:
        return [], f"unknown update group: {', '.join(unknown)}; known: {', '.join(sorted(known))}"
    if len(groups) != len(set(groups)):
        return [], "--include has duplicate groups"
    return sorted(groups), None


def run(path: Path, machine: str, confirm: str | None, dry_run: bool, include: str, ssh_config: Path | None) -> int:
    """Validate the request before preparing or installing updates on a compute machine."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, False, False)
    if errors:
        print(f"{path}: {len(errors)} error(s); no machine was updated", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    problem = valid_target(config, machine, confirm, dry_run)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    groups, problem = parse_include(include)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    try:
        if dry_run:
            return preview(config, path, machine, groups, ssh_config)
        return apply(config, path, machine, groups, ssh_config)
    except (UpdateError, restart.RestartError) as failure:
        print(f"{machine}: {failure}", file=sys.stderr)
        return 1
