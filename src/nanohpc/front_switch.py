"""`nanohpc front-status`, `nanohpc front-switch` and `nanohpc front-back`: make the backup front node (the login node
with `front_backup: true`) the front node, from the administrator's machine.

The switch shows what it found (does each machine answer, its Slurm controller, the age of the backup front node's
copies), asks the administrator to type the machine's name, and runs every dry run before changing anything: the
old front node's stop step (when it answers), the new front node's take step, and a deploy of the cluster as it
will be. Then: the old front node stops (fence held, last copies, front services stopped), the new one restores
the copies, cluster.yml swaps the two machines' roles (the old front node becomes the backup front node), and a
deploy sets up the new front node and points every machine at it. An old front node that does not answer is left
out of both deploys, as if it were not in cluster.yml; its fence keeps it quiet when it comes back. The Slack agent
is not set up by the switch (the next `nanohpc deploy` does). Users are told on Slack; the end prints the address to
log in and the DNS change to make.

`front-back` is the same move, planned: the current front node must answer, so its last copies (Slurm state,
accounting, and the monitoring history made meanwhile) reach the machine that takes over.
"""

import copy
import json
import shlex
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanohpc import clusterfile
from nanohpc.config import agent_files, secrets, website_files
from nanohpc.probe import run_remote, ssh_failure
from nanohpc.render import front_backup_machine, front_machine

STEP = "/usr/local/sbin/nanohpc-front-switch-step"
STEP_OPTIONS = [
    "--copy-run",
    "/usr/local/sbin/nanohpc-front-copy-run",
    "--fence-run",
    "/usr/local/sbin/nanohpc-front-fence-run",
]
STATUS_COMMAND = (
    "sudo -S -p '' sh -c 'echo now=$(date +%s); echo slurmctld=$(systemctl is-active slurmctld);"
    " echo state=$(cat /var/lib/nanohpc-front-copy/state/state.done 2>/dev/null);"
    " echo nightly=$(cat /var/lib/nanohpc-front-copy/nightly/nightly.done 2>/dev/null);"
    " echo fenced=$(cat /var/lib/nanohpc-front-fence/fenced 2>/dev/null)' </dev/null"
)
STEP_SECONDS = 4 * 60 * 60  # a step may send the nightly copy

# Runs `nanohpc deploy` of a configuration: (config, dry_run only) -> exit code.
Deploy = Callable[[dict[str, Any], bool], int]
# Asks the administrator a question and returns the typed answer.
Ask = Callable[[str], str]
# Posts a text with a Slack webhook: (webhook, text).
Post = Callable[[str, str], None]


@dataclass(frozen=True)
class Found:
    """What the switch reads from one machine, without changing anything."""

    answers: bool
    problem: str  # why it does not answer
    slurmctld: str
    fenced: str
    state_age: int | None  # seconds since the last Slurm state copy it received; None: none
    nightly_age: int | None


def read(machine: str, ssh_config: Path | None) -> Found:
    """Read a machine's Slurm controller, fence, and copies (read-only)."""
    result = run_remote(machine, ssh_config, STATUS_COMMAND, True, 30)
    failure = ssh_failure(machine, result)
    if failure is not None:
        return Found(False, failure, "", "", None, None)
    if result.returncode != 0:
        # It answers, but its state could not be read (for example sudo refused): never taken as down.
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        return Found(True, f"cannot read its state: {detail}", "", "", None, None)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    now = int(values["now"])

    def age(key: str) -> int | None:
        return now - int(values[key]) if values.get(key, "").strip().isdigit() else None

    return Found(True, "", values.get("slurmctld", ""), values.get("fenced", ""), age("state"), age("nightly"))


def shown_age(seconds: int | None) -> str:
    """An age in words."""
    if seconds is None:
        return "none yet"
    if seconds < 2 * 60 * 60:
        return f"{seconds // 60} minutes old"
    if seconds < 2 * 24 * 60 * 60:
        return f"{seconds // 3600} hours old"
    return f"{seconds // 86400} days old"


def describe(name: str, what: str, address: str, found: Found) -> list[str]:
    """Lines about one machine."""
    if not found.answers:
        return [f"{name} ({what}, {address}): does not answer ({found.problem})"]
    if found.problem:
        return [f"{name} ({what}, {address}): answers, but {found.problem}"]
    lines = [f"{name} ({what}, {address}): answers; Slurm controller {found.slurmctld}"]
    if found.fenced:
        lines.append(f"  fenced: {found.fenced}")
    lines.append(
        f"  copies it holds: Slurm state copy: {shown_age(found.state_age)}; nightly copy: {shown_age(found.nightly_age)}"
    )
    return lines


