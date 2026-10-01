"""`nanohpc deploy`: set up a cluster from cluster.yml with Ansible.

nanoHPC reaches each machine with `ssh <machine name>`, so the administrator's SSH config decides
the user, address, and key (`--ssh-config` points at another SSH config file, as for the simulated
cluster). It reads each machine's real hostname first, generates every configuration file, then
runs the playbook in `nanohpc/ansible/`. Work files go to ~/.cache/nanohpc/clusters/<cluster name>/.
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from nanohpc.render import compute_machines, home_clients, home_server, render

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


def variables(config: dict[str, Any], work: Path, fake_gpus: list[str], qos: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the `nanohpc` variables the roles read."""
    policy_limit = config["policy"]["max_gpus_per_user"]
    gpus = {name: machine["gpu"]["count"] for name, machine in compute_machines(config).items() if machine.get("gpu")}
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
        }
    }


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
    for name, text in rendered.gres_conf.items():
        (files / "gres" / f"{name}.conf").write_text(text)
    (work / "vars.json").write_text(json.dumps(variables(config, work, fake_gpus, rendered.qos), indent=2))
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory(config)))
    (work / "ansible.cfg").write_text(ansible_cfg(ssh_config))


NO_TERMINAL = """Nothing was changed: sudo needs a password on {machines}, and no one can type it here (no terminal).
Options:
  - Run this one command yourself in a terminal; it asks for the password once, for this run only.
    In Claude Code, type:  ! nanohpc {arguments}
  - After the first deploy, administrators' SSH keys unlock sudo: deploy as an administrator from cluster.yml
    with your key loaded in your SSH agent (ssh-add). nanoHPC forwards it, so no password is needed."""


def deploy(config: dict[str, Any], ssh_config: Path | None, simulated: bool, fake_gpus: list[str]) -> int:
    """Set up the cluster. Return the exit code of the Ansible run."""
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
    prepare(config, found.hostnames, ssh_config, simulated, fake_gpus, work)
    playbook = Path(sys.executable).parent / "ansible-playbook"
    command = [
        str(playbook),
        "-i",
        str(work / "inventory.yml"),
        str(ANSIBLE / "site.yml"),
        "-e",
        f"@{work / 'vars.json'}",
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
        # Close the shared SSH connections now, so none keeps the forwarded agent open after the run.
        for socket in Path(private).iterdir():
            if socket.is_socket():
                close = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", "nanohpc"]
                subprocess.run(close, capture_output=True, check=False)
        return code
