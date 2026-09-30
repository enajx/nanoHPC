"""Simulated test cluster: Lima VMs made from a cluster.yml and a test-only sim file.

`nanohpc sim up SIM` starts one VM per machine on Lima's user-v2 network and writes, under
`.nanohpc-sim/<name>/`: the cluster.yml with the VMs' real addresses, an SSH config that
reaches every machine by its name, and the list of machines with fake GPUs.
`nanohpc sim down SIM` deletes the VMs, their disks, and that folder. See md/testing.md.
"""

import getpass
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import Checker, UniqueKeyLoader, load_config

UBUNTU_VERSIONS = ("22.04", "24.04", "26.04")
SIM_FIELDS = ("cluster", "ubuntu", "fake_gpus", "vms", "extra_disk_gb")
VM_FIELDS = ("cpus", "memory_gb", "disk_gb")
SIM_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
STATE_ROOT = Path(".nanohpc-sim")
LIMA_HOME = Path.home() / ".lima"
RECORD = "lima.yml"  # the Lima instances and disks made by `sim up`, read by `sim down`
GIB = 1024**3


@dataclass(frozen=True)
class Vm:
    """One Lima VM standing in for one machine of the cluster."""

    machine: str
    instance: str
    cpus: int
    memory_gb: float
    disk_gb: int
    disks: list[str]  # Lima disk names, attached in order as /dev/vdb, /dev/vdc, ...


@dataclass(frozen=True)
class SimPlan:
    """What `sim up` creates, read from a sim file and its cluster.yml."""

    name: str
    cluster_path: Path
    ubuntu: str
    fake_gpus: list[str]
    extra_disk_gb: int
    vms: list[Vm]


def load_sim(path: Path) -> tuple[SimPlan | None, list[str]]:
    """Read a sim file and its cluster.yml. Return the plan, or None and the list of errors."""
    checker = Checker()
    loader = UniqueKeyLoader(path.read_text())
    raw = loader.get_single_data()
    loader.dispose()
    checker.errors.extend(loader.duplicates)
    sim = checker.mapping(raw, "", SIM_FIELDS, ())
    if sim is None or checker.errors:
        return None, checker.errors
    name = path.stem
    if not SIM_NAME.fullmatch(name):
        checker.fail(f"{path.name}:", "the file name must be lowercase letters, digits, and -")
    if not isinstance(sim["cluster"], str) or not sim["cluster"]:
        checker.fail("cluster", "must be a file name")
        return None, checker.errors
    cluster_path = (path.parent / sim["cluster"]).resolve()
    if not cluster_path.is_file():
        checker.fail("cluster:", f"file not found: {cluster_path}")
        return None, checker.errors
    config, errors = load_config(cluster_path)
    if errors:
        return None, [f"{cluster_path.name}: {error}" for error in errors]
    machines: dict[str, dict[str, Any]] = config["machines"]
    if sim["ubuntu"] not in UBUNTU_VERSIONS:
        checker.fail("ubuntu", f"must be one of {', '.join(UBUNTU_VERSIONS)} (in quotes)")
    fake_gpus = sim["fake_gpus"]
    if not isinstance(fake_gpus, list):
        checker.fail("fake_gpus", "must be a list of machine names")
        fake_gpus = []
    if len(set(map(str, fake_gpus))) != len(fake_gpus):
        checker.fail("fake_gpus", "has duplicates")
    for index, machine in enumerate(fake_gpus):
        if not isinstance(machine, str):
            checker.fail(f"fake_gpus[{index}]", "must be a machine name")
        elif machine not in machines:
            checker.fail("fake_gpus:", f"{machine} is not a machine in the cluster configuration")
        elif machines[machine].get("gpu") is None:
            checker.fail("fake_gpus:", f"{machine} has no gpu in the cluster configuration")
    extra_disk_gb = checker.positive(sim["extra_disk_gb"], "extra_disk_gb") or 0
    sizes = check_vms(checker, sim["vms"], machines)
    vms = [plan_vm(checker, name, machine, values, sizes) for machine, values in machines.items()]
    if checker.errors:
        return None, checker.errors
    return SimPlan(name, cluster_path, sim["ubuntu"], list(fake_gpus), extra_disk_gb, vms), []