def pair(config: dict[str, Any]) -> tuple[str, str] | None:
    """The front node and the backup front node, or None without a backup front node."""
    backup = front_backup_machine(config)
    return None if backup is None else (front_machine(config)[0], backup)


def status_lines(config: dict[str, Any], ssh_config: Path | None) -> list[str]:
    """What front-status shows: both machines, read-only."""
    names = pair(config)
    if names is None:
        return ["No machine has front_backup: true in cluster.yml."]
    lines = []
    for name, what in zip(names, ("front node", "backup front node"), strict=True):
        lines += describe(name, what, config["machines"][name]["address"], read(name, ssh_config))
    return lines


def status(path: Path, ssh_config: Path | None) -> int:
    """`nanohpc front-status`: print what the switch would find."""
    config, errors = loaded(path)
    if errors:
        return 1
    print("\n".join(status_lines(config, ssh_config)))
    return 0


def loaded(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Read and check cluster.yml; print its errors."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return {}, ["missing"]
    file = clusterfile.load(path)
    errors = file.validate_in(path.parent)
    config, _ = file.checked()
    if not errors:
        secrets(config, path.parent)
        website_files(config["cluster"]["website"], path.parent)
        agent_files(config["agent"], path.parent)
    for error in errors:
        print(f"{path}: {error}", file=sys.stderr)
    return config, errors


def swapped(file: clusterfile.ClusterFile, front: str, backup: str) -> None:
    """cluster.yml after the switch: `backup` is the front node, `front` the backup front node."""
    file.set_value(["machines", front, "roles"], ["login"])
    file.set_value(["machines", front, "front_backup"], True)
    file.set_value(["machines", backup, "roles"], ["front"])
    with clusterfile.keep_end(file.data["machines"], backup):
        clusterfile.remove_item(file.data["machines"][backup], "front_backup")


def deploy_config(file: clusterfile.ClusterFile, folder: Path, away: str | None) -> tuple[dict[str, Any], list[str]]:
    """The configuration the switch deploys: cluster.yml after the switch, without the machine `away` (an old front
    node that does not answer), and without the Slack agent (the switch does not move it)."""
    text = copy.deepcopy(file)
    if away is not None:
        text.remove_machine(away)
    errors = text.validate_in(folder)
    config, _ = text.checked()
    if errors:
        return config, errors
    secrets(config, folder)
    website_files(config["cluster"]["website"], folder)
    agent_files(config["agent"], folder)
    config["agent"]["enabled"] = False
    return config, []


def step(machine: str, ssh_config: Path | None, arguments: list[str]) -> bool:
    """Run one switch step on a machine as root; print its output; True when it worked."""
    command = "sudo -S -p '' " + shlex.join([STEP, *arguments, *STEP_OPTIONS]) + " </dev/null"
    result = run_remote(machine, ssh_config, command, True, STEP_SECONDS)
    if result.stdout.strip():
        print("\n".join(f"  {machine}: {line}" for line in result.stdout.strip().splitlines()))
    failure = ssh_failure(machine, result)
    if failure is not None or result.returncode != 0:
        print(f"{machine}: {failure or result.stderr.strip() or f'exit code {result.returncode}'}", file=sys.stderr)
        return False
    return True


def post_to_slack(webhook: str, text: str) -> None:
    """Post a text with a Slack webhook; anything but Slack's "ok" is an error."""
    request = urllib.request.Request(
        webhook, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode()
    if body != "ok":
        raise RuntimeError(f"Slack answered: {body}")


def switch(
    path: Path, machine: str, ssh_config: Path | None, planned: bool, ask: Ask, deploy_run: Deploy, post: Post
) -> int:
    """Make `machine`, the backup front node, the front node (see the module's description). `planned`: front-back,
    which needs the current front node to answer. Return 0, or 1 when it stopped (it says whether anything changed)."""
    config, errors = loaded(path)
    if errors:
        return 1
    names = pair(config)
    if names is None or names[1] != machine:
        print(f"Nothing was changed: {machine} is not the backup front node in cluster.yml (front_backup: true).",
              file=sys.stderr)  # fmt: skip
        return 1
    front, backup = names
    if config["auto_deploy"]["enabled"] is True:
        print(
            "Nothing was changed: turn automatic deploys off first (auto_deploy.enabled: false in cluster.yml); turn"
            " them on again after the switch, once this cluster.yml is pushed to the configuration repository.",
            file=sys.stderr,
        )
        return 1
    front_found, backup_found = read(front, ssh_config), read(backup, ssh_config)
    print("\n".join(describe(front, "front node", config["machines"][front]["address"], front_found)
                    + describe(backup, "backup front node", config["machines"][backup]["address"], backup_found)))  # fmt: skip
    unread = [name for name, found in ((front, front_found), (backup, backup_found)) if found.answers and found.problem]
    if unread:
        print(f"Nothing was changed: the state of {', '.join(unread)} could not be read (see above).", file=sys.stderr)
        return 1
    if not backup_found.answers:
        print(f"Nothing was changed: {backup} does not answer.", file=sys.stderr)
        return 1
    if planned and not front_found.answers:
        print(f"Nothing was changed: moving back needs {front} to answer; use nanohpc front-switch if it is down.",
              file=sys.stderr)  # fmt: skip
        return 1
    plan = f"{backup} becomes the front node; {front} becomes the backup front node"
    plan += "" if front_found.answers else " (it does not answer: it is left out, and its fence keeps it quiet)"
    print(f"\n{plan}.")
    if ask(f"Type {backup} to go on: ").strip() != backup:
        print("Nothing was changed.", file=sys.stderr)
        return 1
    file = clusterfile.load(path)
    swapped(file, front, backup)
    away = None if front_found.answers else front
    new_config, errors = deploy_config(file, path.parent, away)
    if errors:
        print("Nothing was changed: cluster.yml after the switch would not be valid:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    peer = f"{config['machines'][front]['address']}:6817"
    print("\nDry runs, before changing anything:")
    if front_found.answers and not step(front, ssh_config, ["stop", "--to", backup, "--dry-run"]):
        print("Nothing was changed: the dry run failed.", file=sys.stderr)
        return 1
    if not step(backup, ssh_config, ["take", "--from", front, "--peer", peer, "--dry-run"]):
        print("Nothing was changed: the dry run failed.", file=sys.stderr)
        return 1
    if deploy_run(new_config, True) != 0:
        print("Nothing was changed: the deploy's dry run failed.", file=sys.stderr)
        return 1
    print("\nSwitching:")
    if front_found.answers and not step(front, ssh_config, ["stop", "--to", backup]):
        print(f"The switch stopped on {front} (see above); run it again once that is fixed.", file=sys.stderr)
        return 1
    if not step(backup, ssh_config, ["take", "--from", front, "--peer", peer]):
        print(f"The switch stopped on {backup} (see above); run it again once that is fixed.", file=sys.stderr)
        return 1
    file.save(path)
    print(f"{path}: {backup} is now the front node, {front} the backup front node.")
    if deploy_run(new_config, False) != 0:
        print(
            f"The switch stopped in the deploy: {path} already names {backup} as the front node. Fix what failed and"
            f" run nanohpc deploy {path}.",
            file=sys.stderr,
        )
        return 1
    return finish(new_config, backup, post)


def finish(config: dict[str, Any], backup: str, post: Post) -> int:
    """Print the login address and the DNS change, then tell users on Slack."""
    address = config["machines"][backup]["address"]
    website = config["cluster"]["website"]
    text = (
        f"The cluster's front node is now {backup}. Log in with ssh USER@{address}. The website"
        f" https://{website['hostname']}{website['path']} works again once its address is changed."
    )
    print(
        f"\nDone. Users log in with: ssh USER@{address}\n"
        f"Change the DNS record of {website['hostname']} to {address}. Until then the website answers at"
        f" https://{address}{website['path']} (browsers warn: the certificate names {website['hostname']}).\n"
        f"The Slack agent is not set up by the switch: nanohpc deploy sets it up on {backup}."
    )
    webhook = config["secrets"]["slack_webhook"]
    if config["alerts"]["slack"] is not True or not webhook:
        print("No Slack (alerts.slack): tell users yourself.")
        return 0
    # Last, so a refused post (it stops with Slack's answer) hides none of the above; the switch itself is done.
    print("Telling users on Slack (if this fails, the switch is still done: tell them yourself)...", flush=True)
    post(webhook, text)
    print("Users were told on Slack.")
    return 0
