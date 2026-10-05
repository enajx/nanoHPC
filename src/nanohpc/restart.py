"""Restart one confirmed compute machine after its Slurm jobs have finished."""

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from nanohpc import restart_check
from nanohpc.config import load_config
from nanohpc.probe import run_remote, ssh_args, ssh_failure
from nanohpc.render import front_machine, gpu_count, home_server

LOCK = "/run/nanohpc-restart.lock"
WAIT_JOBS_SECONDS = 24 * 60 * 60
WAIT_BOOT_SECONDS = 60 * 60
POLL_JOBS_SECONDS = 60
POLL_BOOT_SECONDS = 5


class RestartError(RuntimeError):
    """A restart cannot continue; the node stays drained once a drain succeeded."""


def sudo_command(arguments: list[str]) -> str:
    """Run a root command through the administrator's forwarded SSH key without a prompt."""
    return "sudo -S -p '' " + shlex.join(arguments) + " </dev/null"


def read(target: str, ssh_config: Path | None, command: str, timeout: int) -> str:
    """Run a command over SSH and require it to succeed."""
    result = run_remote(target, ssh_config, command, True, timeout)
    failure = ssh_failure(target, result)
    if failure:
        raise RestartError(failure)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RestartError(f"{target}: {command.split()[0]} failed: {detail}")
    return result.stdout.strip()


def root(target: str, ssh_config: Path | None, arguments: list[str], timeout: int) -> str:
    """Run one root command on a machine over SSH."""
    return read(target, ssh_config, sudo_command(arguments), timeout)


def verify_address(config: dict[str, Any], machine: str, ssh_config: Path | None) -> None:
    """Confirm an SSH alias reaches the configured machine before changing Slurm or rebooting."""
    output = read(machine, ssh_config, "ip -4 -j addr show", 30)
    try:
        interfaces = json.loads(output)
    except json.JSONDecodeError as failure:
        raise RestartError(f"{machine}: cannot read live IPv4 addresses: {failure}") from failure
    if not isinstance(interfaces, list):
        raise RestartError(f"{machine}: cannot read live IPv4 addresses")
    live = {
        address.get("local")
        for interface in interfaces
        if isinstance(interface, dict)
        for address in interface.get("addr_info", [])
        if isinstance(address, dict)
    }
    expected = config["machines"][machine]["address"]
    if expected not in live:
        raise RestartError(f"{machine}: configured address {expected} is not on the SSH target")


def invoking_admin(config: dict[str, Any], machine: str, ssh_config: Path | None) -> str:
    """Use the administrator authenticated by the current SSH connection."""
    user = read(machine, ssh_config, "id -un", 30)
    if user not in config["cluster"]["admins"]:
        raise RestartError(f"{machine}: SSH user {user} is not an administrator in cluster.yml")
    return user


def lock(front: str, ssh_config: Path | None) -> None:
    """Allow one compute restart at a time across administrator machines."""
    root(front, ssh_config, ["mkdir", LOCK], 30)


def unlock(front: str, ssh_config: Path | None) -> None:
    """Release the cluster-wide restart lock."""
    root(front, ssh_config, ["rmdir", LOCK], 30)


def node_state(front: str, machine: str, ssh_config: Path | None) -> str:
    """Read the target's Slurm state before changing it."""
    output = root(front, ssh_config, ["scontrol", "show", "node", machine, "-o"], 30)
    match = re.search(r"\bState=([^\s]+)", output)
    if match is None:
        raise RestartError(f"{machine}: cannot read its Slurm state")
    return match[1]


def drain(front: str, machine: str, ssh_config: Path | None, reason: str) -> None:
    """Keep the compute machine unavailable to new Slurm jobs."""
    root(front, ssh_config, ["scontrol", "update", f"nodename={machine}", "state=drain", f"reason={reason}"], 30)


def resume(front: str, machine: str, ssh_config: Path | None) -> None:
    """Allow jobs on the checked machine for its smoke job."""
    root(front, ssh_config, ["scontrol", "update", f"nodename={machine}", "state=resume"], 30)


def reservation(machine: str) -> str:
    """Name the one-node reservation that keeps normal jobs away during the smoke job."""
    return f"nanohpc_restart_{machine}"


