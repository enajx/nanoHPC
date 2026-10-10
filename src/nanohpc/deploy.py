"""`nanohpc deploy`: set up a cluster from cluster.yml with Ansible.

nanoHPC reaches each machine with `ssh <machine name>`, so the administrator's SSH config decides
the user, address, and key (`--ssh-config` points at another SSH config file, as for the simulated
cluster). It reads each machine's real hostname first, generates every configuration file, then
runs the playbook in `nanohpc/ansible/`: first as a dry run (check mode; nothing changes except apt's package
lists), then for real on the machines whose dry run passed (none when the front node or the home machine failed). Work files go to ~/.cache/nanohpc/clusters/<cluster name>/.
A partial deploy (`--only`, see ONLY) runs ansible/partial.yml instead, with the same checks and dry run.
"""

import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import yaml

from nanohpc import maintenance_lock
from nanohpc.render import compute_machines, front_machine, home_clients, home_server, render, render_prometheus

SLURM: dict[str, Any] = {
    "version": "26.05.4",
    "url": "https://download.schedmd.com/slurm/slurm-26.05.4.tar.bz2",
    "sha256": "035f4b193d4de979ba5381beca206a50b6b886b2793b06f68a1ce7e67022b06a",
    "uid": 64030,  # fixed UID and GID of the slurm service account on every machine
    "packages": {
        "common": ["slurm-smd", "slurm-smd-client"],
        "front": ["slurm-smd-slurmctld", "slurm-smd-slurmdbd"],
        "compute": ["slurm-smd-slurmd"],
    },
}
UV: dict[str, Any] = {
    "version": "0.12.21",
    "sha256": {
        "x86_64": "23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0",
        "aarch64": "030b69227b40af8c1981b7301793dc66e71ed3c796ea8688209dd268bd91ec51",
    },
}
METRICS: dict[str, Any] = {
    "prometheus": {
        "version": "3.15.0",
        "sha256": {
            "amd64": "2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0",
            "arm64": "f1f90ec08e849d494ca66c611470afc50192f0355f1a61c33f2cbde02d067823",
        },
    },
    "node_exporter": {
        "version": "1.12.1",
        "sha256": {
            "amd64": "b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63",
            "arm64": "ad35b605f9954b9f1ffddf5ba054bdc5a98d790b9eae5291e1eeb83f1ecbd0e7",
        },
    },
    "alertmanager": {
        "version": "0.34.1",
        "sha256": {
            "amd64": "265b9d1e55ef0d5306a436018af6d2b686c2ce051f03d968f7464ecb1372a7e8",
            "arm64": "d98d6cbaf52151c7e76e24355fec88b11cebcb9875d4cdd8b76ddce7a7e5535c",
        },
    },
    "grafana": {
        "version": "13.2.3",
        "sha256": {
            "amd64": "6107ad27016296aac38e0d7ffa8753ab540b5541ad27e94790f771289d733235",
            "arm64": "a2a41b960ba4c25e83140484e48813730d3c81891a128439a2a9d2df6eb3840e",
        },
    },
}
CERTBOT: dict[str, Any] = {"version": "5.8.0"}
# Node for `website.build: front` (Node's architecture names: x64, arm64).
NODE: dict[str, Any] = {
    "version": "24.21.0",
    "sha256": {
        "x64": "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6",
        "arm64": "6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2",
    },
}
# Pebble, Let's Encrypt's test server, stands in for Let's Encrypt on a simulated cluster (its VMs have no
# public address). It runs on the front node; the sim setup installs it and its certificate authority.
TEST_ACME: dict[str, str] = {"server": "https://127.0.0.1:14000/dir", "ca_bundle": "/etc/nanohpc/test-acme/ca.pem"}
# Where the front node gets nanoHPC for automatic deploys when nothing else says (at tag v<version>).
NANOHPC_REPOSITORY = "https://github.com/enajx/nanoHPC"
CACHE = Path.home() / ".cache" / "nanohpc"
ANSIBLE = Path(str(resources.files("nanohpc").joinpath("ansible")))


