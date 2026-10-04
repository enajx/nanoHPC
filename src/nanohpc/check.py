"""`nanohpc check`: report how every machine compares with cluster.yml, without changing anything.

Each machine gets one SSH call (in parallel, with a time limit), as `nanohpc deploy` reaches it: `ssh <machine
name>`, the SSH agent forwarded so an administrator's key can unlock sudo, and never a password prompt. The call runs
a small Python program that only reads: the cluster-health report (as root with `sudo` when it works without a
password, else as the login user), the users' accounts (getent), the mounts (findmnt), the GPUs (nvidia-smi, or the
simulated cluster's placeholder /dev/nvidiaN devices when there is no nvidia-smi), the nanoHPC version the last
deploy recorded (/etc/nanohpc/version), and on the front node the Slurm node states (sinfo).

The report is a table, one line per machine (machines with problems first), then the problems, the warnings, and
notes. A problem is something that differs from cluster.yml or is broken (a FAIL line of cluster-health); a warning
is a state to look at (a WARN line of cluster-health, a drained Slurm node). Exit code: 0 without problems, 1 with.
"""

import json
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanohpc.config import load_config
from nanohpc.probe import account_problems, run_remote, ssh_failure
from nanohpc.render import front_machine, gpu_count, home_server

CHECK_TIMEOUT = 200  # seconds for the whole SSH call to one machine
HEALTH_TIMEOUT = 150  # seconds for cluster-health on the machine (it limits its own slow checks to 10 seconds each)
TOOL_TIMEOUT = 30  # seconds for each other tool on the machine
COLUMNS = ("machine", "ssh", "health", "users", "mounts", "gpus", "slurm", "version")
# Slurm node states that need no attention (sinfo %T, lowercase).
SLURM_OK = ("idle", "mixed", "allocated", "completing")

# Runs on the machine; it only reads. `timeout` stops a tool that hangs (an NFS mount that does not answer), so the
# other facts still arrive. The sudo check is the deploy's: an empty stdin, so a forwarded key unlocks sudo and a
# password prompt fails at once (`sudo -n` would never try the forwarded key).
CHECK_PROGRAM = r"""
import json, os, pwd, re, shutil, subprocess, sys

ARGUMENTS = json.loads(sys.argv[1])
ENVIRONMENT = dict(os.environ, LC_ALL="C")
SUDO = ["sudo", "-S", "-p", ""]

def run(command, seconds):
    result = subprocess.run(["timeout", str(seconds), *command], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, env=ENVIRONMENT)
    return {"code": result.returncode, "out": result.stdout, "err": result.stderr}

def read(path):
    if not os.path.exists(path):
        return None
    with open(path) as file:
        return file.read().strip()

health = shutil.which("cluster-health")
sudo = run([*SUDO, "true"], ARGUMENTS["tool_timeout"])["code"] == 0
facts = {
    "user": pwd.getpwuid(os.getuid()).pw_name,
    "sudo": sudo,
    "health": None if health is None else run([*(SUDO if sudo else []), health], ARGUMENTS["health_timeout"]),
    "version": read("/etc/nanohpc/version"),
    "passwd": run(["getent", "passwd", *ARGUMENTS["keys"]], ARGUMENTS["tool_timeout"]),
    "group": run(["getent", "group", *ARGUMENTS["keys"]], ARGUMENTS["tool_timeout"]),
    "mounts": run(["findmnt", "-r", "-n", "-o", "TARGET,SOURCE,FSTYPE"], ARGUMENTS["tool_timeout"]),
    "nvidia_smi": None,
    "gpu_devices": len([name for name in os.listdir("/dev") if re.fullmatch(r"nvidia[0-9]+", name)]),
    "sinfo": None,
}
if shutil.which("nvidia-smi"):
    facts["nvidia_smi"] = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], ARGUMENTS["tool_timeout"])
if ARGUMENTS["sinfo"]:
    facts["sinfo"] = run(["sinfo", "-h", "-N", "-o", "%N %T"], ARGUMENTS["tool_timeout"])
print(json.dumps(facts))
"""


@dataclass(frozen=True)
class Report:
    """What `nanohpc check` found on one machine: the table cells (by column) and what to list below the table."""

    cells: dict[str, str]
    problems: list[str]
    warnings: list[str]
    notes: list[str]


