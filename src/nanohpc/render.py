"""Generate the cluster's configuration files from a validated cluster.yml.

Ansible only copies these files and runs services, so every generated file can be tested here.
Ported from SLURM-REAL's templates (slurm.conf.j2, gres.conf.j2, job_submit.lua.j2, hosts.j2),
generalized to admin-defined partitions and CPU-only nodes.
"""

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class Rendered:
    """Everything generated for one cluster."""

    slurm_conf: str
    cgroup_conf: str
    gres_conf: dict[str, str]  # per compute machine; empty for CPU-only machines
    job_submit_lua: str
    hosts: str
    qos: list[dict[str, Any]]  # one per partition, applied with sacctmgr


CGROUP_CONF = """CgroupPlugin=autodetect
ConstrainCores=yes
ConstrainRAMSpace=yes
ConstrainSwapSpace=yes
AllowedSwapSpace=0
ConstrainDevices=yes
"""


def compute_machines(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the machines with the compute role, in cluster.yml order."""
    return {name: machine for name, machine in config["machines"].items() if "compute" in machine["roles"]}


def front_machine(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the name and definition of the front node."""
    return next((name, machine) for name, machine in config["machines"].items() if "front" in machine["roles"])


def gpu_count(machine: dict[str, Any]) -> int:
    """Return a machine's GPU count, 0 for CPU-only machines."""
    return machine["gpu"]["count"] if machine.get("gpu") else 0


def partition_members(config: dict[str, Any], partition: str) -> list[str]:
    """Return the compute machines in a partition, in cluster.yml order."""
    return [name for name, machine in compute_machines(config).items() if partition in machine["partitions"]]


def partition_gpu_limit(config: dict[str, Any], partition: str) -> int:
    """Return the most GPUs one user can hold in a partition: its limits and its capacity, whichever is smallest."""
    limits = [sum(gpu_count(config["machines"][name]) for name in partition_members(config, partition))]
    for limit in (config["partitions"][partition]["max_gpus_per_user"], config["policy"]["max_gpus_per_user"]):
        if limit != "unlimited":
            limits.append(limit)
    return min(limits)


def minutes(slurm_time: str) -> int:
    """Convert a Slurm time (HH:MM:SS or D-HH:MM:SS) to whole minutes."""
    days, _, clock = slurm_time.rpartition("-")
    hours, mins, _ = (int(part) for part in clock.split(":"))
    return int(days or 0) * 1440 + hours * 60 + mins


def wall_as_printed(slurm_time: str) -> str:
    """Return a time limit the way sacctmgr prints it: D-HH:MM:SS from one day up, else HH:MM:SS."""
    days, _, clock = slurm_time.rpartition("-")
    hours, mins, secs = (int(part) for part in clock.split(":"))
    total = int(days or 0) * 86400 + hours * 3600 + mins * 60 + secs
    day, rest = divmod(total, 86400)
    text = f"{rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}"
    return f"{day}-{text}" if day else text


def render_slurm_conf(config: dict[str, Any], hostnames: dict[str, str], simulated: bool) -> str:
    """Return slurm.conf, shared by the controller and every compute machine.

    Slurm matches daemons to machines by hostname, so each machine's real hostname is used
    and machines keep the hostnames they have.
    """
    front, front_values = front_machine(config)
    policy = config["policy"]
    lines = [
        f"ClusterName={config['cluster']['name']}",
        f"SlurmctldHost={hostnames[front]}({front_values['address']})",
        "SlurmUser=slurm",
        "StateSaveLocation=/var/lib/slurm/slurmctld",
        "SlurmdSpoolDir=/var/lib/slurm/slurmd",
        "SlurmctldPidFile=/run/slurmctld/slurmctld.pid",
        "SlurmdPidFile=/run/slurm/slurmd.pid",
        "SlurmctldLogFile=/var/log/slurm/slurmctld.log",
        "SlurmdLogFile=/var/log/slurm/slurmd.log",
        "ReturnToService=1",
        "SchedulerType=sched/backfill",
        "PriorityType=priority/multifactor",
        f"PriorityWeightFairshare={policy['fairshare_weight']}",
        f"PriorityWeightAge={policy['age_weight']}",
        f"PriorityDecayHalfLife={policy['fairshare_half_life']}",
        f"PriorityMaxAge={policy['age_max']}",
        "PriorityCalcPeriod=00:01:00",
        "PreemptType=preempt/none",
        "EnforcePartLimits=ALL",
        "JobSubmitPlugins=lua",
        "AuthType=auth/munge",
        "SelectType=select/cons_tres",
        "SelectTypeParameters=CR_Core_Memory",
        "GresTypes=gpu",
        "AccountingStorageType=accounting_storage/slurmdbd",
        f"AccountingStorageHost={front_values['address']}",
        "AccountingStorageEnforce=associations,limits,qos",
        "AccountingStorageTRES=gres/gpu",
        "ProctrackType=proctrack/cgroup",
        "TaskPlugin=task/affinity,task/cgroup",
        "JobAcctGatherType=jobacct_gather/cgroup",
    ]
    if simulated:
        # Simulated machines are smaller VMs than the hardware written in cluster.yml.
        lines.append("SlurmdParameters=config_overrides")
    for name, machine in compute_machines(config).items():
        cpu = machine["cpu"]
        cpus = cpu["sockets"] * cpu["cores_per_socket"] * cpu["threads_per_core"]
        gres = f" Gres=gpu:{machine['gpu']['type']}:{machine['gpu']['count']}" if machine.get("gpu") else ""
        lines.append(
            f"NodeName={name} NodeHostname={hostnames[name]} NodeAddr={machine['address']} CPUs={cpus} Boards=1 SocketsPerBoard={cpu['sockets']} "
            f"CoresPerSocket={cpu['cores_per_socket']} ThreadsPerCore={cpu['threads_per_core']} "
            f"RealMemory={machine['memory_mb']}{gres} State=UNKNOWN"
        )
    for name, partition in config["partitions"].items():
        lines.append(
            f"PartitionName={name} Nodes={','.join(partition_members(config, name))} "
            f"Default={'YES' if partition['default'] else 'NO'} MaxTime={partition['max_time']} "
            f"DefMemPerCPU={policy['default_memory_mb_per_cpu']} DefCpuPerGPU={policy['default_cpus_per_gpu']} "
            f"QoS={name} AllowQos=normal TRESBillingWeights=CPU=0,Mem=0,GRES/gpu=1 OverSubscribe=NO State=UP"
        )
    return "\n".join(lines) + "\n"


def render_gres_conf(machine: dict[str, Any]) -> str:
    """Return gres.conf for one compute machine. Slurm needs a device file per GPU; on simulated
    machines nanoHPC creates placeholder device files at the same paths."""
    gpu = machine.get("gpu")
    if not gpu:
        return ""
    devices = "/dev/nvidia0" if gpu["count"] == 1 else f"/dev/nvidia[0-{gpu['count'] - 1}]"
    return f"Name=gpu Type={gpu['type']} File={devices}\n"


def lua_table(values: dict[str, Any]) -> str:
    """Return a Lua table literal with string keys, for simple string and number values."""
    return "{ " + ", ".join(f"[{json.dumps(key)}] = {json.dumps(value)}" for key, value in values.items()) + " }"


def render_job_submit(config: dict[str, Any]) -> str:
    """Return job_submit.lua: the partition tables followed by the shared rules."""
    partitions = config["partitions"]
    default = next(name for name, partition in partitions.items() if partition["default"])
    header = "\n".join(
        [
            "-- Generated by nanoHPC from cluster.yml. Explains common policy refusals; Slurm's own limits stay authoritative.",
            f"local submit_limit = {config['policy']['max_submit_jobs_per_user']}",
            f"local default_partition = {json.dumps(default)}",
            f"local job_types = {lua_table({name: p['jobs'] for name, p in partitions.items()})}",
            f"local walls = {lua_table({name: minutes(p['max_time']) for name, p in partitions.items()})}",
            f"local gpu_limits = {lua_table({name: partition_gpu_limit(config, name) for name in partitions})}",
        ]
    )
    rules = resources.files("nanohpc").joinpath("files", "job_submit_rules.lua").read_text()
    return header + "\n" + rules


def render_qos(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one QoS per partition: time limit, per-user GPU limit, and per-job GPU limit."""
    items: list[dict[str, Any]] = []
    for name, partition in config["partitions"].items():
        per_user = partition["max_gpus_per_user"]
        items.append(
            {
                "name": name,
                "max_wall": wall_as_printed(partition["max_time"]),
                "max_gpus_per_user": None if per_user == "unlimited" else per_user,
                "max_gpus_per_job": partition_gpu_limit(config, name),
            }
        )
    return items


def render_hosts(config: dict[str, Any]) -> str:
    """Return the /etc/hosts block naming every machine and its aliases."""
    return "".join(
        f"{machine['address']} {' '.join([name, *machine['aliases']])}\n"
        for name, machine in config["machines"].items()
    )


def render(config: dict[str, Any], hostnames: dict[str, str], simulated: bool) -> Rendered:
    """Generate every configuration file for a validated config and the machines' real hostnames."""
    return Rendered(
        slurm_conf=render_slurm_conf(config, hostnames, simulated),
        cgroup_conf=CGROUP_CONF,
        gres_conf={name: render_gres_conf(machine) for name, machine in compute_machines(config).items()},
        job_submit_lua=render_job_submit(config),
        hosts=render_hosts(config),
        qos=render_qos(config),
    )