def ssh_command(ssh_config: Path | None, machine: str, command: str) -> list[str]:
    """Return the ssh command line that runs `command` on a machine, without prompts.

    The SSH agent is forwarded (-A) so administrators' keys can unlock sudo (pam_ssh_agent_auth).
    """
    config = ["-F", str(ssh_config)] if ssh_config else []
    return ["ssh", *config, "-A", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", machine, command]


@dataclass(frozen=True)
class Probe:
    """What deploy learns from each machine before changing anything."""

    hostnames: dict[str, str]
    needs_password: list[str]  # machines where sudo asks for a password
    errors: list[str]  # machines that cannot be reached, or where the login account may not use sudo
    # The roles each machine was last deployed with (/etc/nanohpc/roles), None when it was never deployed.
    deployed: dict[str, list[str] | None]


def probe(machines: list[str], ssh_config: Path | None) -> Probe:
    """Read each machine's short hostname, whether sudo works without a password, and the roles it was deployed
    with. Changes nothing."""
    # Not `sudo -n`: it skips authentication, so a forwarded key (pam_ssh_agent_auth) would never be tried.
    # With an empty stdin, a key login succeeds and a password prompt fails at once.
    command = (
        "hostname -s; if out=$(sudo -S -p '' true </dev/null 2>&1); then echo sudo-ok;"
        " else case \"$out\" in *sudoers*|*'not allowed'*) echo sudo-denied;; *) echo sudo-password;; esac; fi;"
        ' if test -f /etc/nanohpc/roles; then echo "roles: $(cat /etc/nanohpc/roles)"; else echo not-deployed; fi'
    )

    def read(machine: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(ssh_command(ssh_config, machine, command), capture_output=True, text=True, check=False)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = dict(zip(machines, pool.map(read, machines), strict=True))
    hostnames: dict[str, str] = {}
    needs_password: list[str] = []
    errors: list[str] = []
    deployed: dict[str, list[str] | None] = {}
    for machine, result in results.items():
        lines = result.stdout.splitlines()
        if result.returncode != 0 or len(lines) != 3:
            errors.append(f"{machine}: cannot connect with `ssh {machine}`: {result.stderr.strip() or 'no output'}")
            continue
        hostnames[machine] = lines[0].strip()
        if lines[1] == "sudo-denied":
            errors.append(f"{machine}: the account `ssh {machine}` logs in with is not allowed to use sudo")
        elif lines[1] != "sudo-ok":
            needs_password.append(machine)
        deployed[machine] = lines[2].removeprefix("roles:").split() if lines[2].startswith("roles:") else None
    return Probe(hostnames, needs_password, errors, deployed)


@dataclass(frozen=True)
class Only:
    """A partial deploy (`--only`): the part, and for `node` the machine's name."""

    part: str
    node: str | None


@dataclass(frozen=True)
class Part:
    """What a partial deploy runs: its plays in ansible/partial.yml carry `tag`, and it runs on the machines that
    have one of `roles` (the others are not touched)."""

    tag: str
    roles: tuple[str, ...]
    what: str


# Each `--only` part. policy and partitions run the same plays: both live in slurm.conf, job_submit.lua, and the
# QoS. `node NAME` runs everything on NAME, and only the shared parts on the other machines.
EVERY_ROLE = ("front", "home", "backup", "compute", "shared")
ONLY: dict[str, Part] = {
    "users": Part(
        "users",
        EVERY_ROLE,
        "accounts, SSH keys, sudo keys, and who may log in on every machine; home folders and quotas; scratch folders"
        " and caches; Slurm accounting users; the status collector's user list; the website's site data (home quotas"
        " and scratch cleanup days) and the pages filled from it",
    ),
    "policy": Part(
        "policy",
        ("front", "compute"),
        "QoS, fair-share, and limits (slurm.conf, job_submit.lua, Slurm's QoS); Slurm restarts where they changed",
    ),
    "partitions": Part(
        "partitions",
        ("front", "compute"),
        "partitions and GPUs (slurm.conf and gres.conf on every Slurm machine, job_submit.lua, Slurm's QoS; Slurm"
        " restarts where they changed), and the website's policy data",
    ),
    "node": Part(
        "node",
        EVERY_ROLE,
        "everything on the machine, and the shared parts on the others: /etc/hosts, slurm.conf and Slurm's"
        " restart, the /home and /shared exports, the Prometheus targets, and the machine lists of the status collector and of"
        " automatic deploys",
    ),
}
ONLY_USAGE = "--only takes users, policy, partitions, or node NAME"


def parse_only(words: list[str]) -> tuple[Only | None, str | None]:
    """Return the partial deploy that `--only` asks for, or an error message."""
    if len(words) == 1 and words[0] in ONLY and words[0] != "node":
        return Only(words[0], None), None
    if len(words) == 2 and words[0] == "node":
        return Only("node", words[1]), None
    return None, f"{ONLY_USAGE} (got: {' '.join(words) or 'nothing'})"


def only_machines(config: dict[str, Any], only: Only) -> list[str]:
    """Return the machines a partial deploy runs on, in cluster.yml order."""
    roles = ONLY[only.part].roles
    return [name for name, machine in config["machines"].items() if set(machine["roles"]) & set(roles)]


FULL_DEPLOY = "run a full deploy (nanohpc deploy without --only)"


def only_refusal(config: dict[str, Any], only: Only) -> str | None:
    """Return why a partial deploy cannot be done, from cluster.yml alone, or None. Only a compute machine can be
    deployed alone: the other machines depend on the front node, the home machine, and the backup machine."""
    if only.node is None:
        return None
    if only.node not in config["machines"]:
        return f"{only.node} is not a machine in cluster.yml"
    roles = config["machines"][only.node]["roles"]
    if "compute" not in roles:
        return (
            f"{only.node} is not a compute machine (roles: {', '.join(roles)}), and the other machines depend on it:"
            f" {FULL_DEPLOY}"
        )
    return None


def deployed_problems(config: dict[str, Any], only: Only, deployed: dict[str, list[str] | None]) -> list[str]:
    """Return why a partial deploy is not safe on this cluster: every machine must have been deployed with its
    roles in cluster.yml, except the new machine of `--only node`, which may also be deployed for the first time."""
    problems = []
    for name, machine in config["machines"].items():
        roles = sorted(machine["roles"])
        recorded = deployed[name]
        if recorded is None:
            if name == only.node:
                continue
            if roles == ["compute"]:
                problems.append(
                    f"{name} has not been deployed yet: deploy it first with --only node {name}, or {FULL_DEPLOY}"
                )
            else:
                problems.append(f"{name} has not been deployed yet: {FULL_DEPLOY}")
        elif sorted(recorded) != roles:
            problems.append(
                f"{name} was deployed with the roles {', '.join(recorded)}, and cluster.yml gives it {', '.join(roles)}:"
                f" a change of roles affects the other machines, {FULL_DEPLOY}"
            )
    return problems


def inventory(config: dict[str, Any], node: str | None) -> dict[str, Any]:
    """Return the Ansible inventory: one group per role (role_front, ...), role_slurm for front and compute, and
    only_node: the machine of `--only node` (empty otherwise)."""
    # Ansible's temporary files on the cluster machines go to the login session's private runtime folder:
    # not the home folder (the shared /home or the home disk can hide it), and not a predictable /tmp path
    # another user could create first. Set on the role groups, which every machine is in, and not on `all`,
    # whose variables the administrator's own machine (localhost) also takes.
    machine_vars = {"ansible_remote_tmp": "$XDG_RUNTIME_DIR/ansible-tmp"}
    groups: dict[str, Any] = {
        f"role_{role}": {
            "hosts": {name: None for name, machine in config["machines"].items() if role in machine["roles"]},
            "vars": machine_vars,
        }
        for role in ("front", "home", "backup", "compute", "shared")
    }
    groups["role_slurm"] = {"children": {"role_front": None, "role_compute": None}}
    groups["only_node"] = {"hosts": {} if node is None else {node: None}}
    return {"all": {"children": groups}}


def monitor_inventory(config: dict[str, Any], node: str | None) -> dict[str, Any]:
    """Return groups for a monitoring host and the other monitored machines."""
    host = config["cluster"]["monitor_host"]
    machine_vars = {"ansible_remote_tmp": "$XDG_RUNTIME_DIR/ansible-tmp"}
    groups = {
        "role_front": {"hosts": {host: None}, "vars": machine_vars},
        "role_machine": {
            "hosts": {name: None for name in config["machines"] if name != host},
            "vars": machine_vars,
        },
        "only_node": {"hosts": {} if node is None else {node: None}},
    }
    return {"all": {"children": groups}}


def home_variables(config: dict[str, Any]) -> dict[str, Any]:
    """Return where /home is served, who mounts it, and users whose filesystem write limits must be cleared."""
    server = home_server(config)
    return {
        "server": server,
        "server_address": config["machines"][server]["address"],
        "device": config["machines"][server]["home"]["device"],
        "clients": home_clients(config),
        "quotas": [{"name": user["name"]} for user in config["users"]],
        "policy_user_args": " ".join(f"--user {user['name']}:{user['uid']}" for user in config["users"]),
        "soft_bytes": config["home"]["quota_soft_gb"] * 1024**3,
        "hard_bytes": config["home"]["quota_hard_gb"] * 1024**3,
    }


def shared_variables(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return the optional shared datasets server and its compute clients."""
    servers = [(name, machine) for name, machine in config["machines"].items() if "shared" in machine["roles"]]
    if not servers:
        return None
    name, machine = servers[0]
    return {
        "server": name,
        "server_address": machine["address"],
        "device": machine["shared"]["device"],
        "clients": list(compute_machines(config)),
    }


def variables(
    config: dict[str, Any],
    work: Path,
    simulated: bool,
    fake_gpus: list[str],
    automatic: bool,
    qos: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the `nanohpc` variables the roles read."""
    policy_limit = config["policy"]["max_gpus_per_user"]
    gpus = {name: machine["gpu"]["count"] for name, machine in compute_machines(config).items() if machine.get("gpu")}
    _, front_values = front_machine(config)
    return {
        "nanohpc": {
            "cluster_name": config["cluster"]["name"],
            # The nanoHPC version running this deploy, recorded on every machine (/etc/nanohpc/version).
            "version": metadata.version("nanohpc"),
            "users": [
                {
                    "name": u["name"],
                    "uid": u["uid"],
                    "ssh_keys": u["ssh_keys"],
                    "slack_id": u.get("slack_id", ""),
                }
                for u in config["users"]
            ],
            "admins": config["cluster"]["admins"],
            "machines": {
                name: {"address": m["address"], "roles": m["roles"]} for name, m in config["machines"].items()
            },
            "real_gpus": {name: count for name, count in gpus.items() if name not in fake_gpus},
            "fake_gpus": {name: count for name, count in gpus.items() if name in fake_gpus},
            "qos": qos,
            "max_submit_jobs_per_user": config["policy"]["max_submit_jobs_per_user"],
            "max_gpus_per_user": -1 if policy_limit == "unlimited" else policy_limit,
            "home": home_variables(config),
            "shared": shared_variables(config),
            "scratch": {
                "machines": {name: machine["scratch"] for name, machine in compute_machines(config).items()},
                "cleanup_days": config["scratch"]["cleanup_days"],
                "job_retention_days": config["scratch"]["job_retention_days"],
            },
            "files": str(work / "files"),
            "package_files": str(resources.files("nanohpc").joinpath("files")),
            "slurm": {**SLURM, "cache": str(CACHE / "slurm" / SLURM["version"])},
            "uv": UV,
            "website": website_variables(config, simulated),
            "backup": backup_variables(config),
            "auto_deploy": auto_deploy_variables(config, simulated, fake_gpus),
            # Run by the front node's automatic deploy (it keeps the install settings a manual deploy made).
            "automatic": automatic,
            "alerts": {"slack": config["alerts"]["slack"]},
            "user_check": config["user_check"],
            "metrics": {
                **METRICS,
                "front_address": front_values["address"],
                "gpus": {
                    name: {"count": m["gpu"]["count"], "type": m["gpu"]["type"], "fake": name in fake_gpus}
                    for name, m in compute_machines(config).items()
                    if m.get("gpu")
                },
            },
        }
    }


def website_variables(config: dict[str, Any], simulated: bool) -> dict[str, Any]:
    """Return what the website role needs: address, access, certificate, logo, and where the site files are."""
    website = config["cluster"]["website"]
    logo = website["logo"]
    source = Path(str(resources.files("nanohpc").joinpath("website-source")))
    return {
        "hostname": website["hostname"],
        "path": website["path"],
        "grafana_url": f"https://{website['hostname']}{website['path']}grafana/",
        "https": website["https"],
        "certificate": website["certificate"],
        "certificate_key": website["certificate_key"],
        "allow": website["allow"],
        "forwarded_by": website["forwarded_by"],
        "build": website["build"],
        "logo": None if logo is None else {"source": logo, "name": "logo" + Path(logo).suffix.lower()},
        "deploy_hook": config.get("auto_deploy", {}).get("webhook") is True,
        "certbot": CERTBOT,
        "acme": TEST_ACME if simulated and website["https"] == "letsencrypt" else None,
        "package": str(resources.files("nanohpc").joinpath("website")),
        "source": str(source),
        "source_hash": website_source_hash(source) if website["build"] == "front" else None,
        "node": NODE,
    }


WEBSITE_SOURCE_FILES = ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")
WEBSITE_SOURCE_FOLDERS = ("src", "public")


def install_source(direct_url: str | None) -> dict[str, str | None]:
    """Return how the front node installs nanoHPC for automatic deploys, from how this nanoHPC was installed
    (`direct_url` is the installed package's direct_url.json, None when it came from PyPI):
    pypi (nanohpc==<version>), git (that repository at tag v<version>), or wheel (a local checkout: the
    deploy builds a wheel and copies it). Anything else: the nanoHPC repository on GitHub."""
    if direct_url is None:
        return {"kind": "pypi", "url": None}
    info = json.loads(direct_url)
    if "vcs_info" in info:
        return {"kind": "git", "url": info["url"]}
    if "dir_info" in info:
        return {"kind": "wheel", "url": info["url"]}
    return {"kind": "git", "url": NANOHPC_REPOSITORY}


def refusal(config: dict[str, Any], automatic: bool, running_version: str) -> str | None:
    """Return why this deploy must not start, or None. With automatic deploys on, every deploy uses the
    nanoHPC version pinned in cluster.yml, so manual and automatic deploys never alternate between versions.
    An automatic deploy cannot turn automatic deploys off: it would remove the root login it runs on."""
    pinned = config["nanohpc_version"]
    if config["auto_deploy"]["enabled"] is True and pinned != running_version:
        return (
            f"cluster.yml pins nanoHPC {pinned} (nanohpc_version), but this is nanoHPC {running_version}: "
            f"deploy with nanoHPC {pinned}, or change nanohpc_version"
        )
    if automatic and config["auto_deploy"]["enabled"] is not True:
        return "this commit turns automatic deploys off: turn them off with nanohpc deploy from the administrator's machine"
    return None


def nanohpc_wheel(install: dict[str, str | None], version: str, work: Path) -> str | None:
    """For nanoHPC from a local checkout, build the wheel the front node installs into work/files/nanohpc-wheel.
    Return an error message when cluster.yml pins another version, None otherwise (and for other install kinds)."""
    if install["kind"] != "wheel":
        return None
    installed = metadata.version("nanohpc")
    if installed != version:
        return (
            f"cluster.yml pins nanoHPC {version}, but this nanoHPC (a local checkout) is {installed}: "
            f"set nanohpc_version to it, or deploy from nanoHPC {version}"
        )
    if shutil.which("uv") is None:
        return "building the nanoHPC wheel for the front node needs uv on this machine (https://docs.astral.sh/uv/)"
    project = Path(urllib.parse.unquote(urllib.parse.urlparse(str(install["url"])).path))
    folder = work / "files" / "nanohpc-wheel"
    if folder.exists():
        shutil.rmtree(folder)
    command = ["uv", "build", "--wheel", "--quiet", "--out-dir", str(folder), str(project)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return f"building the nanoHPC wheel from {project} failed:\n{result.stderr.strip()}"
    return None


def auto_deploy_variables(config: dict[str, Any], simulated: bool, fake_gpus: list[str]) -> dict[str, Any]:
    """Return what the auto_deploy role needs: the repository, branch, schedule, webhook, how to install the
    pinned nanoHPC version on the front node, and, on a simulated cluster, how `nanohpc sim deploy` deployed it
    (so the front node's automatic deploys do the same)."""
    deploy = config["auto_deploy"]
    repository = deploy["repository"]
    host = None
    if isinstance(repository, str):
        host = repository.removeprefix("ssh://").split("@", 1)[1].split(":", 1)[0].split("/", 1)[0]
    return {
        **deploy,
        "repository_host": host,
        "version": config["nanohpc_version"],
        "install": install_source(metadata.distribution("nanohpc").read_text("direct_url.json")),
        "simulated": simulated,
        "fake_gpus": fake_gpus,
    }


def backup_variables(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return where the nightly /home mirror goes: the cluster's backup machine (an unprivileged nanohpc-backup
    account there, into <backup path>/home), or an outside SSH server (user@host:/path)."""
    backup = config["backup"]
    if backup is None:
        return None
    target = backup["to"]
    common = {"time": backup["time"], "exclude": backup["exclude"]}
    if target in config["machines"]:
        address = config["machines"][target]["address"]
        folder = config["machines"][target]["backup"]["path"].rstrip("/") + "/home"
        return {
            "kind": "machine",
            "machine": target,
            "address": address,
            "destination": f"nanohpc-backup@{address}:{folder}/",
            "folder": folder,
            **common,
        }
    host = target.split("@", 1)[1].split(":", 1)[0]
    return {"kind": "outside", "address": host, "destination": target.rstrip("/") + "/", **common}


def website_source_hash(source: Path) -> str:
    """Return the hash of the website source, as its build records it (website-source/scripts/hash-source.mjs):
    SHA-256 over each file's relative path, a NUL, its length, a NUL, and its content, in path order."""
    paths = list(WEBSITE_SOURCE_FILES)
    for folder in WEBSITE_SOURCE_FOLDERS:
        paths += [
            str(path.relative_to(source))
            for path in (source / folder).rglob("*")
            if path.is_file() and path.name != ".DS_Store"
        ]
    digest = hashlib.sha256()
    for path in sorted(paths):
        content = (source / path).read_bytes()
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def site_data(config: dict[str, Any]) -> dict[str, Any]:
    """Return site.json: what the website shows that comes from cluster.yml (read by the page when it loads)."""
    website = config["cluster"]["website"]
    if config["cluster"].get("mode") == "monitor":
        return {
            "mode": "monitor",
            "cluster_name": config["cluster"]["name"],
            "logo": None if website["logo"] is None else "logo" + Path(website["logo"]).suffix.lower(),
            "login_address": website["login_address"],
            "users": config["users"],
        }
    return {
        "cluster_name": config["cluster"]["name"],
        "logo": None if website["logo"] is None else "logo" + Path(website["logo"]).suffix.lower(),
        "login_address": website["login_address"],
        "home_quota_soft_gb": config["home"]["quota_soft_gb"],
        "home_quota_hard_gb": config["home"]["quota_hard_gb"],
        "scratch_cleanup_days": config["scratch"]["cleanup_days"],
    }


def monitor_machines(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return, per machine, what the status collector checks: its role (front, compute, or storage), the
    services that must be active, and the mount points that must be writable with free space.

    Only services node_exporter's systemd collector reports can be listed (see the node_metrics role).
    """
    if config["cluster"].get("mode") == "monitor":
        return {
            name: {
                "role": "monitor" if name == config["cluster"]["monitor_host"] else "machine",
                "units": (
                    [
                        "nanohpc-node-exporter.service",
                        "nanohpc-prometheus.service",
                        "nanohpc-history.service",
                        "nanohpc-alertmanager.service",
                        "nanohpc-grafana.service",
                        "nanohpc-monitor-snapshot.timer",
                    ]
                    if name == config["cluster"]["monitor_host"]
                    else ["nanohpc-node-exporter.service"]
                ),
                "mounts": ["/"],
            }
            for name in config["machines"]
        }
    server = home_server(config)
    shared = shared_variables(config)
    clients = home_clients(config)
    result: dict[str, dict[str, Any]] = {}
    for name, machine in config["machines"].items():
        roles = machine["roles"]
        nfs = ["nfs-server.service"] if name == server or (shared is not None and name == shared["server"]) else []
        home = ["/home"] if name == server or name in clients else []
        shared_mount = []
        if shared is not None:
            if name == shared["server"]:
                shared_mount = ["/shared"]
            elif name in shared["clients"]:
                shared_mount = ["/shared/datasets"]
        if "front" in roles:
            units = ["slurmctld.service", "slurmdbd.service", "munge.service", "mariadb.service", *nfs]
            result[name] = {"role": "front", "units": units, "mounts": ["/", *home, *shared_mount]}
        elif "compute" in roles:
            units = ["slurmd.service", "munge.service"]
            result[name] = {"role": "compute", "units": units, "mounts": ["/", *home, "/scratch", *shared_mount]}
        else:
            result[name] = {"role": "storage", "units": nfs, "mounts": ["/", *home, *shared_mount]}
    return result


def ansible_cfg(ssh_config: Path | None) -> str:
    """Return ansible.cfg for the run."""
    ssh_args = "-o ControlMaster=auto -o ControlPersist=120s -o ForwardAgent=yes" + (
        f" -F {ssh_config}" if ssh_config else ""
    )
    return (
        "[defaults]\n"
        f"roles_path = {ANSIBLE / 'roles'}\n"
        "forks = 32\n"
        "timeout = 60\n"
        "interpreter_python = auto_silent\n"
        "retry_files_enabled = False\n"
        # Records the changed and failed tasks for the dry run's summary (callback_plugins/nanohpc_record.py).
        f"callback_plugins = {ANSIBLE / 'callback_plugins'}\n"
        "callbacks_enabled = nanohpc_record\n"
        "\n[privilege_escalation]\n"
        # Ansible's default adds -n, which skips authentication by forwarded key.
        "become_flags = -H -S\n"
        "\n[ssh_connection]\n"
        "pipelining = True\n"
        f"ssh_args = {ssh_args}\n"
    )


def prepare(
    config: dict[str, Any],
    hostnames: dict[str, str],
    ssh_config: Path | None,
    simulated: bool,
    fake_gpus: list[str],
    automatic: bool,
    node: str | None,
    work: Path,
) -> None:
    """Write the generated files, inventory, variables, and ansible.cfg into the work folder. `node`: the machine
    of `--only node`, None otherwise."""
    rendered = render(config, hostnames, simulated)
    files = work / "files"
    (files / "gres").mkdir(parents=True, exist_ok=True)
    (files / "slurm.conf").write_text(rendered.slurm_conf)
    (files / "cgroup.conf").write_text(rendered.cgroup_conf)
    (files / "job_submit.lua").write_text(rendered.job_submit_lua)
    (files / "hosts").write_text(rendered.hosts)
    (files / "exports").write_text(rendered.home_exports)
    (files / "prometheus.yml").write_text(rendered.prometheus_yml)
    (files / "monitor-machines.json").write_text(json.dumps(monitor_machines(config), indent=2))
    (files / "site.json").write_text(json.dumps(site_data(config), indent=2))
    for name, text in rendered.gres_conf.items():
        (files / "gres" / f"{name}.conf").write_text(text)
    # Secrets go in their own file that only this user can read, never in vars.json.
    secrets = work / "secrets.json"
    secrets.touch(mode=0o600)
    secrets.chmod(0o600)
    secrets.write_text(json.dumps({"nanohpc_secrets": config["secrets"]}))
    (work / "vars.json").write_text(
        json.dumps(variables(config, work, simulated, fake_gpus, automatic, rendered.qos), indent=2)
    )
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory(config, node)))
    (work / "ansible.cfg").write_text(ansible_cfg(ssh_config))


def prepare_monitor(
    config: dict[str, Any],
    ssh_config: Path | None,
    simulated: bool,
    gpus: dict[str, dict[str, Any]],
    work: Path,
    node: str | None,
) -> None:
    """Write only the monitoring inputs for a monitor-mode playbook."""
    files = work / "files"
    files.mkdir(parents=True, exist_ok=True)
    (files / "prometheus.yml").write_text(render_prometheus(config))
    (files / "monitor-machines.json").write_text(json.dumps(monitor_machines(config), indent=2))
    (files / "site.json").write_text(json.dumps(site_data(config), indent=2))
    secrets = work / "secrets.json"
    secrets.touch(mode=0o600)
    secrets.chmod(0o600)
    secrets.write_text(json.dumps({"nanohpc_secrets": config["secrets"]}))
    host = config["cluster"]["monitor_host"]
    values = {
        "mode": "monitor",
        "cluster_name": config["cluster"]["name"],
        "version": metadata.version("nanohpc"),
        "users": config["users"],
        "machines": {
            name: {"address": machine["address"], "roles": ["monitor" if name == host else "machine"]}
            for name, machine in config["machines"].items()
        },
        "files": str(files),
        "package_files": str(resources.files("nanohpc").joinpath("files")),
        "uv": UV,
        "website": website_variables(config, simulated),
        "alerts": {"slack": config["alerts"]["slack"]},
        "gpu_baseline": gpus,
        "metrics": {
            **METRICS,
            "front_address": config["machines"][host]["address"],
            "gpus": {
                name: {"count": gpu["count"], "type": gpu["models"][0], "fake": gpu["fake"]}
                for name, gpu in gpus.items()
                if gpu["count"] > 0
            },
        },
    }
    (work / "vars.json").write_text(json.dumps({"nanohpc": values}, indent=2))
    (work / "inventory.yml").write_text(yaml.safe_dump(monitor_inventory(config, node)))
    (work / "ansible.cfg").write_text(ansible_cfg(ssh_config))


MONITOR_GPU_PROBE = """\
import json, shutil, subprocess, sys
if shutil.which("nvidia-smi") is None:
    print(json.dumps({"count": 0, "models": [], "fake": False}))
else:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=25, check=False,
    )
    models = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not models:
        sys.stderr.write(result.stderr or "nvidia-smi did not report a GPU")
        sys.exit(1)
    print(json.dumps({"count": len(models), "models": models, "fake": False}))
"""


def probe_monitor_gpus(
    machines: list[str], ssh_config: Path | None, fake_gpus: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Discover GPU counts and models over SSH, keeping test-only fake GPUs outside cluster.yml."""
    command = "python3 -c " + shlex.quote(MONITOR_GPU_PROBE)

    def read(name: str) -> subprocess.CompletedProcess[str] | None:
        if name in fake_gpus:
            return None
        return subprocess.run(ssh_command(ssh_config, name, command), capture_output=True, text=True, check=False)

    with ThreadPoolExecutor(max_workers=32) as pool:
        found = dict(zip(machines, pool.map(read, machines), strict=True))
    inventory: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for name, result in found.items():
        if result is None:
            fake = fake_gpus[name]
            inventory[name] = {
                "count": fake["count"],
                "models": [fake["type"]] * fake["count"],
                "fake": True,
            }
        elif result.returncode != 0:
            errors.append(f"{name}: GPU discovery failed: {result.stderr.strip() or 'no output'}")
        else:
            inventory[name] = json.loads(result.stdout)
    return inventory, errors


def read_monitor_baseline(host: str, ssh_config: Path | None) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Read the monitoring host's accepted GPU inventory without changing it."""
    command = "if test -f /etc/nanohpc/monitor-gpus-cluster.json; then cat /etc/nanohpc/monitor-gpus-cluster.json; else echo null; fi"
    result = subprocess.run(ssh_command(ssh_config, host, command), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None, f"{host}: cannot read the accepted GPU inventory: {result.stderr.strip()}"
    return json.loads(result.stdout), None


def deploy_monitor(
    config: dict[str, Any],
    ssh_config: Path | None,
    simulated: bool,
    fake_gpus: dict[str, dict[str, Any]],
    dry_run_only: bool,
    only: Only | None,
    accept_hardware_change: str | None,
) -> int:
    """Deploy monitoring only, after checking mode conflicts and accepted GPU inventory."""
    names = list(config["machines"])
    host = config["cluster"]["monitor_host"]
    node = None if only is None else only.node
    if only is not None and (only.part != "node" or node not in config["machines"]):
        print(f"Nothing was changed: --only node needs a machine in cluster.yml (got {node}).", file=sys.stderr)
        return 1
    if accept_hardware_change is not None and accept_hardware_change not in config["machines"]:
        print(f"Nothing was changed: {accept_hardware_change} is not a machine in cluster.yml.", file=sys.stderr)
        return 1
    if node is not None and accept_hardware_change is not None and accept_hardware_change != node:
        print(
            "Nothing was changed: --accept-hardware-change must name the machine selected by --only node.",
            file=sys.stderr,
        )
        return 1
    selected = names if node is None else list(dict.fromkeys([host, node]))
    found = probe(selected, ssh_config)
    if found.errors:
        print("Nothing was changed: some machines cannot be used.", file=sys.stderr)
        for error in found.errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    incompatible = [name for name, roles in found.deployed.items() if roles and roles not in (["monitor"], ["machine"])]
    if incompatible:
        print(
            f"Nothing was changed: another nanoHPC setup is present on {', '.join(incompatible)}; monitor mode does not convert it.",
            file=sys.stderr,
        )
        return 1
    if node is not None and not found.deployed.get(host):
        print("Nothing was changed: deploy the monitoring host before --only node.", file=sys.stderr)
        return 1
    observed, errors = probe_monitor_gpus(selected, ssh_config, fake_gpus)
    if errors:
        print("Nothing was changed: GPU discovery failed.", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    recorded, error = read_monitor_baseline(host, ssh_config)
    if error is not None:
        print(f"Nothing was changed: {error}", file=sys.stderr)
        return 1
    if recorded is None and found.deployed.get(host):
        print("Nothing was changed: the monitoring host has no GPU inventory record.", file=sys.stderr)
        return 1
    baseline = {} if recorded is None else dict(recorded)
    mismatches = [
        name
        for name in selected
        if name in baseline and baseline[name] != observed[name] and name != accept_hardware_change
    ]
    if mismatches:
        print(
            "Nothing was changed: GPU inventory changed on "
            + ", ".join(mismatches)
            + "; inspect the machines, then use --accept-hardware-change NAME for an intentional change.",
            file=sys.stderr,
        )
        return 1
    for name in selected:
        if name not in baseline or name == accept_hardware_change:
            baseline[name] = observed[name]
    baseline = {name: baseline[name] for name in names if name in baseline}
    if found.needs_password and not sys.stdin.isatty():
        print(
            f"Nothing was changed: sudo needs a password on {', '.join(found.needs_password)}, and no terminal is available.",
            file=sys.stderr,
        )
        return 1
    password = (
        getpass.getpass(f"sudo password on {', '.join(found.needs_password)}: ") if found.needs_password else None
    )
    try:
        holder = maintenance_lock.acquire(host, ssh_config, password if host in found.needs_password else None)
    except maintenance_lock.MaintenanceLockError as failure:
        print(f"Nothing was changed: {failure}", file=sys.stderr)
        return 1
    try:
        with holder, maintenance_lock.active(holder):
            locked = probe(selected, ssh_config)
            locked_gpus, locked_errors = probe_monitor_gpus(selected, ssh_config, fake_gpus)
            locked_baseline, locked_error = read_monitor_baseline(host, ssh_config)
            if (
                locked.errors
                or locked_errors
                or locked_error is not None
                or locked != found
                or locked_gpus != observed
                or locked_baseline != recorded
            ):
                print(
                    "Nothing was changed: monitor facts changed while waiting for maintenance; retry deploy.",
                    file=sys.stderr,
                )
                return 1
            return _deploy_monitor_prepared(
                config, ssh_config, simulated, baseline, node, names, host, found, dry_run_only, password
            )
    except maintenance_lock.MaintenanceLockError as failure:
        print(f"Monitor deploy stopped: {failure}", file=sys.stderr)
        return 1


def _deploy_monitor_prepared(
    config: dict[str, Any],
    ssh_config: Path | None,
    simulated: bool,
    baseline: dict[str, dict[str, Any]],
    node: str | None,
    names: list[str],
    host: str,
    found: Probe,
    dry_run_only: bool,
    password: str | None,
) -> int:
    """Prepare and apply monitoring while holding the monitor host's maintenance lock."""
    work = CACHE / "clusters" / config["cluster"]["name"] / "monitor"
    work.mkdir(parents=True, exist_ok=True)
    prepare_monitor(config, ssh_config, simulated, baseline, work, node)
    machines = names if node is None else list(dict.fromkeys([node, host]))
    playbook = Path(sys.executable).parent / "ansible-playbook"
    command = [
        str(playbook),
        "-i",
        str(work / "inventory.yml"),
        str(ANSIBLE / "monitor.yml"),
        *([] if node is None else ["--limit", ",".join(machines)]),
        "-e",
        f"@{work / 'vars.json'}",
        "-e",
        f"@{work / 'secrets.json'}",
    ]
    with tempfile.TemporaryDirectory(prefix="nanohpc-monitor-", dir="/tmp") as private:
        environment = {
            **os.environ,
            "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
            "ANSIBLE_SSH_CONTROL_PATH_DIR": private,
        }
        if password is not None:
            password_file = Path(private) / "sudo"
            password_file.touch(mode=0o600)
            password_file.write_text(password)
            command += ["--become-password-file", str(password_file)]

        def run(arguments: list[str], record: Path) -> int:
            run_environment = {**environment, "NANOHPC_RECORD": str(record)}
            return maintenance_lock.guarded_run(
                [*command, *arguments],
                input="",
                capture_output=False,
                text=True,
                timeout=None,
                check=False,
                env=run_environment,
                cwd=None,
            ).returncode

        needed = {host: "the monitoring host"}
        if node is not None:
            needed[node] = "the machine of --only node"
        try:
            return check_then_apply(run, machines, needed, Path(private), dry_run_only)
        finally:
            (work / "secrets.json").unlink(missing_ok=True)
            for socket in Path(private).iterdir():
                if socket.is_socket():
                    subprocess.run(
                        ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", "nanohpc"],
                        capture_output=True,
                        check=False,
                    )


def only_words(only: Only) -> str:
    """Return a partial deploy as it is written after --only."""
    return only.part if only.node is None else f"node {only.node}"


def playbook_arguments(only: Only | None, machines: list[str]) -> list[str]:
    """Return the playbook to run and, for a partial deploy, its part's tag and machines."""
    if only is None:
        return [str(ANSIBLE / "site.yml")]
    return [str(ANSIBLE / "partial.yml"), "--tags", ONLY[only.part].tag, "--limit", ",".join(machines)]


NO_TERMINAL = """Nothing was changed: sudo needs a password on {machines}, and no one can type it here (no terminal).
Options:
  - Run this one command yourself in a terminal; it asks for the password once, for this run only.
    In Claude Code, type:  ! nanohpc {arguments}
  - After the first deploy, administrators' SSH keys unlock sudo: deploy as an administrator from cluster.yml
    with your key loaded in your SSH agent (ssh-add). nanoHPC forwards it, so no password is needed."""


def needed_machines(config: dict[str, Any], machines: list[str], node: str | None) -> dict[str, str]:
    """Return the machines the rest of a deploy depends on, among `machines` (the machines of this run), each with
    what it is: the front node, the home machine, the optional shared datasets server, and the machine of
    `--only node` (`node`), whose shared parts the other machines get."""
    front, _ = front_machine(config)
    home = home_server(config)
    needed = (
        {front: "the front node and the home machine"}
        if front == home
        else {front: "the front node", home: "the home machine"}
    )
    shared = shared_variables(config)
    if shared is not None:
        server = shared["server"]
        needed[server] = (
            f"{needed[server]} and the shared datasets server" if server in needed else "the shared datasets server"
        )
    if node is not None:
        needed[node] = "the machine of --only node"
    return {machine: what for machine, what in needed.items() if machine in machines}


def dry_run_summary(machines: list[str], record: dict[str, Any]) -> list[str]:
    """Return one line per machine: what the dry run would change there, or why it failed."""
    lines = []
    for machine in machines:
        changed = record["changed"].get(machine, [])
        if machine in record["failed"]:
            lines.append(f"Dry run: {machine} failed: {record['failed'][machine]}")
        elif changed:
            things = "thing" if len(changed) == 1 else "things"
            lines.append(f"Dry run: {machine} would change {len(changed)} {things}: {'; '.join(changed)}")
        else:
            lines.append(f"Dry run: {machine} has nothing to change")
    return lines


# Runs ansible-playbook with extra arguments, its callback writing the record file; returns the exit code.
PlaybookRunner = Callable[[list[str], Path], int]

# Exit codes of a deploy that ran its dry run (1: stopped before it, nothing changed).
DRY_RUN_FAILED = 3  # the dry run failed on some machines: those were not changed (the others may have been deployed)
REAL_RUN_FAILED = 4  # the real run failed: the machines it names may be partly changed
# The words the real run's pause stops with (roles/auto_deploy/tasks/pause.yml).
AUTO_DEPLOY_IN_BETWEEN = "An automatic deploy ran during this deploy's dry run"


def read_record(path: Path) -> dict[str, Any] | None:
    """Return a run's record (see callback_plugins/nanohpc_record.py), or None when the run left none."""
    return json.loads(path.read_text()) if path.is_file() else None


def run_with(
    run: PlaybookRunner, arguments: list[str], variables: dict[str, Any], folder: Path, name: str
) -> tuple[int, dict[str, Any] | None]:
    """Run the playbook with extra arguments and the variables (a file in `folder`); return its exit code and
    record. Only this run's record counts."""
    variables_path = folder / f"{name}-vars.json"
    variables_path.write_text(json.dumps(variables))
    record_path = folder / f"{name}.json"
    record_path.unlink(missing_ok=True)
    code = run([*arguments, "-e", f"@{variables_path}"], record_path)
    return code, read_record(record_path)


def check_then_apply(
    run: PlaybookRunner, machines: list[str], needed: dict[str, str], folder: Path, dry_run_only: bool
) -> int:
    """Run the playbook in check mode (the dry run: nothing changes except apt's package lists) and print what it
    would change on each machine. Then, unless `dry_run_only`, run it for real on the machines whose dry run
    passed; the others are left out, unchanged. When a machine in `needed` (the front node, the home machine, the
    machine of --only node: the rest of the deploy depends on them, named with what they are) fails its dry run,
    nothing is deployed.
    Return 0, DRY_RUN_FAILED, or REAL_RUN_FAILED. The records go into `folder`."""
    print("Dry run: checking every machine without changing anything (ansible-playbook --check)", flush=True)
    code, record = run_with(run, ["--check"], {"nanohpc_left_out": [], "nanohpc_dry_run": {}}, folder, "dry-run")
    if record is None:
        print(f"Dry run failed: nothing was changed. The dry run (exit code {code}) left no record.", file=sys.stderr)
        return DRY_RUN_FAILED
    print("\n".join(["", *dry_run_summary(machines, record), ""]), flush=True)
    failed = [machine for machine in machines if machine in record["failed"]]
    if code != 0 and not failed:
        print(
            f"Dry run failed (ansible-playbook exit code {code}): nothing was deployed, and nothing was changed.",
            file=sys.stderr,
        )
        return DRY_RUN_FAILED
    blocking = [f"{machine} ({needed[machine]})" for machine in failed if machine in needed]
    if blocking:
        print(
            f"Dry run failed on {', '.join(blocking)}, which the rest of this deploy depends on: nothing was deployed,"
            " and nothing was changed. Fix what failed above and deploy again.",
            file=sys.stderr,
        )
        return DRY_RUN_FAILED
    if dry_run_only:
        if failed:
            print(f"Dry run failed on {', '.join(failed)}. Nothing was changed (--dry-run).", file=sys.stderr)
            return DRY_RUN_FAILED
        print("Dry run passed. Nothing was changed (--dry-run).")
        return 0
    if failed:
        print(
            f"Dry run failed on {', '.join(failed)}: left out of this deploy, unchanged. Applying the changes on the"
            " others.",
            flush=True,
        )
    else:
        print("Dry run passed: applying the changes.", flush=True)
    variables = {"nanohpc_left_out": failed, "nanohpc_dry_run": record["facts"]}
    code, applied = run_with(run, [], variables, folder, "run")
    if applied is None:
        print(
            f"The deploy failed (exit code {code}) and left no record: machines may be partly changed.", file=sys.stderr
        )
        return REAL_RUN_FAILED
    broken = {machine: text for machine, text in applied["failed"].items() if machine not in failed}
    # The real run's pause stops before any change when an automatic deploy ran during the dry run.
    out_of_date = [text for text in broken.values() if AUTO_DEPLOY_IN_BETWEEN in text]
    if out_of_date:
        print(f"{out_of_date[0]}\n{left_out_list(failed, record)}", file=sys.stderr)
        return DRY_RUN_FAILED
    if broken or (code != 0 and not failed):
        lines = [f"  {machine}: {text}" for machine, text in broken.items()] or [
            f"  (ansible-playbook exit code {code})"
        ]
        print(
            "The deploy failed on these machines, which may be partly changed. Fix the problem and deploy again:\n"
            + "\n".join(lines)
            + ("\n" + left_out_list(failed, record) if failed else ""),
            file=sys.stderr,
        )
        return REAL_RUN_FAILED
    if failed:
        print(left_out_list(failed, record), file=sys.stderr)
        return DRY_RUN_FAILED
    return 0


def left_out_list(failed: list[str], record: dict[str, Any]) -> str:
    """Return the closing list of the machines left out after their dry run failed (empty when none)."""
    if not failed:
        return ""
    return "Left out of this deploy (their dry run failed; nothing was changed on them):\n" + "\n".join(
        f"  {machine}: {record['failed'][machine]}" for machine in failed
    )


def deploy(
    config: dict[str, Any],
    ssh_config: Path | None,
    simulated: bool,
    fake_gpus: list[str],
    automatic: bool,
    dry_run_only: bool,
    only: Only | None,
) -> int:
    """Set up the cluster: a dry run first, then the real run on the machines whose dry run passed (unless
    `dry_run_only`). Return 0, 1 (stopped before the dry run), DRY_RUN_FAILED, or REAL_RUN_FAILED.
    `automatic`: run by the front node's automatic deploy. `only`: a partial deploy (None: the whole cluster)."""
    refused = refusal(config, automatic, metadata.version("nanohpc"))
    if refused is None and only is not None:
        refused = only_refusal(config, only)
    if refused is not None:
        print(f"Nothing was changed: {refused}", file=sys.stderr)
        return 1
    if automatic:
        inherited = os.environ.get("NANOHPC_MAINTENANCE_FD", "")
        try:
            maintenance_lock.validate_inherited(int(inherited), maintenance_lock.LOCK_PATH)
        except (ValueError, maintenance_lock.MaintenanceLockError) as failure:
            print(f"Nothing was changed: automatic deploy has no valid maintenance lock: {failure}", file=sys.stderr)
            return 1
    found = probe(list(config["machines"]), ssh_config)
    if found.errors:
        print("Nothing was changed: some machines cannot be used.", file=sys.stderr)
        for error in found.errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    problems = [] if only is None else deployed_problems(config, only, found.deployed)
    if only is not None and problems:
        print(f"Nothing was changed: --only {only_words(only)} cannot be done safely here.", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if found.needs_password and not sys.stdin.isatty():
        arguments = " ".join(sys.argv[1:])
        print(NO_TERMINAL.format(machines=", ".join(found.needs_password), arguments=arguments), file=sys.stderr)
        return 1
    password: str | None = None
    if found.needs_password:
        prompt = (
            f"sudo password on {', '.join(found.needs_password)}, asked once for this run "
            "(administrators from cluster.yml can load their SSH key with ssh-add instead): "
        )
        password = getpass.getpass(prompt)
    if automatic:
        return _deploy_prepared(
            config, ssh_config, simulated, fake_gpus, automatic, dry_run_only, only, found, password
        )
    front = front_machine(config)[0]
    try:
        holder = maintenance_lock.acquire(front, ssh_config, password if front in found.needs_password else None)
    except maintenance_lock.MaintenanceLockError as failure:
        print(f"Nothing was changed: {failure}", file=sys.stderr)
        return 1
    try:
        with holder, maintenance_lock.active(holder):
            locked = probe(list(config["machines"]), ssh_config)
            if locked.errors or locked != found:
                print(
                    "Nothing was changed: machine facts changed while waiting for maintenance; retry deploy.",
                    file=sys.stderr,
                )
                return 1
            return _deploy_prepared(
                config, ssh_config, simulated, fake_gpus, automatic, dry_run_only, only, locked, password
            )
    except maintenance_lock.MaintenanceLockError as failure:
        print(f"Deploy stopped: {failure}", file=sys.stderr)
        return 1


def _deploy_prepared(
    config: dict[str, Any],
    ssh_config: Path | None,
    simulated: bool,
    fake_gpus: list[str],
    automatic: bool,
    dry_run_only: bool,
    only: Only | None,
    found: Probe,
    password: str | None,
) -> int:
    """Prepare inputs and run Ansible while the coordinator maintenance lock is held."""
    work = CACHE / "clusters" / config["cluster"]["name"]
    work.mkdir(parents=True, exist_ok=True)
    # The front node's nanoHPC for automatic deploys is installed by a full deploy only.
    if config["auto_deploy"]["enabled"] is True and only is None:
        install = install_source(metadata.distribution("nanohpc").read_text("direct_url.json"))
        error = nanohpc_wheel(install, config["nanohpc_version"], work)
        if error is not None:
            print(f"Nothing was changed: {error}", file=sys.stderr)
            return 1
    prepare(
        config, found.hostnames, ssh_config, simulated, fake_gpus, automatic, None if only is None else only.node, work
    )
    machines = list(config["machines"]) if only is None else only_machines(config, only)
    playbook = Path(sys.executable).parent / "ansible-playbook"
    command = [
        str(playbook),
        "-i",
        str(work / "inventory.yml"),
        *playbook_arguments(only, machines),
        "-e",
        f"@{work / 'vars.json'}",
        "-e",
        f"@{work / 'secrets.json'}",
    ]
    if only is not None:
        print(
            f"Partial deploy (--only {only_words(only)}) on {', '.join(machines)}: {ONLY[only.part].what}.", flush=True
        )
    # A private folder for this run only (0700, short path under /tmp for SSH sockets, removed at the end).
    # Ansible's shared SSH connections live here, so a run never reuses a connection from an earlier run
    # that forwarded a different (maybe stopped) SSH agent.
    with tempfile.TemporaryDirectory(prefix="nanohpc-", dir="/tmp") as private:
        environment = {
            **os.environ,
            "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
            "ANSIBLE_SSH_CONTROL_PATH_DIR": private,
        }
        if password is not None:
            # Ansible reads it from a file that only this user can read, deleted when the run ends.
            password_file = Path(private) / "sudo"
            password_file.touch(mode=0o600)
            password_file.write_text(password)
            command += ["--become-password-file", str(password_file)]

        def run(arguments: list[str], record: Path) -> int:
            # Ansible refuses non-blocking terminal handles, so it gets a plain stdin.
            run_environment = {**environment, "NANOHPC_RECORD": str(record)}
            return maintenance_lock.guarded_run(
                [*command, *arguments],
                input="",
                capture_output=False,
                text=True,
                timeout=None,
                check=False,
                env=run_environment,
                cwd=None,
            ).returncode

        try:
            return check_then_apply(
                run,
                machines,
                needed_machines(config, machines, None if only is None else only.node),
                Path(private),
                dry_run_only,
            )
        finally:
            # No copy of the secrets stays in the work folder (the real run put them on the front node).
            (work / "secrets.json").unlink(missing_ok=True)
            # Close shared SSH connections so none keeps the forwarded agent open after this run.
            for socket in Path(private).iterdir():
                if socket.is_socket():
                    close = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", "nanohpc"]
                    subprocess.run(close, capture_output=True, check=False)