def read_machine(
    name: str, ssh_config: Path | None, keys: list[str], sinfo: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one machine's facts over one SSH call (read-only). Return the facts, or None and why it failed."""
    arguments = {"keys": keys, "sinfo": sinfo, "tool_timeout": TOOL_TIMEOUT, "health_timeout": HEALTH_TIMEOUT}
    command = f"python3 -c {shlex.quote(CHECK_PROGRAM)} {shlex.quote(json.dumps(arguments))}"
    result = run_remote(name, ssh_config, command, True, CHECK_TIMEOUT)
    failure = ssh_failure(name, result)
    if failure is not None:
        return None, failure
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return None, f"the check could not run on {name}: {lines[-1] if lines else 'no output'}"
    return json.loads(result.stdout), None


def first_line(output: dict[str, Any]) -> str:
    """Return the first line a tool printed (errors first), or 'no output'."""
    lines = (output["err"] + output["out"]).strip().splitlines()
    return lines[0] if lines else "no output"


def health_result(facts: dict[str, Any], report: Report) -> None:
    """Fill the health cell from the cluster-health report: FAIL lines are problems, WARN lines warnings."""
    health = facts["health"]
    lines = health["out"].splitlines()
    fails = [line.removeprefix("FAIL").strip() for line in lines if line.startswith("FAIL")]
    warns = [line.removeprefix("WARN").strip() for line in lines if line.startswith("WARN")]
    report.problems.extend(f"cluster-health: FAIL {line}" for line in fails)
    report.warnings.extend(f"cluster-health: WARN {line}" for line in warns)
    finished = health["code"] == 0 or (health["code"] == 1 and bool(fails))
    if not finished:
        report.problems.append(f"cluster-health did not finish (exit code {health['code']}): {first_line(health)}")
    state = "FAIL" if fails or not finished else "WARN" if warns else "ok"
    report.cells["health"] = f"{state} (sudo)" if facts["sudo"] else f"{state} (as {facts['user']})"
    if not facts["sudo"]:
        report.notes.append(
            f"sudo does not work without a password, so cluster-health ran as {facts['user']}: "
            "some checks may differ from root's"
        )


def users_result(facts: dict[str, Any], users: list[tuple[str, int]], name: str, report: Report) -> None:
    """Fill the users cell: every user of cluster.yml has an account, with the UID and group ID cluster.yml says."""
    problems: list[str] = []
    for database in ("passwd", "group"):
        # getent: 0 all found, 2 some key not found.
        if facts[database]["code"] not in (0, 2):
            problems.append(f"getent {database} failed: {first_line(facts[database])}")
    if not problems:
        accounts = [line.split(":") for line in facts["passwd"]["out"].splitlines() if line]
        groups = [line.split(":") for line in facts["group"]["out"].splitlines() if line]
        found = {entry[0] for entry in accounts}
        problems = [f"{user} has no account" for user, _ in users if user not in found]
        problems += account_problems(name, users, accounts, groups)
    report.problems.extend(problems)
    report.cells["users"] = "FAIL" if problems else "ok"


def mounts_result(facts: dict[str, Any], config: dict[str, Any], name: str, report: Report) -> None:
    """Fill the mounts cell: /home as cluster.yml says (served here, or the NFS mount from the home machine; none
    on the backup machine) and /scratch on compute machines."""
    if facts["mounts"]["code"] != 0:
        report.problems.append(f"findmnt failed: {first_line(facts['mounts'])}")
        report.cells["mounts"] = "FAIL"
        return
    # findmnt -r: one line per mount, fields split by one space. A later line for the same mount point is
    # mounted on top of the earlier one.
    lines = [line.split(" ") for line in facts["mounts"]["out"].splitlines() if line]
    mounts = {fields[0]: (fields[1], fields[2]) for fields in lines}
    roles = config["machines"][name]["roles"]
    server = home_server(config)
    shared = f"{config['machines'][server]['address']}:/home"
    problems: list[str] = []
    home = mounts.get("/home")
    if "home" in roles:
        if home is None:
            problems.append("/home is not mounted (cluster.yml: this machine serves /home)")
        elif home[1].startswith("nfs"):
            problems.append(f"/home is the NFS mount {home[0]}, but cluster.yml says this machine serves /home")
    elif "backup" not in roles:
        if home is None:
            problems.append(f"/home is not mounted (cluster.yml: the NFS mount {shared})")
        elif home != (shared, "nfs4"):
            problems.append(f"/home is {home[0]} ({home[1]}), but cluster.yml says the NFS mount {shared}")
    if "compute" in roles and "/scratch" not in mounts:
        problems.append("/scratch is not mounted")
    report.problems.extend(problems)
    checked = "home" in roles or "backup" not in roles or "compute" in roles
    report.cells["mounts"] = "FAIL" if problems else "ok" if checked else "-"


def gpus_result(facts: dict[str, Any], machine: dict[str, Any], report: Report) -> None:
    """Fill the gpus cell of a compute machine: the GPUs found against cluster.yml's count."""
    expected = gpu_count(machine)
    smi = facts["nvidia_smi"]
    if smi is not None:
        if smi["code"] != 0:
            report.problems.append(f"nvidia-smi fails: {first_line(smi)}")
            report.cells["gpus"] = f"?/{expected}"
            return
        found = len([line for line in smi["out"].splitlines() if line.strip()])
        report.cells["gpus"] = f"{found}/{expected}"
        if found != expected:
            report.problems.append(f"nvidia-smi sees {found} GPUs, cluster.yml says {expected}")
        return
    # No nvidia-smi: the simulated cluster's placeholder devices stand in for GPUs.
    devices = facts["gpu_devices"]
    if devices == expected:
        report.cells["gpus"] = f"{devices}/{expected} (devices)" if expected else "0/0"
        if expected:
            report.notes.append(
                "no nvidia-smi: counted the placeholder GPU devices /dev/nvidiaN (as on the simulated cluster)"
            )
        return
    report.cells["gpus"] = f"{devices}/{expected}"
    report.problems.append(
        f"no nvidia-smi (NVIDIA driver not installed?), {devices} /dev/nvidiaN devices; cluster.yml says {expected} GPUs"
    )


def slurm_states(front: dict[str, Any] | None) -> tuple[dict[str, set[str]] | None, str | None]:
    """Return each Slurm node's states from the front node's sinfo (None when they are not known: the front node
    could not be read or is not deployed), and why sinfo failed, or None."""
    if front is None or front["health"] is None:
        return None, None
    sinfo = front["sinfo"]
    if sinfo["code"] != 0:
        return None, f"sinfo failed: {first_line(sinfo)}"
    states: dict[str, set[str]] = {}
    for line in sinfo["out"].splitlines():
        if line.strip():
            node, state = line.split(maxsplit=1)
            states.setdefault(node, set()).add(state.strip())
    return states, None


def slurm_result(states: dict[str, set[str]] | None, name: str, front_name: str, report: Report) -> None:
    """Fill the slurm cell of a compute machine from the node states sinfo gave."""
    if states is None:
        report.cells["slurm"] = "?"
        return
    if name not in states:
        report.problems.append(f"not a Slurm node (sinfo on {front_name} does not list it)")
        report.cells["slurm"] = "missing"
        return
    text = ",".join(sorted(states[name]))
    report.cells["slurm"] = text
    if any(state not in SLURM_OK for state in states[name]):
        report.warnings.append(f"the Slurm node is {text}")


def machine_report(
    config: dict[str, Any],
    name: str,
    facts: dict[str, Any] | None,
    failure: str | None,
    states: dict[str, set[str]] | None,
) -> Report:
    """Compare one machine's facts with cluster.yml."""
    machine = config["machines"][name]
    compute = "compute" in machine["roles"]
    unknown = {column: "?" for column in COLUMNS[1:]}
    report = Report({"machine": name, **unknown}, [], [], [])
    if facts is None:
        report.cells["ssh"] = "FAIL"
        report.problems.append(str(failure))
        return report
    report.cells["ssh"] = "ok"
    if facts["health"] is None:
        report.cells["health"] = "not deployed"
        report.problems.append("not deployed yet (cluster-health is not installed)")
        return report
    health_result(facts, report)
    users_result(facts, [(user["name"], user["uid"]) for user in config["users"]], name, report)
    mounts_result(facts, config, name, report)
    report.cells["gpus"] = "-"
    report.cells["slurm"] = "-"
    if compute:
        gpus_result(facts, machine, report)
        slurm_result(states, name, front_machine(config)[0], report)
    report.cells["version"] = facts["version"] or "unknown"
    return report


def table(reports: list[Report]) -> str:
    """Return the reports as a table with aligned columns, a header first."""
    rows = [list(COLUMNS)] + [[report.cells[column] for column in COLUMNS] for report in reports]
    widths = [max(len(row[index]) for row in rows) for index in range(len(COLUMNS))]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows)