def reserve(front: str, machine: str, admin: str, ssh_config: Path | None) -> str:
    """Reserve the drained node for a single administrator's test job."""
    name = reservation(machine)
    root(
        front,
        ssh_config,
        [
            "scontrol",
            "create",
            "reservation",
            f"ReservationName={name}",
            f"Nodes={machine}",
            "StartTime=now",
            "Duration=infinite",
            f"Users={admin}",
            "Flags=MAINT,IGNORE_JOBS",
        ],
        30,
    )
    return name


def unreserve(front: str, name: str, ssh_config: Path | None) -> None:
    """Let ordinary jobs use a node only after its test job passed."""
    root(front, ssh_config, ["scontrol", "delete", f"ReservationName={name}"], 30)


def wait_for_jobs(front: str, machine: str, ssh_config: Path | None) -> None:
    """Wait for every job with a live node allocation; never cancel it."""
    deadline = time.monotonic() + WAIT_JOBS_SECONDS
    while True:
        jobs = root(
            front,
            ssh_config,
            ["squeue", "-h", "-w", machine, "-o", "%i"],
            30,
        )
        if not jobs:
            return
        if time.monotonic() >= deadline:
            raise RestartError(f"{machine}: jobs still run after 24 hours: {jobs}")
        print(f"{machine}: waiting for running jobs: {jobs}", flush=True)
        time.sleep(POLL_JOBS_SECONDS)


def before_restart(machine: str, ssh_config: Path | None, gpu: bool) -> str:
    """Require every read-only boot check to pass, then return GRUB's selected kernel."""
    facts, error = restart_check.read_machine(machine, ssh_config)
    if error or facts is None:
        raise RestartError(error or f"{machine}: restart checks returned no facts")
    findings = restart_check.evaluate(facts, gpu)
    problems = [f"{label}: {reason}" for label, reasons in findings.items() for reason in reasons]
    if problems:
        raise RestartError(f"{machine}: restart checks failed: " + "; ".join(problems))
    kernel, grub_problems = restart_check.grub_kernel(facts)
    if grub_problems or not kernel:
        raise RestartError(f"{machine}: GRUB's next kernel is unknown")
    return kernel


def boot_id(machine: str, ssh_config: Path | None) -> str:
    """Read the Linux boot ID so the restart must produce a new boot."""
    return read(machine, ssh_config, "cat /proc/sys/kernel/random/boot_id", 30)


def reboot(machine: str, ssh_config: Path | None, old_id: str) -> None:
    """Request a restart, then wait for SSH to return with a new Linux boot ID."""
    command = sudo_command(["systemctl", "reboot"])
    result = run_remote(machine, ssh_config, command, True, 30)
    fatal_ssh = (
        "permission denied",
        "host key verification failed",
        "remote host identification has changed",
        "could not resolve hostname",
        "bad configuration option",
    )
    if result.returncode not in (0, 255) or (
        result.returncode == 255 and any(reason in result.stderr.lower() for reason in fatal_ssh)
    ):
        raise RestartError(f"{machine}: reboot request failed: {result.stderr.strip() or result.stdout.strip()}")
    deadline = time.monotonic() + WAIT_BOOT_SECONDS
    while time.monotonic() < deadline:
        probe = run_remote(machine, ssh_config, "cat /proc/sys/kernel/random/boot_id", False, 30)
        if probe.returncode == 0 and probe.stdout.strip() and probe.stdout.strip() != old_id:
            return
        time.sleep(POLL_BOOT_SECONDS)
    raise RestartError(f"{machine}: did not return with a new boot ID within one hour")


def fresh_login(machine: str, user: str, ssh_config: Path | None) -> None:
    """Open a new SSH connection as root or the configured administrator."""
    arguments = ssh_args(ssh_config, machine, "true", False)
    command = [*arguments[:-2], "-o", "ControlPath=none", "-l", user, *arguments[-2:]]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode:
        raise RestartError(f"{machine}: fresh {user} login failed: {result.stderr.strip() or 'no output'}")


