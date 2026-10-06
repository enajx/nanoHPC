"""Load and validate cluster.yml, and fill in defaults.

`check_config` collects every error it finds, each naming the field path, so the
administrator can fix them all in one pass. Nothing is changed on any machine here.
"""

import ipaddress
import re
import subprocess
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import Any

import yaml

ROLES = ("front", "home", "backup", "compute", "shared")
JOB_TYPES = ("batch", "interactive", "any")  # batch: sbatch only; interactive: only the documented shell
COMPUTE_FIELDS = ("cpu", "memory_mb", "gpu", "partitions", "scratch")
MACHINE_FIELDS = ("address", "aliases", "roles", "home", "backup", "shared", *COMPUTE_FIELDS)
TOP_FIELDS = (
    "cluster",
    "machines",
    "users",
    "partitions",
    "policy",
    "home",
    "scratch",
    "backup",
    "alerts",
    "auto_deploy",
    "nanohpc_version",
)

POLICY_DEFAULTS: dict[str, Any] = {
    "max_submit_jobs_per_user": 30,
    "max_gpus_per_user": "unlimited",
    "default_cpus_per_gpu": 4,
    "default_memory_mb_per_cpu": 8192,
    "fairshare_weight": 10000,
    "fairshare_half_life": "7-00:00:00",
    "age_weight": 1000,
    "age_max": "7-00:00:00",
}
HOME_DEFAULTS: dict[str, Any] = {"quota_soft_gb": 300, "quota_hard_gb": 400, "quota_grace": "7days"}
SCRATCH_DEFAULTS: dict[str, Any] = {"cleanup_days": 14, "job_retention_days": 7}
ALERTS_DEFAULTS: dict[str, Any] = {"slack": False}
AUTO_DEPLOY_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "repository": None,
    "branch": "main",
    "every_minutes": 10,
    "webhook": False,
}

CLUSTER_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
DNS_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
VERSION = re.compile(r"\d+\.\d+\.\d+")
# user@host:path or ssh://user@host/path, on SSH's own port (the host key is learned for port 22).
GIT_SSH_URL = re.compile(
    r"(ssh://[a-z_][a-z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9_.-]*/|[a-z_][a-z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9_.-]*:)[A-Za-z0-9_.~/-]+"
)
BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
URL_PATH = re.compile(r"/([A-Za-z0-9._~-]+/)*([A-Za-z0-9._~-]+)?")


def is_network(value: str) -> bool:
    """Return whether a value is an IPv4 or IPv6 network (an address alone is a one-address network)."""
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


def is_address(value: str) -> bool:
    """Return whether a value is an IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


MACHINE_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")
HOST_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
USER_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
PARTITION_NAME = re.compile(r"[a-z][a-z0-9_-]*")
GPU_TYPE = re.compile(r"[a-z0-9][a-z0-9_-]*")
SLURM_TIME = re.compile(r"(\d+-\d{1,2}|\d{1,3}):[0-5]\d:[0-5]\d")
CLOCK_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
DEVICE = re.compile(r"/dev/[a-zA-Z0-9_./-]+")
ABSOLUTE_PATH = re.compile(r"/[a-zA-Z0-9_./-]*")
SSH_KEY = re.compile(
    r"(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)"
    r" [A-Za-z0-9+/]+={0,2}( [^\r\n]*)?"
)
# Accounts that exist on Ubuntu or that nanoHPC creates for its own services.
# Accounts nanoHPC creates itself (users in cluster.yml cannot have these names).
NANOHPC_ACCOUNTS = frozenset(["nanohpc-backup"])
EXCLUDE_PATTERN = re.compile(r"[A-Za-z0-9._/*?\[\]-]+")
SYSTEM_NAMES = frozenset(
    [
        "root",
        "daemon",
        "bin",
        "sys",
        "sync",
        "games",
        "man",
        "lp",
        "mail",
        "news",
        "uucp",
        "proxy",
        "www-data",
        "backup",
        "list",
        "irc",
        "nobody",
        "systemd-network",
        "systemd-resolve",
        "messagebus",
        "sshd",
        "syslog",
        "munge",
        "slurm",
        "mysql",
        "prometheus",
        "grafana",
    ]
)
MAX_UID = 60000
TOP_SECTIONS = "the file must be a mapping with the sections cluster, machines, users, partitions"
SSH_TARGET = re.compile(r"[a-z_][a-z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9_.-]*:/[a-zA-Z0-9_./-]*")


class UniqueKeyLoader(yaml.SafeLoader):
    """A YAML loader that records duplicate keys instead of letting the last one silently win."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.duplicates: list[str] = []

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Build a mapping, recording each key that appears twice."""
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, Hashable):
                if key in seen:
                    self.duplicates.append(f"line {key_node.start_mark.line + 1}: duplicate key {key}")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


class Checker:
    """Collect errors, each prefixed with the path of the field it is about."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, path: str, message: str) -> None:
        """Record one error for the field at `path`."""
        self.errors.append(f"{path} {message}" if path else message)

    def mapping(self, value: Any, path: str, required: tuple[str, ...], optional: tuple[str, ...]) -> dict | None:
        """Check a mapping has the required keys and no unknown keys. Return it, or None if it is not a mapping."""
        if not isinstance(value, Mapping):
            self.fail(path, "must be a mapping")
            return None
        prefix = f"{path}." if path else ""
        for key in value:
            if key not in required and key not in optional:
                self.fail(f"{prefix}{key}", "is not a known field")
        for key in required:
            if key not in value:
                self.fail(f"{prefix}{key}", "is required")
        return dict(value)

    def positive(self, value: Any, path: str) -> int | None:
        """Check a positive integer, refusing booleans and strings."""
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        self.fail(path, "must be a positive integer")
        return None

    def matches(self, value: Any, pattern: re.Pattern[str], path: str, description: str) -> str | None:
        """Check a string fully matches `pattern`."""
        if isinstance(value, str) and pattern.fullmatch(value):
            return value
        self.fail(path, f"must be {description}")
        return None

    def boolean(self, value: Any, path: str) -> bool | None:
        """Check a true/false value."""
        if isinstance(value, bool):
            return value
        self.fail(path, "must be true or false")
        return None

    def string_list(self, value: Any, path: str) -> list[str] | None:
        """Check a list of strings (may be empty)."""
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        self.fail(path, "must be a list of strings")
        return None

    def gpu_limit(self, value: Any, path: str) -> int | str | None:
        """Check a GPU limit: a positive integer or the word unlimited."""
        if value == "unlimited" or (isinstance(value, int) and not isinstance(value, bool) and value > 0):
            return value
        self.fail(path, "must be a positive integer or unlimited")
        return None


