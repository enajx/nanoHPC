"""Read-only checks of saved boot settings on every machine before a restart."""

import fnmatch
import json
import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from nanohpc.check import Report, listing, table
from nanohpc.config import load_config
from nanohpc.probe import run_remote, ssh_failure
from nanohpc.render import gpu_count

COLUMNS = ("machine", "ssh", "fstab", "grub", "nvidia", "network", "restarts")
TIMEOUT = 120

# The whole collector runs as root to read grub.cfg and saved NetworkManager profiles. It only reads files and
# runs read-only commands. Each command has a short time limit, so one stalled tool cannot hold the SSH call.
PROGRAM = r"""
import json, os, pathlib, shutil, subprocess, time

def command(*args):
    if shutil.which(args[0]) is None:
        return {"code": 127, "out": "", "err": args[0] + " is not installed"}
    result = subprocess.run(["timeout", "15", *args], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C"))
    return {"code": result.returncode, "out": result.stdout, "err": result.stderr}

def read(path):
    file = pathlib.Path(path)
    return file.read_text() if file.is_file() else None

def output(*args):
    result = command(*args)
    return result["out"] if result["code"] == 0 else None

def profile(path):
    sections = {}
    section = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section in ("connection", "ipv4"):
                sections[section] = {}
        elif "=" in line and section in sections:
            key, value = line.split("=", 1)
            key = key.strip()
            if key in ("uuid", "autoconnect", "never-default", "method", "gateway") or key.startswith("address"):
                sections[section][key] = value.strip()
    return sections

defaults = [pathlib.Path("/etc/default/grub"), *sorted(pathlib.Path("/etc/default/grub.d").glob("*.cfg"))]
saved = [*pathlib.Path("/etc/netplan").glob("*.yaml"),
         *pathlib.Path("/lib/netplan").glob("*.yaml"),
         *pathlib.Path("/run/netplan").glob("*.yaml"),
         *pathlib.Path("/etc/NetworkManager/system-connections").glob("*")]
boot_time = time.time() - float(pathlib.Path("/proc/uptime").read_text().split()[0])
route = output("ip", "-4", "-j", "route", "show", "default")
addresses = output("ip", "-4", "-j", "addr", "show")
device = ""
if route:
    routes = json.loads(route)
    if len(routes) == 1:
        device = routes[0].get("dev", "")
nm = {}
if device:
    uuid = output("nmcli", "-g", "GENERAL.CON-UUID", "device", "show", device)
    if uuid and uuid.strip() and uuid.strip() != "--":
        uuid = uuid.strip()
        nm = {"uuid": uuid,
              "files": {str(path): profile(path) for path in saved if path.is_file() and "NetworkManager" in str(path)}}
kernel_names = [path.name.removeprefix("vmlinuz-") for path in pathlib.Path("/boot").glob("vmlinuz-*")]
module = {name: command("modinfo", "-k", name, "nvidia")["code"] == 0 for name in kernel_names}
facts = {"sudo": True, "fstab": read("/etc/fstab"),
         "boot_mounted": command("findmnt", "--mountpoint", "/boot")["code"] == 0,
         "grub_defaults": "\n".join(path.read_text() for path in defaults if path.is_file()),
         "grub_cfg": read("/boot/grub/grub.cfg"), "grubenv": read("/boot/grub/grubenv") or "",
         "kernels": kernel_names, "module": module,
         "driver_loaded": pathlib.Path("/sys/module/nvidia").exists(),
         "routes": json.loads(route) if route is not None else None,
         "addresses": json.loads(addresses) if addresses is not None else None,
         "netplan": output("netplan", "get"),
         "netplan_files": [str(path) for path in saved if "netplan" in str(path)],
         "volatile_netplan": [str(path) for path in saved if str(path).startswith("/run/netplan/")],
         "changed_files": [str(path) for path in saved if path.is_file() and path.stat().st_mtime > boot_time],
         "nm": nm, "apt": output("apt-config", "dump", "Unattended-Upgrade::Automatic-Reboot")}
print(json.dumps(facts))
"""


def fstab_problems(facts: dict[str, Any]) -> list[str]:
    """Find mounts that can block boot, including a missing separate /boot mount."""
    source = facts.get("fstab")
    if source is None:
        return ["cannot read /etc/fstab"]
    problems: list[str] = []
    separate_boot = False
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 4:
            problems.append(f"/etc/fstab line {number} is incomplete")
            continue
        mount, kind, options = fields[1], fields[2], fields[3].split(",")
        if mount == "/boot":
            separate_boot = True
        if mount in ("/", "/boot/efi", "none") or kind == "swap" or ("noauto" in options and mount != "/boot"):
            continue
        if "nofail" not in options:
            problems.append(f"{mount} has no nofail")
    if separate_boot and not facts.get("boot_mounted"):
        problems.append("/boot is not mounted")
    return problems


