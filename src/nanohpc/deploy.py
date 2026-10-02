"""`nanohpc deploy`: set up a cluster from cluster.yml with Ansible.

nanoHPC reaches each machine with `ssh <machine name>`, so the administrator's SSH config decides
the user, address, and key (`--ssh-config` points at another SSH config file, as for the simulated
cluster). It reads each machine's real hostname first, generates every configuration file, then
runs the playbook in `nanohpc/ansible/`. Work files go to ~/.cache/nanohpc/clusters/<cluster name>/.
"""

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import yaml

from nanohpc.render import compute_machines, front_machine, home_clients, home_server, render

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


def probe(machines: list[str], ssh_config: Path | None) -> Probe:
    """Read each machine's short hostname and whether sudo works without a password. Changes nothing."""
    # Not `sudo -n`: it skips authentication, so a forwarded key (pam_ssh_agent_auth) would never be tried.
    # With an empty stdin, a key login succeeds and a password prompt fails at once.
    command = (
        "hostname -s; if out=$(sudo -S -p '' true </dev/null 2>&1); then echo sudo-ok;"
        " else case \"$out\" in *sudoers*|*'not allowed'*) echo sudo-denied;; *) echo sudo-password;; esac; fi"
    )

    def read(machine: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(ssh_command(ssh_config, machine, command), capture_output=True, text=True, check=False)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = dict(zip(machines, pool.map(read, machines), strict=True))
    hostnames: dict[str, str] = {}
    needs_password: list[str] = []
    errors: list[str] = []
    for machine, result in results.items():
        lines = result.stdout.split()
        if result.returncode != 0 or len(lines) != 2:
            errors.append(f"{machine}: cannot connect with `ssh {machine}`: {result.stderr.strip() or 'no output'}")
            continue
        hostnames[machine] = lines[0]
        if lines[1] == "sudo-denied":
            errors.append(f"{machine}: the account `ssh {machine}` logs in with is not allowed to use sudo")
        elif lines[1] != "sudo-ok":
            needs_password.append(machine)
    return Probe(hostnames, needs_password, errors)


def inventory(config: dict[str, Any]) -> dict[str, Any]:
    """Return the Ansible inventory: one group per role (role_front, ...), and role_slurm for front and compute."""
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
        for role in ("front", "home", "backup", "compute")
    }
    groups["role_slurm"] = {"children": {"role_front": None, "role_compute": None}}
    return {"all": {"children": groups}}


def home_variables(config: dict[str, Any]) -> dict[str, Any]:
    """Return where /home is served, who mounts it, and each user's quota in setquota's 1 KiB blocks."""
    server = home_server(config)
    home = config["home"]
    seconds = {"days": 86400, "hours": 3600, "minutes": 60}  # the units the validator accepts
    unit = next(unit for unit in seconds if home["quota_grace"].endswith(unit))
    grace = int(home["quota_grace"].removesuffix(unit)) * seconds[unit]
    return {
        "server": server,
        "server_address": config["machines"][server]["address"],
        "device": config["machines"][server]["home"]["device"],
        "clients": home_clients(config),
        "quotas": [
            {
                "name": user["name"],
                "soft_kib": home["quota_soft_gb"] * 1024 * 1024,
                "hard_kib": home["quota_hard_gb"] * 1024 * 1024,
            }
            for user in config["users"]
        ],
        "grace_seconds": grace,
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
            "users": [{"name": u["name"], "uid": u["uid"], "ssh_keys": u["ssh_keys"]} for u in config["users"]],
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
            "scratch": {
                "machines": {name: machine["scratch"] for name, machine in compute_machines(config).items()},
                "cleanup_days": config["scratch"]["cleanup_days"],
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
        "deploy_hook": config["auto_deploy"]["webhook"] is True,
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
    server = home_server(config)
    clients = home_clients(config)
    result: dict[str, dict[str, Any]] = {}
    for name, machine in config["machines"].items():
        roles = machine["roles"]
        nfs = ["nfs-server.service"] if name == server else []
        home = ["/home"] if name == server or name in clients else []
        if "front" in roles:
            units = ["slurmctld.service", "slurmdbd.service", "munge.service", "mariadb.service", *nfs]
            result[name] = {"role": "front", "units": units, "mounts": ["/", *home]}
        elif "compute" in roles:
            units = ["slurmd.service", "munge.service"]
            result[name] = {"role": "compute", "units": units, "mounts": ["/", *home, "/scratch"]}
        else:
            result[name] = {"role": "storage", "units": nfs, "mounts": ["/", *home]}
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
    work: Path,
) -> None:
    """Write the generated files, inventory, variables, and ansible.cfg into the work folder."""
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
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory(config)))
    (work / "ansible.cfg").write_text(ansible_cfg(ssh_config))