def check_vms(checker: Checker, value: Any, machines: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Check the VM sizes: a required `default`, and optional sizes for named machines."""
    if not isinstance(value, Mapping):
        checker.fail("vms", "must be a mapping")
        return {}
    if "default" not in value:
        checker.fail("vms.default", "is required")
    sizes: dict[str, dict[str, Any]] = {}
    for key, size in value.items():
        if key != "default" and key not in machines:
            checker.fail(f"vms.{key}", "is not a machine in the cluster configuration")
            continue
        vm = checker.mapping(size, f"vms.{key}", VM_FIELDS if key == "default" else (), VM_FIELDS)
        if vm is None:
            continue
        for field in ("cpus", "disk_gb"):
            if field in vm:
                checker.positive(vm[field], f"vms.{key}.{field}")
        memory = vm.get("memory_gb", 1)
        if isinstance(memory, bool) or not isinstance(memory, int | float) or memory <= 0:
            checker.fail(f"vms.{key}.memory_gb", "must be a positive number")
        sizes[key] = vm
    return sizes


def plan_vm(checker: Checker, sim: str, machine: str, values: dict[str, Any], sizes: dict[str, dict[str, Any]]) -> Vm:
    """Plan the VM of one machine: its size, and one extra disk per device path in the configuration."""
    size = {**sizes.get("default", {}), **sizes.get(machine, {})}
    instance = f"nanohpc-{sim}-{machine.lower()}"
    home = values.get("home") or {}
    scratch = values.get("scratch") or {}
    devices = sorted(device for device in (home.get("device"), scratch.get("device")) if device)
    expected = [f"/dev/vd{chr(ord('b') + index)}" for index in range(len(devices))]
    if devices != expected:
        found = ", ".join(device for device in devices if device not in expected)
        checker.fail(
            f"machines.{machine}:",
            f"a simulated machine's extra disks are /dev/vdb, /dev/vdc, ... in order, found {found}",
        )
    disks = [f"{instance}-{Path(device).name}" for device in devices]
    return Vm(machine, instance, size.get("cpus", 0), size.get("memory_gb", 0), size.get("disk_gb", 0), disks)


def render_cluster(plan: SimPlan, addresses: dict[str, str]) -> str:
    """Return the cluster.yml text with each machine's address replaced by its VM's address."""
    raw = yaml.safe_load(plan.cluster_path.read_text())
    for machine, address in addresses.items():
        raw["machines"][machine]["address"] = address
    header = f"# Generated by nanohpc sim up from {plan.cluster_path}. Do not edit: it is rewritten on every sim up.\n"
    return header + yaml.safe_dump(raw, sort_keys=False)


def render_ssh_config(plan: SimPlan, ports: dict[str, int], user: str, identity: Path) -> str:
    """Return an SSH config that reaches each simulated machine by its name, through Lima's forwarded port."""
    blocks = [
        f"Host {vm.machine}\n  HostName 127.0.0.1\n  Port {ports[vm.machine]}\n  User {user}\n"
        f"  IdentityFile {identity}\n  IdentitiesOnly yes\n"
        "  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n  LogLevel ERROR\n"
        for vm in plan.vms
    ]
    return "\n".join(blocks)


def lima(*arguments: str) -> str:
    """Run limactl and return its output. A failure stops the command with limactl's own error."""
    result = subprocess.run(["limactl", *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"limactl {' '.join(arguments)} failed:\n{result.stderr.strip()}")
    return result.stdout


def instances() -> dict[str, tuple[int, int, int]]:
    """Return the existing Lima instances with their CPUs, memory bytes, and disk bytes."""
    listing = lima("list", "--format", "{{.Name}} {{.CPUs}} {{.Memory}} {{.Disk}}")
    rows = [line.split() for line in listing.splitlines() if line.strip()]
    return {row[0]: (int(row[1]), int(row[2]), int(row[3])) for row in rows}


def disks() -> dict[str, int]:
    """Return the existing Lima disks with their size in bytes."""
    rows = [json.loads(line) for line in lima("disk", "ls", "--json").splitlines() if line.strip()]
    return {row["name"]: int(row["size"]) for row in rows}


def size_mismatches(
    plan: SimPlan, found_instances: dict[str, tuple[int, int, int]], found_disks: dict[str, int]
) -> list[str]:
    """Describe existing VMs and disks whose size differs from the sim file: they must be removed first."""
    problems: list[str] = []
    for vm in plan.vms:
        wanted = (vm.cpus, round(vm.memory_gb * GIB), vm.disk_gb * GIB)
        if vm.instance in found_instances and found_instances[vm.instance] != wanted:
            cpus, memory, disk = found_instances[vm.instance]
            problems.append(
                f"{vm.instance} has {cpus} CPUs, {memory / GIB:g} GB memory, {disk / GIB:g} GB disk; "
                f"the sim file asks for {vm.cpus} CPUs, {vm.memory_gb:g} GB memory, {vm.disk_gb} GB disk"
            )
        for disk in vm.disks:
            if disk in found_disks and found_disks[disk] != plan.extra_disk_gb * GIB:
                problems.append(
                    f"disk {disk} is {found_disks[disk] / GIB:g} GB; the sim file asks for {plan.extra_disk_gb} GB"
                )
    return problems


def cluster_network() -> ipaddress.IPv4Network:
    """Return the subnet of Lima's user-v2 network, from Lima's networks.yaml."""
    networks = yaml.safe_load((LIMA_HOME / "_config" / "networks.yaml").read_text())["networks"]["user-v2"]
    return ipaddress.IPv4Network(f"{networks['gateway']}/{networks['netmask']}", strict=False)


def vm_address(instance: str, network: ipaddress.IPv4Network) -> str:
    """Return the VM's address on the cluster network, waiting up to 60 seconds for DHCP."""
    output = ""
    for _ in range(30):
        output = lima("shell", "--workdir", "/", instance, "ip", "-4", "-o", "addr", "show")
        for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", output):
            if ipaddress.IPv4Address(match.group(1)) in network:
                return match.group(1)
        time.sleep(2)
    sys.exit(f"{instance}: no address on the Lima user-v2 network {network} after 60 seconds:\n{output}")


def up(plan: SimPlan) -> Path:
    """Create and start the VMs, then write the generated files. Existing VMs are reused."""
    found_instances = instances()
    found_disks = disks()
    problems = size_mismatches(plan, found_instances, found_disks)
    if problems:
        sys.exit("\n".join([*problems, f"run nanohpc sim down first to remove the old {plan.name} cluster"]))
    state = STATE_ROOT / plan.name
    state.mkdir(parents=True, exist_ok=True)
    # Recorded before anything is created, so `sim down` can remove a half-made cluster.
    record = {"instances": [vm.instance for vm in plan.vms], "disks": [disk for vm in plan.vms for disk in vm.disks]}
    (state / RECORD).write_text(yaml.safe_dump(record))
    for vm in plan.vms:
        for disk in vm.disks:
            if disk not in found_disks:
                lima("disk", "create", disk, "--size", f"{plan.extra_disk_gb}GiB", "--format", "raw")
        if vm.instance not in found_instances:
            print(f"creating {vm.instance}")
            attached = ", ".join(f'{{"name": "{disk}", "format": false}}' for disk in vm.disks)
            lima(
                "create", "--tty=false", f"--name={vm.instance}", f"--cpus={vm.cpus}", f"--memory={vm.memory_gb}",
                f"--disk={vm.disk_gb}", "--network=lima:user-v2", "--set", ".mounts = []",
                "--set", f".additionalDisks = [{attached}]", f"template:ubuntu-{plan.ubuntu}",
            )  # fmt: skip
    print(f"starting {len(plan.vms)} VMs")
    starts = [
        (vm, subprocess.Popen(["limactl", "start", "--tty=false", vm.instance], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True))
        for vm in plan.vms
    ]  # fmt: skip
    failed = [(vm, process.communicate()[1]) for vm, process in starts if process.wait() != 0]
    if failed:
        sys.exit("\n".join(f"{vm.instance} did not start:\n{error.strip()}" for vm, error in failed))
    network = cluster_network()
    addresses = {vm.machine: vm_address(vm.instance, network) for vm in plan.vms}
    listing = lima("list", "--format", "{{.Name}} {{.SSHLocalPort}}")
    ports_by_instance = dict(line.split() for line in listing.splitlines() if line.strip())
    ports = {vm.machine: int(ports_by_instance[vm.instance]) for vm in plan.vms}
    (state / "cluster.yml").write_text(render_cluster(plan, addresses))
    (state / "ssh_config").write_text(render_ssh_config(plan, ports, getpass.getuser(), LIMA_HOME / "_config" / "user"))
    (state / "fake-gpus.yml").write_text(yaml.safe_dump({"fake_gpus": plan.fake_gpus}))
    return state


def down(name: str, plan: SimPlan | None) -> None:
    """Delete the VMs and extra disks recorded by `sim up` and those of the plan, then the generated files."""
    state = STATE_ROOT / name
    wanted: dict[str, set[str]] = {"instances": set(), "disks": set()}
    if (state / RECORD).is_file():
        record = yaml.safe_load((state / RECORD).read_text())
        wanted["instances"].update(record["instances"])
        wanted["disks"].update(record["disks"])
    if plan is not None:
        wanted["instances"].update(vm.instance for vm in plan.vms)
        wanted["disks"].update(disk for vm in plan.vms for disk in vm.disks)
    for instance in sorted(wanted["instances"] & set(instances())):
        print(f"deleting {instance}")
        lima("delete", "--force", instance)
    for disk in sorted(wanted["disks"] & set(disks())):
        lima("disk", "delete", disk)
    if state.exists():
        shutil.rmtree(state)