def grub_kernel(facts: dict[str, Any]) -> tuple[str, list[str]]:
    """Find the kernel selected by an ordinary GRUB default of zero."""
    defaults = facts.get("grub_defaults")
    config = facts.get("grub_cfg")
    if not defaults or not config:
        return "", ["cannot read GRUB's saved settings and menu"]
    values = re.findall(r"^\s*GRUB_DEFAULT=(.*)$", defaults, re.MULTILINE)
    selected = values[-1].strip().strip("'\"") if values else "0"
    if selected != "0":
        return "", [f"GRUB_DEFAULT is {selected}; selected kernel is unknown"]
    if re.search(r"^next_entry=.+$", facts.get("grubenv") or "", re.MULTILINE):
        return "", ["GRUB has a one-time next entry"]
    first = re.search(r"^\s*linux(?:efi)?\s+/(?:boot/)?vmlinuz-(\S+)", config, re.MULTILINE)
    if first is None:
        return "", ["no kernel found in GRUB's first entry"]
    kernel = first[1]
    if kernel not in (facts.get("kernels") or []):
        return kernel, [f"/boot/vmlinuz-{kernel} is missing"]
    return kernel, []


def active_network(facts: dict[str, Any]) -> tuple[str, str, str, bool, list[str]]:
    """Find one active IPv4 default route and its matching interface address."""
    routes = facts.get("routes")
    addresses = facts.get("addresses")
    if not isinstance(routes, list) or len(routes) != 1 or not isinstance(addresses, list):
        return "", "", "", False, ["cannot identify one active IPv4 default route and address"]
    route = routes[0]
    device = route.get("dev", "")
    gateway = route.get("gateway", "")
    interfaces = [item for item in addresses if item.get("ifname") == device]
    ips = [entry for item in interfaces for entry in item.get("addr_info", []) if entry.get("family") == "inet"]
    source = route.get("prefsrc") or route.get("src")
    if source:
        ips = [entry for entry in ips if entry.get("local") == source]
    if not device or not gateway or len(ips) != 1:
        return "", "", "", False, ["cannot identify one active IPv4 default route and address"]
    ip = ips[0]
    return device, f"{ip['local']}/{ip['prefixlen']}", gateway, bool(ip.get("dynamic")), []


def netplan_matches(facts: dict[str, Any], device: str, address: str, gateway: str, dynamic: bool) -> bool:
    """Check the merged saved Netplan settings of the default-route device."""
    if not facts.get("netplan_files") or not facts.get("netplan"):
        return False
    try:
        saved = yaml.safe_load(facts["netplan"]) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(saved, dict):
        return False
    network = saved.get("network") or {}
    if not isinstance(network, dict):
        return False
    for kind in ("ethernets", "bonds", "bridges", "vlans"):
        for identifier, entry in (network.get(kind) or {}).items():
            match = entry.get("match") or {}
            if match:
                interfaces = [item for item in facts["addresses"] if item.get("ifname") == device]
                mac = interfaces[0].get("address", "").lower() if len(interfaces) == 1 else ""
                matched = (
                    set(match) <= {"name", "macaddress"}
                    and (not entry.get("set-name") or entry["set-name"] == device)
                    and ("name" not in match or fnmatch.fnmatchcase(device, match["name"]))
                    and ("macaddress" not in match or bool(mac) and mac == match["macaddress"].lower())
                )
            else:
                matched = device == identifier
            if not matched:
                continue
            routes = [
                item.get("via") for item in entry.get("routes") or [] if item.get("to") in ("default", "0.0.0.0/0")
            ]
            route = facts["routes"][0]
            dhcp_route = route.get("protocol") in ("dhcp", "boot")
            if (
                entry.get("dhcp4") is True
                and dynamic
                and dhcp_route
                and entry.get("dhcp4-overrides", {}).get("use-routes", True)
            ):
                return True
            values = [item if isinstance(item, str) else next(iter(item)) for item in entry.get("addresses") or []]
            if address in values and gateway in [entry.get("gateway4"), *routes]:
                return True
    return False