NO_TERMINAL = """Nothing was changed: sudo needs a password on {machines}, and no one can type it here (no terminal).
Options:
  - Run this one command yourself in a terminal; it asks for the password once, for this run only.
    In Claude Code, type:  ! nanohpc {arguments}
  - After the first deploy, administrators' SSH keys unlock sudo: deploy as an administrator from cluster.yml
    with your key loaded in your SSH agent (ssh-add). nanoHPC forwards it, so no password is needed."""


def deploy(
    config: dict[str, Any], ssh_config: Path | None, simulated: bool, fake_gpus: list[str], automatic: bool
) -> int:
    """Set up the cluster. Return the exit code of the Ansible run. `automatic`: run by the front node's
    automatic deploy."""
    refused = refusal(config, automatic, metadata.version("nanohpc"))
    if refused is not None:
        print(f"Nothing was changed: {refused}", file=sys.stderr)
        return 1
    found = probe(list(config["machines"]), ssh_config)
    if found.errors:
        print("Nothing was changed: some machines cannot be used.", file=sys.stderr)
        for error in found.errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    if found.needs_password and not sys.stdin.isatty():
        arguments = " ".join(sys.argv[1:])
        print(NO_TERMINAL.format(machines=", ".join(found.needs_password), arguments=arguments), file=sys.stderr)
        return 1
    work = CACHE / "clusters" / config["cluster"]["name"]
    work.mkdir(parents=True, exist_ok=True)
    if config["auto_deploy"]["enabled"] is True:
        install = install_source(metadata.distribution("nanohpc").read_text("direct_url.json"))
        error = nanohpc_wheel(install, config["nanohpc_version"], work)
        if error is not None:
            print(f"Nothing was changed: {error}", file=sys.stderr)
            return 1
    prepare(config, found.hostnames, ssh_config, simulated, fake_gpus, automatic, work)
    playbook = Path(sys.executable).parent / "ansible-playbook"
    command = [
        str(playbook),
        "-i",
        str(work / "inventory.yml"),
        str(ANSIBLE / "site.yml"),
        "-e",
        f"@{work / 'vars.json'}",
        "-e",
        f"@{work / 'secrets.json'}",
    ]
    # A private folder for this run only (0700, short path under /tmp for SSH sockets, removed at the end).
    # Ansible's shared SSH connections live here, so a run never reuses a connection from an earlier run
    # that forwarded a different (maybe stopped) SSH agent.
    with tempfile.TemporaryDirectory(prefix="nanohpc-", dir="/tmp") as private:
        environment = {
            **os.environ,
            "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
            "ANSIBLE_SSH_CONTROL_PATH_DIR": private,
        }
        if found.needs_password:
            prompt = (
                f"sudo password on {', '.join(found.needs_password)}, asked once for this run "
                "(administrators from cluster.yml can load their SSH key with ssh-add instead): "
            )
            # Ansible reads it from a file that only this user can read, deleted when the run ends.
            password_file = Path(private) / "sudo"
            password_file.touch(mode=0o600)
            password_file.write_text(getpass.getpass(prompt))
            command += ["--become-password-file", str(password_file)]
        # Ansible refuses non-blocking terminal handles, so it gets a plain stdin.
        code = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL, check=False).returncode
        # The secrets are on the front node now; no copy stays in the work folder.
        (work / "secrets.json").unlink()
        # Close the shared SSH connections now, so none keeps the forwarded agent open after the run.
        for socket in Path(private).iterdir():
            if socket.is_socket():
                close = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", "nanohpc"]
                subprocess.run(close, capture_output=True, check=False)
        return code