def listing(title: str, items: list[tuple[str, str]]) -> str:
    """Return a titled list of (machine, text) lines, or '' when there are none."""
    if not items:
        return ""
    return f"\n\n{title} ({len(items)}):\n" + "\n".join(f"  {machine}: {text}" for machine, text in items)


def check(config: dict[str, Any], ssh_config: Path | None) -> int:
    """Read every machine in parallel, print the report, and return the exit code (0: no problems, 1: problems)."""
    names = list(config["machines"])
    front_name = front_machine(config)[0]
    keys = [user["name"] for user in config["users"]] + [str(user["uid"]) for user in config["users"]]

    def read(name: str) -> tuple[dict[str, Any] | None, str | None]:
        return read_machine(name, ssh_config, keys, name == front_name)

    with ThreadPoolExecutor(max_workers=32) as pool:
        found = dict(zip(names, pool.map(read, names), strict=True))
    states, sinfo_failure = slurm_states(found[front_name][0])
    reports = {name: machine_report(config, name, *found[name], states) for name in names}
    if sinfo_failure is not None:
        reports[front_name].problems.append(sinfo_failure)
    ordered = [reports[n] for n in names if reports[n].problems] + [
        reports[n] for n in names if not reports[n].problems
    ]
    problems = [(report.cells["machine"], text) for report in ordered for text in report.problems]
    warnings = [(report.cells["machine"], text) for report in ordered for text in report.warnings]
    notes = [(report.cells["machine"], text) for report in ordered for text in report.notes]
    output = table(ordered) + listing("Problems", problems) + listing("Warnings", warnings) + listing("Notes", notes)
    print(output + ("" if problems else "\n\nNo problems."))
    return 1 if problems else 0


def run(path: Path, ssh_config: Path | None) -> int:
    """`nanohpc check`: validate cluster.yml, then check every machine against it."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    # The check reads no certificate or logo file.
    config, errors = load_config(path, False, False)
    if errors:
        print(f"{path}: {len(errors)} error(s); no machine was checked", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    return check(config, ssh_config)