def nm_matches(facts: dict[str, Any], address: str, gateway: str, dynamic: bool) -> bool:
    """Check that the active NetworkManager UUID has a persistent, automatic profile."""
    nm = facts.get("nm") or {}
    files = nm.get("files") or {}
    for path, saved in files.items():
        if not path.startswith("/etc/NetworkManager/system-connections/"):
            continue
        connection = saved.get("connection") or {}
        ipv4 = saved.get("ipv4") or {}
        if connection.get("uuid") != nm.get("uuid") or connection.get("autoconnect") not in ("true", "yes", "1"):
            continue
        if ipv4.get("never-default") in ("true", "yes", "1"):
            continue
        if ipv4.get("method") == "auto":
            route = facts["routes"][0]
            if dynamic and route.get("protocol") in ("dhcp", "boot"):
                return True
        if ipv4.get("method") == "manual":
            for key, value in ipv4.items():
                if key.startswith("address"):
                    parts = [part.strip() for part in value.split(",")]
                    if parts[0] == address and gateway in (ipv4.get("gateway"), *parts[1:]):
                        return True
    return False


def network_problems(facts: dict[str, Any]) -> list[str]:
    """Compare saved and live network settings; fail when either side cannot be verified."""
    device, address, gateway, dynamic, problems = active_network(facts)
    problems += [f"{path} changed since the last boot" for path in facts.get("changed_files") or []]
    problems += [f"{path} is a temporary Netplan setting" for path in facts.get("volatile_netplan") or []]
    if device and not (
        netplan_matches(facts, device, address, gateway, dynamic) or nm_matches(facts, address, gateway, dynamic)
    ):
        problems.append(f"no saved Netplan or persistent NetworkManager setting gives {device} {address} via {gateway}")
    return problems


def evaluate(facts: dict[str, Any], gpu: bool) -> dict[str, list[str]]:
    """Return each restart check's reasons for failure, empty when it passes."""
    kernel, grub = grub_kernel(facts)
    if gpu or facts.get("driver_loaded"):
        module = facts.get("module") or {}
        nvidia = (
            [f"no NVIDIA module built for {kernel}"]
            if kernel and not module.get(kernel)
            else ([] if kernel else ["GRUB's next kernel is unknown"])
        )
    else:
        nvidia = []
    apt = facts.get("apt")
    values = re.findall(r'Unattended-Upgrade::Automatic-Reboot\s+"([^"]+)"', apt or "", re.IGNORECASE)
    restarts = ["cannot verify automatic update restart setting"] if not values else []
    if values and values[-1].lower() in ("true", "yes", "1"):
        restarts.append("automatic updates can restart the machine")
    elif values and values[-1].lower() not in ("false", "no", "0"):
        restarts.append(f"unknown automatic update restart value: {values[-1]}")
    return {
        "fstab": fstab_problems(facts),
        "grub": grub,
        "nvidia": nvidia,
        "network": network_problems(facts),
        "restarts": restarts,
    }


def read_machine(name: str, ssh_config: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """Collect one machine's boot facts over SSH with read-only root access."""
    command = f"sudo -S -p '' python3 -c {shlex.quote(PROGRAM)} </dev/null"
    result = run_remote(name, ssh_config, command, True, TIMEOUT)
    failure = ssh_failure(name, result)
    if failure:
        return None, failure
    if result.returncode:
        return (
            None,
            f"restart check could not run: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'no output'}",
        )
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, "restart check returned invalid machine facts"


def report(name: str, facts: dict[str, Any] | None, error: str | None, gpu: bool) -> Report:
    """Build one table row and its failure reasons."""
    cells = {column: "?" for column in COLUMNS}
    cells["machine"] = name
    problems: list[str] = []
    if error or facts is None:
        cells["ssh"] = "FAIL"
        problems.append(error or "no facts returned")
    else:
        cells["ssh"] = "ok"
        try:
            findings = evaluate(facts, gpu)
        except (AttributeError, KeyError, TypeError, ValueError) as failure:
            problems.append(f"cannot interpret restart facts: {failure}")
        else:
            for label, reasons in findings.items():
                cells[label] = "FAIL" if reasons else "ok"
                problems.extend(f"{label}: {reason}" for reason in reasons)
    return Report(cells, problems, [], [])


def run(path: Path, ssh_config: Path | None) -> int:
    """Validate cluster.yml, check every machine, print results, and fail on any unknown or unsafe setting."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, False, False)
    if errors:
        print(f"{path}: {len(errors)} error(s); no machine was checked", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    names = list(config["machines"])
    with ThreadPoolExecutor(max_workers=32) as pool:
        found = dict(zip(names, pool.map(lambda name: read_machine(name, ssh_config), names), strict=True))
    reports = [report(name, *found[name], gpu_count(config["machines"][name]) > 0) for name in names]
    reports.sort(key=lambda item: not item.problems)
    problems = [(item.cells["machine"], problem) for item in reports for problem in item.problems]
    print(table(reports, COLUMNS) + listing("Problems", problems) + ("" if problems else "\n\nNo problems."))
    return 1 if problems else 0
