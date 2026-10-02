"""Generate the cluster's configuration files from a validated cluster.yml.

Ansible only copies these files and runs services, so every generated file can be tested here.
Partitions are defined by the administrator, and compute nodes may be CPU-only.
"""

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml


@dataclass(frozen=True)
class Rendered:
    """Everything generated for one cluster."""

    slurm_conf: str
    cgroup_conf: str
    gres_conf: dict[str, str]  # per compute machine; empty for CPU-only machines
    job_submit_lua: str
    hosts: str
    qos: list[dict[str, Any]]  # one per partition, applied with sacctmgr
    home_exports: str  # /etc/exports.d line per machine that mounts /home
    prometheus_yml: str  # the front node's Prometheus configuration


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


def home_server(config: dict[str, Any]) -> str:
    """Return the name of the machine that serves /home."""
    return next(name for name, machine in config["machines"].items() if "home" in machine["roles"])


def home_clients(config: dict[str, Any]) -> list[str]:
    """Return the machines that mount /home: all but the home machine and the backup machine."""
    server = home_server(config)
    return [name for name, m in config["machines"].items() if name != server and "backup" not in m["roles"]]


def render_home_exports(config: dict[str, Any]) -> str:
    """Return the NFS exports of /home, one line per client address.

    no_root_squash: on Ubuntu 26.04+ the forwarded SSH agent socket that unlocks an
    administrator's sudo is in their home, and root must reach it. /home is mounted nosuid on every machine,
    so no program in /home can gain root (agreed with the user, 2026-10-01).
    """
    return "".join(
        f"/home {config['machines'][name]['address']}(rw,sync,no_root_squash,no_subtree_check)\n"
        for name in home_clients(config)
    )


METRICS_TLS = "/etc/nanohpc/metrics-tls"


def render_prometheus(config: dict[str, Any]) -> str:
    """Return prometheus.yml: the front node's own exporter on localhost, and every other machine's
    exporter over mutually authenticated TLS (certificates signed by the cluster's metrics CA)."""
    front, _ = front_machine(config)

    def job(name: str, target: str, machine: str) -> dict[str, Any]:
        return {
            "job_name": name,
            "static_configs": [{"targets": [target], "labels": {"machine": machine}}],
            "sample_limit": 20000,
        }

    jobs = [job("node", "127.0.0.1:9100", front)]
    for name, machine in config["machines"].items():
        if name == front:
            continue
        remote = job(f"node-{name}", f"{machine['address']}:9100", name)
        remote["scheme"] = "https"
        remote["tls_config"] = {
            "ca_file": f"{METRICS_TLS}/ca.crt",
            "cert_file": f"{METRICS_TLS}/prometheus.crt",
            "key_file": f"{METRICS_TLS}/prometheus.key",
        }
        jobs.append(remote)
    jobs.append(job("prometheus", "127.0.0.1:9090", front))
    jobs.append(job("alertmanager", "127.0.0.1:9093", front))
    prometheus = {
        "global": {"scrape_interval": "30s", "scrape_timeout": "10s", "evaluation_interval": "30s"},
        "rule_files": ["/etc/nanohpc/prometheus/daily-rules.yml", "/etc/nanohpc/prometheus/alert-rules.yml"],
        "alerting": {"alertmanagers": [{"static_configs": [{"targets": ["127.0.0.1:9093"]}]}]},
        "scrape_configs": jobs,
    }
    return yaml.safe_dump(prometheus, sort_keys=False)


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
        home_exports=render_home_exports(config),
        prometheus_yml=render_prometheus(config),
    )


FORWARDING_RULES = """\
Forwarding rules for the lab's own web server, so that https://<lab website>{path} shows the cluster
website served by the front node at https://{hostname}{path}. Add the one for your web server, keep the
path {path} the same on both sides, and reload the web server.

In cluster.yml, also set `forwarded_by: <the lab web server's address>` under cluster.website: the
front node then uses the visitor address the lab web server passes on, for its per-visitor rate limits and
for `allow`. Without it, every visitor looks like the lab web server.

--- nginx (inside the lab website's server block) ---
location = {bare} {{ return 301 {path}; }}
location {path} {{
    proxy_pass https://{hostname};
    proxy_set_header Host {hostname};
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_ssl_server_name on;
    proxy_ssl_name {hostname};
    proxy_ssl_verify on;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
}}

--- Apache (inside the lab website's VirtualHost; needs mod_proxy, mod_proxy_http, and mod_ssl) ---
SSLProxyEngine on
SSLProxyVerify require
SSLProxyCACertificateFile /etc/ssl/certs/ca-certificates.crt
RedirectMatch 301 ^{bare}$ {path}
ProxyPass {path} https://{hostname}{path}
ProxyPassReverse {path} https://{hostname}{path}

--- Caddy (inside the lab website's site block) ---
redir {bare} {path}
handle {path}* {{
    reverse_proxy https://{hostname} {{
        header_up Host {hostname}
    }}
}}
"""


def render_forwarding_rules(config: dict[str, Any]) -> str:
    """Return ready-made rules (nginx, Apache, Caddy) for a lab's own web server to show the cluster website
    under the same path, forwarded over verified HTTPS to the front node."""
    website = config["cluster"]["website"]
    path = website["path"]
    # A lab website forwards a path such as /cluster/ (the whole hostname, "/", is not forwarded).
    return FORWARDING_RULES.format(path=path, bare=path.rstrip("/"), hostname=website["hostname"])