def after_restart(config: dict[str, Any], machine: str, ssh_config: Path | None, kernel: str, admin: str) -> None:
    """Check fresh access, kernel, GPUs, storage, and slurmd on the restarted machine."""
    fresh_login(machine, "root", ssh_config)
    fresh_login(machine, admin, ssh_config)
    running_kernel = read(machine, ssh_config, "uname -r", 30)
    if running_kernel != kernel:
        raise RestartError(f"{machine}: running kernel {running_kernel} differs from GRUB's {kernel}")
    expected_gpus = gpu_count(config["machines"][machine])
    if expected_gpus:
        found: list[str] = []
        gpu_error = ""
        for attempt in range(10):
            result = run_remote(machine, ssh_config, "nvidia-smi -L", False, 30)
            found = [line for line in result.stdout.splitlines() if line.startswith("GPU ")]
            gpu_error = result.stderr.strip()
            if result.returncode == 0 and len(found) == expected_gpus:
                break
            if attempt < 9:
                time.sleep(6)
        else:
            raise RestartError(
                f"{machine}: nvidia-smi saw {len(found)} GPUs, cluster.yml expects {expected_gpus}"
                + (f": {gpu_error}" if gpu_error else "")
            )
    server = home_server(config)
    expected_home = f"{config['machines'][server]['address']}:/home"
    home = read(machine, ssh_config, "ls /home >/dev/null && findmnt -n -o SOURCE -t nfs,nfs4 /home", 30)
    if home != expected_home:
        raise RestartError(f"{machine}: /home is {home}, expected {expected_home}")
    scratch = read(machine, ssh_config, "findmnt -n -o TARGET --mountpoint /scratch", 30)
    if scratch != "/scratch":
        raise RestartError(f"{machine}: /scratch is not mounted")
    service = "unknown"
    for attempt in range(10):
        result = run_remote(machine, ssh_config, "systemctl is-active slurmd", False, 30)
        service = result.stdout.strip() or result.stderr.strip() or f"exit code {result.returncode}"
        if result.returncode == 0 and service == "active":
            return
        if attempt < 9:
            time.sleep(6)
    raise RestartError(f"{machine}: slurmd is {service} after restart")


def batch_partition(config: dict[str, Any], machine: str) -> str | None:
    """Choose a batch-capable partition, if this machine has one."""
    for name in config["machines"][machine]["partitions"]:
        if config["partitions"][name]["jobs"] in ("batch", "any"):
            return name
    return None