def with_defaults(checker: Checker, value: Any, path: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge an optional section over its defaults, refusing unknown keys."""
    if value is None:
        return dict(defaults)
    section = checker.mapping(value, path, (), tuple(defaults))
    return {**defaults, **(section or {})}


def check_cluster(checker: Checker, value: Any, users: set[str], mode: str) -> dict[str, Any] | None:
    """Check the cluster section: name, admins, and website."""
    required = ("name", "monitor_host", "website") if mode == "monitor" else ("name", "admins", "website")
    cluster = checker.mapping(value, "cluster", required, ("mode",))
    if cluster is None:
        return None
    if "mode" in cluster and cluster["mode"] not in ("slurm", "monitor"):
        checker.fail("cluster.mode", "must be slurm or monitor")
    if "name" in cluster:
        checker.matches(
            cluster["name"],
            CLUSTER_NAME,
            "cluster.name",
            "lowercase letters, digits, and -, starting with a letter, at most 40 characters",
        )
    if "admins" in cluster:
        admins = cluster["admins"]
        if not isinstance(admins, list) or not admins:
            checker.fail("cluster.admins", "must be a non-empty list of user names")
        else:
            for index, admin in enumerate(admins):
                if not isinstance(admin, str):
                    checker.fail(f"cluster.admins[{index}]", "must be a user name")
                elif admin not in users:
                    checker.fail("cluster.admins:", f"{admin} is not in users")
    if "website" in cluster:
        website = checker.mapping(
            cluster["website"],
            "cluster.website",
            ("hostname", "https"),
            ("certificate", "certificate_key", "logo", "login_address", "path", "allow", "forwarded_by", "build"),
        )
        if website is not None:
            if "hostname" in website:
                # Lowercase, as certificates store it; Let's Encrypt refuses names with underscores.
                hostname = website["hostname"]
                if isinstance(hostname, str) and DNS_NAME.fullmatch(hostname.lower()):
                    website["hostname"] = hostname.lower()
                else:
                    checker.fail("cluster.website.hostname", "must be a DNS name (letters, digits, - and .)")
            https = website.get("https")
            if "https" in website and https not in ("letsencrypt", "own"):
                checker.fail("cluster.website.https", "must be letsencrypt or own")
            for key in ("certificate", "certificate_key"):
                value = website.get(key)
                if https == "own" and not (isinstance(value, str) and value):
                    checker.fail(f"cluster.website.{key}", "is required when https is own")
                elif https != "own" and value is not None:
                    checker.fail(f"cluster.website.{key}", "is only used when https is own")
            logo = website.get("logo")
            if logo is not None and not (isinstance(logo, str) and logo):
                checker.fail("cluster.website.logo", "must be a file path")
            if website.get("login_address") is not None:
                checker.matches(website["login_address"], HOST_NAME, "cluster.website.login_address", "a host name")
            path = website.get("path", "/cluster/")
            if (
                not isinstance(path, str)
                or not URL_PATH.fullmatch(path)
                or any(part in (".", "..") for part in path.split("/"))
            ):
                checker.fail("cluster.website.path", 'must be a URL path such as "/cluster/" or "/"')
            else:
                website["path"] = path if path.endswith("/") else path + "/"
            allow = website.get("allow", [])
            if not isinstance(allow, list):
                checker.fail("cluster.website.allow", "must be a list of networks, such as [10.0.0.0/8]")
            else:
                for index, network in enumerate(allow):
                    if not isinstance(network, str) or not is_network(network):
                        checker.fail(f"cluster.website.allow[{index}]", "must be a network, such as 10.0.0.0/8")
                website["allow"] = allow
            forwarded_by = website.get("forwarded_by")
            if forwarded_by is not None and not (isinstance(forwarded_by, str) and is_address(forwarded_by)):
                checker.fail("cluster.website.forwarded_by", "must be an IP address (the lab web server's)")
            if website.get("build", "package") not in ("package", "front"):
                checker.fail("cluster.website.build", "must be package or front")
            website.setdefault("path", "/cluster/")
            website.setdefault("forwarded_by", None)
            website.setdefault("build", "package")
            website.setdefault("certificate", None)
            website.setdefault("certificate_key", None)
            website.setdefault("logo", None)
            if website.get("login_address") is None:
                website["login_address"] = website.get("hostname")
            cluster["website"] = website
    return cluster


def check_monitor_users(checker: Checker, value: Any) -> list[str]:
    """Check login names shown on a monitor website; accounts are never managed."""
    if not isinstance(value, list):
        checker.fail("users", "must be a list of login names")
        return []
    names: list[str] = []
    for index, item in enumerate(value):
        name = checker.matches(item, USER_NAME, f"users[{index}]", "a lowercase Linux user name")
        if name is None:
            continue
        if name in names:
            checker.fail(f"users[{index}]", f"{name} is already used")
        names.append(name)
    return names


def check_monitor_machines(checker: Checker, value: Any) -> dict[str, dict[str, Any]]:
    """Check machine names and addresses without Slurm roles or declared hardware."""
    if not isinstance(value, Mapping) or not value:
        checker.fail("machines", "must be a non-empty mapping")
        return {}
    machines: dict[str, dict[str, Any]] = {}
    addresses: dict[str, str] = {}
    hostnames: dict[str, str] = {}
    for name, item in value.items():
        path = f"machines.{name}"
        if not isinstance(name, str) or not MACHINE_NAME.fullmatch(name):
            checker.fail(f"{path}:", "the name must be letters, digits, _ or -, starting with a letter")
            continue
        machine = checker.mapping(item, path, ("address",), ("aliases",))
        if machine is None:
            continue
        address = machine.get("address")
        if "address" in machine:
            if not isinstance(address, str) or not is_ipv4(address):
                checker.fail(f"{path}.address", "must be an IPv4 address")
            elif address.startswith(("127.", "0.")) or address == "255.255.255.255":
                checker.fail(f"{path}.address", "must be the machine's network address, not a loopback or placeholder")
            elif address in addresses:
                checker.fail(f"{path}.address", f"{address} is already used by machines.{addresses[address]}")
            else:
                addresses[address] = name
        machine.setdefault("aliases", [])
        aliases = checker.string_list(machine["aliases"], f"{path}.aliases") or []
        for hostname in [name, *aliases]:
            if not HOST_NAME.fullmatch(hostname):
                checker.fail(f"{path}.aliases:", f"{hostname} is not a valid host name")
            elif hostname.lower() in ("localhost", "localhost.localdomain"):
                checker.fail(f"{path}.aliases:", f"{hostname} is reserved")
            elif hostnames.get(hostname.lower(), name) != name:
                checker.fail(
                    f"{path}.aliases:", f"{hostname} is already used by machines.{hostnames[hostname.lower()]}"
                )
            else:
                hostnames[hostname.lower()] = name
        machines[name] = machine
    return machines


def check_monitor_config(checker: Checker, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the monitoring-only configuration and fill shared website and alert defaults."""
    top = checker.mapping(raw, "", ("cluster", "machines", "users"), ("alerts",))
    if top is None:
        return {}
    users = check_monitor_users(checker, top.get("users"))
    machines = check_monitor_machines(checker, top.get("machines"))
    cluster = check_cluster(checker, top.get("cluster"), set(users), "monitor")
    if cluster is not None:
        host = cluster.get("monitor_host")
        if not isinstance(host, str) or host not in machines:
            checker.fail("cluster.monitor_host", "must name a machine in machines")
        cluster["mode"] = "monitor"
    alerts = with_defaults(checker, top.get("alerts"), "alerts", ALERTS_DEFAULTS)
    checker.boolean(alerts["slack"], "alerts.slack")
    return {"cluster": cluster, "machines": machines, "users": users, "alerts": alerts}


def check_users(checker: Checker, value: Any) -> list[dict[str, Any]]:
    """Check the users list: unique names and UIDs, at least one SSH key each."""
    if not isinstance(value, list) or not value:
        checker.fail("users", "must be a non-empty list")
        return []
    users: list[dict[str, Any]] = []
    names: set[str] = set()
    uids: dict[int, str] = {}
    for index, item in enumerate(value):
        path = f"users[{index}]"
        user = checker.mapping(item, path, ("name", "uid", "ssh_keys"), ())
        if user is None:
            continue
        name = checker.matches(user.get("name"), USER_NAME, f"{path}.name", "a lowercase Linux user name")
        if name in SYSTEM_NAMES:
            checker.fail(f"{path}.name", f"{name} is a system account")
        elif name in NANOHPC_ACCOUNTS:
            checker.fail(f"{path}.name", f"{name} is reserved for nanoHPC")
        elif name is not None:
            if name in names:
                checker.fail(f"{path}.name", f"{name} is already used")
            names.add(name)
        uid = user.get("uid")
        if "uid" in user and checker.positive(uid, f"{path}.uid") is not None:
            if uid < 1000:
                checker.fail(f"{path}.uid", "must be at least 1000 (lower UIDs are for system accounts)")
            elif uid > MAX_UID:
                checker.fail(f"{path}.uid", f"must be at most {MAX_UID} (higher UIDs are for system accounts)")
            elif uid in uids:
                checker.fail(f"{path}.uid", f"{uid} is already used by {uids[uid]}")
            else:
                uids[uid] = str(name)
        keys = user.get("ssh_keys")
        if "ssh_keys" in user:
            if not isinstance(keys, list) or not keys:
                checker.fail(f"{path}.ssh_keys", "must be a non-empty list of public keys")
            else:
                for number, key in enumerate(keys):
                    checker.matches(
                        key,
                        SSH_KEY,
                        f"{path}.ssh_keys[{number}]",
                        "one public key line, like ssh-ed25519 AAAA... comment",
                    )
        users.append(user)
    return users


def check_partitions(checker: Checker, value: Any) -> dict[str, dict[str, Any]]:
    """Check admin-defined partitions and their limits; exactly one is the default."""
    if not isinstance(value, Mapping) or not value:
        checker.fail("partitions", "must be a non-empty mapping of partition names")
        return {}
    partitions: dict[str, dict[str, Any]] = {}
    for name, item in value.items():
        path = f"partitions.{name}"
        if not isinstance(name, str) or not PARTITION_NAME.fullmatch(name):
            checker.fail(f"{path}:", "the name must be lowercase letters, digits, _ or -, starting with a letter")
            continue
        if name == "normal":
            checker.fail(f"{path}:", "the name normal is reserved by Slurm")
            continue
        partition = checker.mapping(item, path, ("max_time",), ("default", "jobs", "max_gpus_per_user"))
        if partition is None:
            continue
        if "max_time" in partition:
            checker.matches(
                partition["max_time"],
                SLURM_TIME,
                f"{path}.max_time",
                'a Slurm time in quotes, like "24:00:00" or "7-00:00:00"',
            )
        partition.setdefault("jobs", "any")
        if partition["jobs"] not in JOB_TYPES:
            checker.fail(f"{path}.jobs", "must be batch, interactive, or any")
        partition.setdefault("default", False)
        checker.boolean(partition["default"], f"{path}.default")
        partition.setdefault("max_gpus_per_user", "unlimited")
        checker.gpu_limit(partition["max_gpus_per_user"], f"{path}.max_gpus_per_user")
        partitions[name] = partition
    defaults = [name for name, partition in partitions.items() if partition["default"] is True]
    if len(defaults) != 1:
        checker.fail("partitions:", f"exactly one partition must have default: true, found {len(defaults)}")
    return partitions


def check_compute(checker: Checker, name: str, machine: dict[str, Any], partitions: dict[str, dict[str, Any]]) -> None:
    """Check the hardware, partitions, and scratch of one compute machine."""
    path = f"machines.{name}"
    for key in ("cpu", "memory_mb", "partitions", "scratch"):
        if key not in machine:
            checker.fail(f"{path}.{key}", "is required")
    if "cpu" in machine:
        cpu = checker.mapping(machine["cpu"], f"{path}.cpu", ("sockets", "cores_per_socket", "threads_per_core"), ())
        if cpu is not None:
            for key in ("sockets", "cores_per_socket", "threads_per_core"):
                if key in cpu:
                    checker.positive(cpu[key], f"{path}.cpu.{key}")
    if "memory_mb" in machine:
        checker.positive(machine["memory_mb"], f"{path}.memory_mb")
    machine.setdefault("gpu", None)
    if machine["gpu"] is not None:
        gpu = checker.mapping(machine["gpu"], f"{path}.gpu", ("type", "count"), ())
        if gpu is not None:
            if "type" in gpu:
                checker.matches(
                    gpu["type"], GPU_TYPE, f"{path}.gpu.type", "a Slurm GPU type (lowercase letters, digits, _ or -)"
                )
            if "count" in gpu:
                checker.positive(gpu["count"], f"{path}.gpu.count")
    if "partitions" in machine:
        names = machine["partitions"]
        if not isinstance(names, list) or not names:
            checker.fail(f"{path}.partitions", "must be a non-empty list")
        else:
            if len(set(map(str, names))) != len(names):
                checker.fail(f"{path}.partitions", "has duplicates")
            for number, partition in enumerate(names):
                if not isinstance(partition, str):
                    checker.fail(f"{path}.partitions[{number}]", "must be a partition name")
                elif partition not in partitions:
                    checker.fail(f"{path}.partitions:", f"unknown partition {partition}")
    if "scratch" in machine:
        scratch = checker.mapping(machine["scratch"], f"{path}.scratch", (), ("device", "image_gb"))
        if scratch is not None:
            if ("device" in scratch) == ("image_gb" in scratch):
                checker.fail(f"{path}.scratch", "must have exactly one of device or image_gb")
            elif "device" in scratch:
                checker.matches(scratch["device"], DEVICE, f"{path}.scratch.device", "a device path under /dev/")
            else:
                checker.positive(scratch["image_gb"], f"{path}.scratch.image_gb")
            machine["scratch"] = {"device": scratch.get("device"), "image_gb": scratch.get("image_gb")}


def check_machines(checker: Checker, value: Any, partitions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Check every machine, its roles, and the cluster-wide role counts."""
    if not isinstance(value, Mapping):
        checker.fail("machines", "must be a mapping")
        return {}
    machines: dict[str, dict[str, Any]] = {}
    addresses: dict[str, str] = {}
    hostnames: dict[str, str] = {}
    for name, item in value.items():
        path = f"machines.{name}"
        if not isinstance(name, str) or not MACHINE_NAME.fullmatch(name):
            checker.fail(f"{path}:", "the name must be letters, digits, _ or -, starting with a letter")
            continue
        machine = checker.mapping(item, path, ("address", "roles"), MACHINE_FIELDS)
        if machine is None:
            continue
        address = machine.get("address")
        if "address" in machine:
            if not isinstance(address, str) or not is_ipv4(address):
                checker.fail(f"{path}.address", "must be an IPv4 address")
            elif address.startswith(("127.", "0.")) or address == "255.255.255.255":
                checker.fail(f"{path}.address", "must be the machine's network address, not a loopback or placeholder")
            elif address in addresses:
                checker.fail(f"{path}.address", f"{address} is already used by machines.{addresses[address]}")
            else:
                addresses[address] = name
        machine.setdefault("aliases", [])
        aliases = checker.string_list(machine["aliases"], f"{path}.aliases") or []
        for hostname in [name, *aliases]:
            if not HOST_NAME.fullmatch(hostname):
                checker.fail(f"{path}.aliases:", f"{hostname} is not a valid host name")
            elif hostname.lower() in ("localhost", "localhost.localdomain"):
                checker.fail(f"{path}.aliases:", f"{hostname} is reserved")
            elif hostnames.get(hostname.lower(), name) != name:
                checker.fail(
                    f"{path}.aliases:", f"{hostname} is already used by machines.{hostnames[hostname.lower()]}"
                )
            else:
                hostnames[hostname.lower()] = name
        roles = machine.get("roles")
        if not isinstance(roles, list) or not roles:
            if "roles" in machine:
                checker.fail(f"{path}.roles", "must be a non-empty list")
            # Without roles, the other fields cannot be judged: skip them to avoid follow-on errors.
            machine["roles"] = []
            machines[name] = machine
            continue
        for role in roles:
            if role not in ROLES:
                checker.fail(f"{path}.roles:", f"unknown role {role} (known: {', '.join(ROLES)})")
        if len(set(map(str, roles))) != len(roles):
            checker.fail(f"{path}.roles", "has duplicates")
        for role in ("front", "home", "backup", "shared"):
            if role in roles and "compute" in roles:
                checker.fail(f"{path}:", f"the {role} role cannot be combined with compute")
        if "front" in roles and "shared" in roles:
            checker.fail(f"{path}:", "the shared role must be on a storage machine, not the front node")
        if "home" in roles and "shared" in roles:
            checker.fail(f"{path}:", "the shared datasets server must differ from the home server")
        if "home" in roles and "backup" in roles:
            checker.fail(f"{path}:", "the backup role cannot be on the home machine")
        if "compute" in roles:
            check_compute(checker, name, machine, partitions)
        else:
            for key in COMPUTE_FIELDS:
                if key in machine:
                    checker.fail(f"{path}.{key}", "is only for compute machines")
        if "home" in roles:
            home = checker.mapping(machine.get("home") or {}, f"{path}.home", (), ("device",)) or {}
            device = home.get("device")
            if device is not None:
                checker.matches(device, DEVICE, f"{path}.home.device", "a device path under /dev/")
            machine["home"] = {"device": device}
        elif "home" in machine:
            checker.fail(f"{path}.home", "is only for the home machine")
        if "backup" in roles:
            if "backup" not in machine:
                checker.fail(f"{path}.backup.path", "is required")
            else:
                backup = checker.mapping(machine["backup"], f"{path}.backup", ("path",), ())
                if backup is not None and "path" in backup and not is_directory_path(backup["path"]):
                    checker.fail(f"{path}.backup.path", "must be an absolute path to a directory, not / and without ..")
        elif "backup" in machine:
            checker.fail(f"{path}.backup", "is only for a machine with the backup role")
        if "shared" in roles:
            shared = checker.mapping(machine.get("shared"), f"{path}.shared", ("device",), ())
            if shared is not None and "device" in shared:
                checker.matches(shared["device"], DEVICE, f"{path}.shared.device", "a device path under /dev/")
        elif "shared" in machine:
            checker.fail(f"{path}.shared", "is only for a machine with the shared role")
        machine["roles"] = roles
        machines[name] = machine
    for role in ("front", "home"):
        count = sum(role in machine["roles"] for machine in machines.values())
        if count != 1:
            checker.fail("machines:", f"exactly one machine must have the {role} role, found {count}")
    if sum("backup" in machine["roles"] for machine in machines.values()) > 1:
        checker.fail("machines:", "at most one machine can have the backup role")
    if sum("shared" in machine["roles"] for machine in machines.values()) > 1:
        checker.fail("machines:", "at most one machine can have the shared role")
    if not any("compute" in machine["roles"] for machine in machines.values()):
        checker.fail("machines:", "at least one machine must have the compute role")
    for partition in partitions:
        members = [
            name
            for name, machine in machines.items()
            if "compute" in machine["roles"] and partition in (machine.get("partitions") or [])
        ]
        if not members:
            checker.fail(f"partitions.{partition}", "has no compute machines")
    return machines


def is_ipv4(text: str) -> bool:
    """Return whether `text` is an IPv4 address, without raising on other text."""
    parts = text.split(".")
    return len(parts) == 4 and all(part.isdigit() and part == str(int(part)) and int(part) <= 255 for part in parts)


def is_directory_path(value: Any) -> bool:
    """Return whether `value` is an absolute directory path other than / and without .. parts."""
    return (
        isinstance(value, str)
        and ABSOLUTE_PATH.fullmatch(value) is not None
        and value.rstrip("/") != ""
        and ".." not in value.split("/")
    )


def check_policy(checker: Checker, value: Any) -> dict[str, Any]:
    """Check the cluster-wide queue policy, filling in defaults."""
    policy = with_defaults(checker, value, "policy", POLICY_DEFAULTS)
    for key in (
        "max_submit_jobs_per_user",
        "default_cpus_per_gpu",
        "default_memory_mb_per_cpu",
        "fairshare_weight",
        "age_weight",
    ):
        checker.positive(policy[key], f"policy.{key}")
    checker.gpu_limit(policy["max_gpus_per_user"], "policy.max_gpus_per_user")
    for key in ("fairshare_half_life", "age_max"):
        checker.matches(policy[key], SLURM_TIME, f"policy.{key}", 'a Slurm time in quotes, like "7-00:00:00"')
    return policy


def whole(value: Any) -> bool:
    """Return whether `value` is an integer and not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def check_capacity(
    checker: Checker, machines: dict[str, dict[str, Any]], partitions: dict[str, dict[str, Any]], policy: dict[str, Any]
) -> None:
    """Reject limits that no compute machine can meet. Skipped values were already reported as errors."""
    compute = [machine for machine in machines.values() if "compute" in machine["roles"]]

    def gpus(machine: dict[str, Any]) -> int:
        gpu = machine.get("gpu")
        return gpu["count"] if isinstance(gpu, dict) and whole(gpu.get("count")) else 0

    for name, partition in partitions.items():
        limit = partition["max_gpus_per_user"]
        total = sum(gpus(machine) for machine in compute if name in (machine.get("partitions") or []))
        if whole(limit) and limit > total:
            checker.fail(
                f"partitions.{name}.max_gpus_per_user", f"{limit} is more than the {total} GPUs in the partition"
            )
    limit = policy["max_gpus_per_user"]
    total = sum(gpus(machine) for machine in compute)
    if whole(limit) and limit > total:
        checker.fail("policy.max_gpus_per_user", f"{limit} is more than the {total} GPUs in the cluster")
    memory = [machine["memory_mb"] for machine in compute if whole(machine.get("memory_mb"))]
    per_cpu = policy["default_memory_mb_per_cpu"]
    if memory and whole(per_cpu) and per_cpu > max(memory):
        checker.fail(
            "policy.default_memory_mb_per_cpu",
            f"{per_cpu} is more than the memory of any compute machine ({max(memory)})",
        )
    cpus = [cpu_count(machine) for machine in compute if gpus(machine) > 0]
    cpus = [count for count in cpus if count is not None]
    per_gpu = policy["default_cpus_per_gpu"]
    if cpus and whole(per_gpu) and per_gpu > max(cpus):
        checker.fail("policy.default_cpus_per_gpu", f"{per_gpu} is more than the CPUs of any GPU machine ({max(cpus)})")


def cpu_count(machine: dict[str, Any]) -> int | None:
    """Return a compute machine's logical CPU count, or None if its cpu field is invalid."""
    cpu = machine.get("cpu")
    if not isinstance(cpu, dict):
        return None
    parts = [cpu.get(key) for key in ("sockets", "cores_per_socket", "threads_per_core")]
    if not all(whole(part) and part > 0 for part in parts):
        return None
    return parts[0] * parts[1] * parts[2]


def check_backup(checker: Checker, value: Any, machines: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Check the backup section and that a backup machine, if any, is used."""
    backup_machines = [name for name, machine in machines.items() if "backup" in machine["roles"]]
    if value is None:
        for name in backup_machines:
            checker.fail(f"machines.{name}", "has the backup role but backup.to does not use it")
        return None
    backup = checker.mapping(value, "backup", ("to",), ("time", "exclude"))
    if backup is None:
        return None
    target = backup.get("to")
    if (
        "to" in backup
        and target not in backup_machines
        and not (
            isinstance(target, str) and SSH_TARGET.fullmatch(target) and is_directory_path(target.split(":", 1)[1])
        )
    ):
        checker.fail("backup.to", "must be a machine with the backup role or user@host:/path")
    for name in backup_machines:
        if target != name:
            checker.fail(f"machines.{name}", "has the backup role but backup.to does not use it")
    backup.setdefault("time", "03:00")
    checker.matches(backup["time"], CLOCK_TIME, "backup.time", 'HH:MM in quotes (24-hour clock), like "03:00"')
    backup.setdefault("exclude", [])
    checker.string_list(backup["exclude"], "backup.exclude")
    # The patterns go on the backup service's command line: no characters systemd or a shell would change.
    if isinstance(backup["exclude"], list):
        for index, pattern in enumerate(backup["exclude"]):
            if isinstance(pattern, str) and not EXCLUDE_PATTERN.fullmatch(pattern):
                checker.fail(
                    f"backup.exclude[{index}]", "must be a plain rsync pattern (letters, digits, . _ - / * ? [ ])"
                )
    return backup


def check_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a parsed cluster.yml. Return the config with defaults filled in, and the list of errors."""
    checker = Checker()
    if not isinstance(raw, Mapping):
        return {}, [TOP_SECTIONS]
    cluster = raw.get("cluster")
    if isinstance(cluster, Mapping) and cluster.get("mode") == "monitor":
        config = check_monitor_config(checker, raw)
        return config, checker.errors
    top = checker.mapping(raw, "", ("cluster", "machines", "users", "partitions"), TOP_FIELDS)
    if top is None or checker.errors:
        # The sections depend on each other: stop at missing or unknown sections to avoid follow-on errors.
        return {}, checker.errors
    users = check_users(checker, top.get("users"))
    partitions = check_partitions(checker, top.get("partitions"))
    machines = check_machines(checker, top.get("machines"), partitions)
    config: dict[str, Any] = {
        "cluster": check_cluster(
            checker, top.get("cluster"), {user["name"] for user in users if isinstance(user.get("name"), str)}, "slurm"
        ),
        "machines": machines,
        "users": users,
        "partitions": partitions,
        "policy": check_policy(checker, top.get("policy")),
        "home": with_defaults(checker, top.get("home"), "home", HOME_DEFAULTS),
        "scratch": with_defaults(checker, top.get("scratch"), "scratch", SCRATCH_DEFAULTS),
        "backup": check_backup(checker, top.get("backup"), machines),
        "alerts": with_defaults(checker, top.get("alerts"), "alerts", ALERTS_DEFAULTS),
        "auto_deploy": with_defaults(checker, top.get("auto_deploy"), "auto_deploy", AUTO_DEPLOY_DEFAULTS),
        "nanohpc_version": top.get("nanohpc_version"),
    }
    check_capacity(checker, machines, partitions, config["policy"])
    home = config["home"]
    for key in ("quota_soft_gb", "quota_hard_gb"):
        checker.positive(home[key], f"home.{key}")
    if (
        isinstance(home["quota_soft_gb"], int)
        and isinstance(home["quota_hard_gb"], int)
        and home["quota_soft_gb"] > home["quota_hard_gb"]
    ):
        checker.fail("home.quota_soft_gb", "must not be larger than home.quota_hard_gb")
    checker.matches(
        home["quota_grace"], re.compile(r"\d+(days|hours|minutes)"), "home.quota_grace", "a time like 7days"
    )
    checker.positive(config["scratch"]["cleanup_days"], "scratch.cleanup_days")
    checker.positive(config["scratch"]["job_retention_days"], "scratch.job_retention_days")
    checker.boolean(config["alerts"]["slack"], "alerts.slack")
    check_auto_deploy(checker, config)
    return config, checker.errors


def check_auto_deploy(checker: Checker, config: dict[str, Any]) -> None:
    """Check automatic deploys: a Git repository over SSH, a branch, minutes between checks, the optional
    webhook, and the nanoHPC version the front node installs."""
    deploy = config["auto_deploy"]
    version = config["nanohpc_version"]
    if version is not None and not (isinstance(version, str) and VERSION.fullmatch(version)):
        checker.fail("nanohpc_version", 'must be a version such as "0.1.0", in quotes')
    enabled = checker.boolean(deploy["enabled"], "auto_deploy.enabled")
    webhook = checker.boolean(deploy["webhook"], "auto_deploy.webhook")
    if webhook and not enabled:
        checker.fail("auto_deploy.webhook", "needs auto_deploy.enabled: true")
    if not enabled:
        return
    if not isinstance(deploy["repository"], str):
        checker.fail("auto_deploy.repository", "is required when auto_deploy.enabled is true")
    elif not GIT_SSH_URL.fullmatch(deploy["repository"]):
        checker.fail(
            "auto_deploy.repository", "must be a Git repository over SSH, like git@github.com:lab/cluster-config.git"
        )
    if version is None:
        checker.fail(
            "nanohpc_version", "is required when auto_deploy.enabled is true (the version the front node installs)"
        )
    if not (isinstance(deploy["branch"], str) and BRANCH_NAME.fullmatch(deploy["branch"])):
        checker.fail("auto_deploy.branch", "must be a branch name, like main or stable")
    minutes = deploy["every_minutes"]
    if not (isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= 1440):
        checker.fail("auto_deploy.every_minutes", "must be a whole number of minutes from 1 to 1440")


def load_config(path: Path, check_files: bool, automatic: bool) -> tuple[dict[str, Any], list[str]]:
    """Read and validate a cluster.yml file. Duplicate keys are reported as errors. With `check_files`, the
    website's certificate, key, and logo paths are resolved against the file's folder and must exist.
    `automatic` (an automatic deploy on the front node): the administrator's own certificate files are not
    there, so they are left out and the front node keeps the certificate it has."""
    loader = UniqueKeyLoader(path.read_text())
    raw = loader.get_single_data()
    loader.dispose()
    config, errors = check_config(raw)
    if automatic and not errors and config["cluster"]["website"]["https"] == "own":
        config["cluster"]["website"]["certificate"] = None
        config["cluster"]["website"]["certificate_key"] = None
    if check_files and not errors:
        errors = website_files(config["cluster"]["website"], path.parent) + secrets(config, path.parent)
    return config, loader.duplicates + errors


def read_env(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines of a .env file (blank lines and # comments skipped, optional quotes removed)."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def secrets(config: dict[str, Any], folder: Path) -> list[str]:
    """Put the secrets the configuration needs into config["secrets"], read from .env next to cluster.yml
    (never committed). Report missing or malformed ones."""
    env_path = (folder / ".env").resolve()
    env = read_env(env_path) if env_path.is_file() else {}
    # Read whether or not their feature is on yet, so the front node has them when a commit turns it on.
    webhook = env.get("NANOHPC_SLACK_WEBHOOK")
    deploy_secret = env.get("NANOHPC_DEPLOY_WEBHOOK_SECRET")
    config["secrets"] = {"slack_webhook": webhook, "deploy_webhook": deploy_secret}
    errors = []
    if config["alerts"]["slack"] is True and not webhook:
        errors.append(f"alerts.slack is true but {env_path} has no NANOHPC_SLACK_WEBHOOK")
    if webhook is not None and not re.fullmatch(r"https?://[^\s'\"]+", webhook):
        errors.append("NANOHPC_SLACK_WEBHOOK in .env must be an http(s) URL")
    if config.get("auto_deploy", {}).get("webhook") is True and not deploy_secret:
        errors.append(f"auto_deploy.webhook is true but {env_path} has no NANOHPC_DEPLOY_WEBHOOK_SECRET")
    if deploy_secret is not None and len(deploy_secret) < 32:
        errors.append(
            "NANOHPC_DEPLOY_WEBHOOK_SECRET in .env must be at least 32 characters (for example openssl rand -hex 32)"
        )
    return errors


LOGO_TYPES = (".png", ".svg", ".jpg", ".webp")


def website_files(website: dict[str, Any], folder: Path) -> list[str]:
    """Resolve the website's certificate, key, and logo paths (on the administrator's machine, relative to
    cluster.yml's folder) to absolute paths, and report missing files and unsupported logo types."""
    errors = []
    for key in ("certificate", "certificate_key", "logo"):
        if website[key] is None:
            continue
        path = (folder / Path(website[key]).expanduser()).resolve()
        website[key] = str(path)
        if not path.is_file():
            errors.append(f"cluster.website.{key}: {path} not found")
        elif key == "logo" and path.suffix.lower() not in LOGO_TYPES:
            errors.append("cluster.website.logo must be a .png, .svg, .jpg, or .webp file")
    if website["https"] == "own" and website["certificate"] is not None and not errors:
        errors = own_certificate_errors(website)
    return errors


def openssl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run openssl on the administrator's machine and return its result."""
    return subprocess.run(["openssl", *arguments], capture_output=True, text=True, check=False)


def own_certificate_errors(website: dict[str, Any]) -> list[str]:
    """Check the administrator's own certificate before any machine is touched: a PEM certificate, matching its
    key, for the website hostname (or a wildcard covering it), and not expired."""
    certificate, key = website["certificate"], website["certificate_key"]
    public = openssl(["x509", "-noout", "-pubkey", "-in", certificate])
    if public.returncode != 0:
        return ["cluster.website.certificate is not a PEM certificate"]
    key_public = openssl(["pkey", "-pubout", "-in", key])
    if key_public.returncode != 0:
        return ["cluster.website.certificate_key is not a PEM private key without a passphrase"]
    errors = []
    if key_public.stdout.strip() != public.stdout.strip():
        errors.append("cluster.website.certificate_key does not match cluster.website.certificate")
    text = openssl(["x509", "-noout", "-text", "-in", certificate]).stdout
    names = [name.lower() for name in re.findall(r"DNS:([^\s,]+)", text)]
    hostname = website["hostname"]
    wildcard = "*." + hostname.split(".", 1)[-1]
    if hostname not in names and wildcard not in names:
        errors.append(
            f"cluster.website.certificate is not for {hostname} (it names {', '.join(names) or 'no DNS name'})"
        )
    if openssl(["x509", "-noout", "-checkend", "0", "-in", certificate]).returncode != 0:
        errors.append("cluster.website.certificate has expired")
    return errors
