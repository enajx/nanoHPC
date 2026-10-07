"""Report waiting Ubuntu package updates on every configured machine without changing APT state."""

import json
import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanohpc.config import load_config
from nanohpc.probe import run_remote, ssh_failure

TIMEOUT = 120
CARE_GROUPS: dict[str, tuple[str, ...]] = {
    "kernel": (
        "linux-generic",
        "linux-image-",
        "linux-headers-",
        "linux-modules-",
        "linux-tools-",
        "linux-cloud-tools-",
    ),
    "grub": ("grub", "shim"),
    "nvidia": (
        "nvidia-",
        "libnvidia-",
        "linux-modules-nvidia-",
        "linux-objects-nvidia-",
        "linux-signatures-nvidia-",
        "xserver-xorg-video-nvidia-",
        "cuda-",
    ),
    "ssh": ("openssh-", "ssh$"),
    "network": (
        "network-manager",
        "libnm",
        "gir1.2-nm",
        "netplan",
        "libnetplan",
        "python3-netplan",
        "ifupdown",
        "isc-dhcp-",
    ),
    "slurm": ("slurm", "munge", "libmunge"),
    "docker": ("docker", "containerd", "runc"),
    "mariadb": ("mariadb", "libmariadb", "galera"),
}
OTHER_GROUPS = ("extra", "security", "rest")

# The system Python has python3-apt on supported Ubuntu installs. Reading apt.Cache and list file timestamps
# does not refresh package lists, acquire an install lock, or write to the target machine.
PROGRAM = r"""
import apt, json
from pathlib import Path

lists = list(Path('/var/lib/apt/lists').glob('*_Packages*'))
if not lists:
    raise RuntimeError('no saved APT package lists; run apt-get update on this machine first')
packages = []
for package in apt.Cache():
    if not package.is_upgradable:
        continue
    origins = package.candidate.origins
    packages.append({'name': package.name,
                     'ubuntu': any(origin.origin.startswith('Ubuntu') for origin in origins),
                     'security': any(origin.origin.startswith('Ubuntu') and origin.archive.endswith('-security')
                                     for origin in origins)})
print(json.dumps({'list_time': max(path.stat().st_mtime for path in lists), 'packages': packages}))
"""


def group_of(package: dict[str, Any]) -> str:
    """Give a waiting package one care group, otherwise classify by source and security pocket."""
    name = package["name"]
    for group, patterns in CARE_GROUPS.items():
        if any(re.match(pattern, name) for pattern in patterns):
            return group
    if not package["ubuntu"]:
        return "extra"
    return "security" if package["security"] else "rest"


def groups_of(packages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Put each upgradable package in exactly one ordered group."""
    groups: dict[str, list[str]] = {name: [] for name in (*CARE_GROUPS, *OTHER_GROUPS)}
    for package in packages:
        groups[group_of(package)].append(package["name"])
    for names in groups.values():
        names.sort()
    return groups


def read_machine(name: str, ssh_config: Path | None) -> tuple[str, dict[str, Any] | None, str | None]:
    """Read one machine's saved APT state over SSH, returning an error for that machine on failure."""
    result = run_remote(name, ssh_config, f"/usr/bin/python3 -c {shlex.quote(PROGRAM)}", False, TIMEOUT)
    failure = ssh_failure(name, result)
    if failure is not None:
        return name, None, failure
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        return name, None, detail[-1] if detail else f"remote command exited {result.returncode} without an error"
    try:
        data = json.loads(result.stdout)
        if not isinstance(data["list_time"], (float, int)) or not isinstance(data["packages"], list):
            raise TypeError("invalid APT report fields")
        for package in data["packages"]:
            if (
                not isinstance(package["name"], str)
                or not isinstance(package["ubuntu"], bool)
                or not isinstance(package["security"], bool)
            ):
                raise TypeError("invalid package entry")
    except (ValueError, KeyError, TypeError) as error:
        return name, None, f"invalid APT report: {error}"
    return name, data, None


def format_machine(name: str, data: dict[str, Any]) -> str:
    """Render the count, package-list timestamp and age, then each package group."""
    packages = data["packages"]
    timestamp = datetime.fromtimestamp(data["list_time"], UTC)
    age_hours = max(0, int((datetime.now(UTC) - timestamp).total_seconds() // 3600))
    age = f"{age_hours // 24}d {age_hours % 24}h"
    security = sum(package["security"] for package in packages)
    header = f"{name}: {len(packages)} updates waiting, {security} security "
    header += f"(package lists from {timestamp:%Y-%m-%d %H:%M} UTC, age {age})"
    lines = [header]
    for group, names in groups_of(packages).items():
        lines.append(f"  {group} ({len(names)}): {' '.join(names)}".rstrip())
    return "\n".join(lines)


def run(path: Path, ssh_config: Path | None) -> int:
    """Validate the cluster and print read-only APT reports for every machine."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, True, False)
    if errors:
        print(f"{path}: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    names = list(config["machines"])
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as executor:
        results = list(executor.map(lambda name: read_machine(name, ssh_config), names))
    failed = False
    for name, data, error in results:
        if error is not None:
            print(f"{name}: FAILED: {error}")
            failed = True
        else:
            assert data is not None
            print(format_machine(name, data))
    return 1 if failed else 0