def interactive_smoke(
    config: dict[str, Any], front: str, machine: str, name: str, admin: str, ssh_config: Path | None
) -> None:
    """Run the documented interactive shell as a Slurm job on a node with no batch partition."""
    gpus = gpu_count(config["machines"][machine])
    partition = config["machines"][machine]["partitions"][0]
    arguments = [
        "runuser",
        "-u",
        admin,
        "--",
        "srun",
        "--pty",
        f"--partition={partition}",
        f"--reservation={name}",
        f"--nodelist={machine}",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=1",
        "--mem=1G",
        "--time=00:05:00",
        "--job-name=nanohpc-restart-test",
    ]
    if gpus:
        arguments.append("--gpus=1")
    arguments.extend(["bash", "-l"])
    command = "sudo -S -p '' " + shlex.join(arguments)
    ssh = ssh_args(ssh_config, front, command, True)
    ssh = [*ssh[:-2], "-tt", *ssh[-2:]]
    script = "hostname -s\n" + ("nvidia-smi -L\n" if gpus else "") + "exit\n"
    result = subprocess.run(ssh, input=script, capture_output=True, text=True, check=False, timeout=30 * 60)
    lines = [line.strip() for line in result.stdout.splitlines()]
    hostname = read(machine, ssh_config, "hostname -s", 30)
    if result.returncode or hostname not in lines:
        raise RestartError(
            f"{machine}: interactive Slurm test job failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    if gpus:
        found = [line for line in lines if re.match(r"^GPU \d+:", line)]
        if not found:
            raise RestartError(f"{machine}: interactive test job saw no GPUs")


def smoke_job(config: dict[str, Any], front: str, machine: str, name: str, admin: str, ssh_config: Path | None) -> None:
    """Run a short Slurm job on the restarted node, then require accounting to show completion there."""
    gpus = gpu_count(config["machines"][machine])
    partition = batch_partition(config, machine)
    if partition is None:
        interactive_smoke(config, front, machine, name, admin, ssh_config)
        return
    command = [
        "runuser",
        "-u",
        admin,
        "--",
        "sbatch",
        "--wait",
        "--parsable",
        f"--nodelist={machine}",
        f"--partition={partition}",
        f"--reservation={name}",
        "--cpus-per-task=1",
        "--mem=1G",
        "--time=00:05:00",
        "--job-name=nanohpc-restart-test",
        "--chdir=/tmp",
        "--output=/dev/null",
    ]
    if gpus:
        command.append("--gpus=1")
    command.extend(["--wrap", "nvidia-smi -L" if gpus else "hostname -s"])
    job_id = root(front, ssh_config, command, 30 * 60).split(";")[0]
    if not job_id.isdigit():
        raise RestartError(f"{machine}: test job returned no job ID: {job_id}")
    state = ""
    for attempt in range(10):
        state = root(front, ssh_config, ["sacct", "-j", job_id, "-X", "-n", "-P", "-o", "State,NodeList"], 30)
        if state == f"COMPLETED|{machine}":
            return
        if state and state.split("|", 1)[0] not in ("PENDING", "CONFIGURING", "RUNNING", "COMPLETING"):
            break
        if attempt < 9:
            time.sleep(6)
    raise RestartError(f"{machine}: test job {job_id} finished as {state or 'unknown'}")


def execute(config: dict[str, Any], machine: str, ssh_config: Path | None) -> int:
    """Drain, wait, check, reboot, verify, and smoke-test one compute machine."""
    front = front_machine(config)[0]
    verify_address(config, front, ssh_config)
    verify_address(config, machine, ssh_config)
    admin = invoking_admin(config, machine, ssh_config)
    lock(front, ssh_config)
    locked = True
    drained = False
    reserved: str | None = None
    success = False
    error: str | None = None
    try:
        state = node_state(front, machine, ssh_config).upper()
        if any(flag in state for flag in ("DRAIN", "DOWN", "FAIL", "MAINT")):
            raise RestartError(f"{machine}: Slurm state is {state}; resolve that state before a restart")
        print(f"{machine}: draining in Slurm", flush=True)
        drain(front, machine, ssh_config, "restart by nanohpc")
        drained = True
        wait_for_jobs(front, machine, ssh_config)
        kernel = before_restart(machine, ssh_config, gpu_count(config["machines"][machine]) > 0)
        old_id = boot_id(machine, ssh_config)
        print(f"{machine}: restarting; GRUB selects {kernel}", flush=True)
        reboot(machine, ssh_config, old_id)
        verify_address(config, machine, ssh_config)
        after_restart(config, machine, ssh_config, kernel, admin)
        reserved = reserve(front, machine, admin, ssh_config)
        resume(front, machine, ssh_config)
        smoke_job(config, front, machine, reserved, admin, ssh_config)
        unreserve(front, reserved, ssh_config)
        reserved = None
        unlock(front, ssh_config)
        locked = False
        success = True
        print(f"{machine}: restart and Slurm test job passed; node resumed")
    except (RestartError, subprocess.TimeoutExpired) as failure:
        error = str(failure)
    finally:
        safe_to_unreserve = True
        if drained and not success:
            try:
                drain(front, machine, ssh_config, "restart by nanohpc failed; check by hand")
            except RestartError as failure:
                safe_to_unreserve = False
                error = f"{error or 'restart stopped'}; could not leave {machine} drained: {failure}"
        if reserved is not None and safe_to_unreserve:
            try:
                unreserve(front, reserved, ssh_config)
            except RestartError as failure:
                error = f"{error or 'restart stopped'}; could not remove reservation {reserved}: {failure}"
        if locked:
            try:
                unlock(front, ssh_config)
            except RestartError as failure:
                error = f"{error or 'restart stopped'}; could not remove restart lock: {failure}"
    if error:
        print(f"{machine}: {error}", file=sys.stderr)
        return 1
    return 0


def valid_target(config: dict[str, Any], machine: str, confirm: str) -> str | None:
    """Return why the request cannot restart exactly one named compute machine."""
    if machine != confirm:
        return f"--confirm must equal the one machine name {machine}"
    if config["cluster"].get("mode") == "monitor":
        return "restart needs a Slurm cluster, not monitor mode"
    selected = config["machines"].get(machine)
    if selected is None:
        return f"{machine} is not in cluster.yml"
    if "front" in selected["roles"] or "compute" not in selected["roles"]:
        return f"{machine} is not one compute machine; front-node restarts stay manual"
    return None


def run(path: Path, machine: str, confirm: str, ssh_config: Path | None) -> int:
    """Validate the cluster and confirmed compute target before any remote action."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, False, False)
    if errors:
        print(f"{path}: {len(errors)} error(s); no machine was restarted", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    problem = valid_target(config, machine, confirm)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        return execute(config, machine, ssh_config)
    except RestartError as failure:
        print(f"{machine}: {failure}", file=sys.stderr)
        return 1
