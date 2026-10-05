"""The `nanohpc` command."""

import argparse
import importlib.metadata
import os
import shutil
import sys
from pathlib import Path

from nanohpc import check, deploy, fixuid, probe, restart_check, sim
from nanohpc.config import load_config
from nanohpc.render import render_forwarding_rules

ONLY_HELP = (
    "deploy only one part, with the same checks and dry run: users, policy, partitions, or node NAME (everything on"
    " a new or changed compute machine, and the shared parts on the others)"
)


def validate(path: Path) -> int:
    """Check a cluster.yml, print every error, and return the exit code."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, True, False)
    if errors:
        print(f"{path}: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    machines = config["machines"].values()
    compute = (
        sum("compute" in machine["roles"] for machine in machines) if config["cluster"].get("mode") != "monitor" else 0
    )
    print(f"{path}: valid (machines: {len(machines)}, compute: {compute}, users: {len(config['users'])})")
    return 0


def read_only(words: list[str] | None) -> tuple[deploy.Only | None, bool]:
    """Return the partial deploy that `--only` asks for (None without --only), and whether the words were right;
    print the error when they were not."""
    if words is None:
        return None, True
    only, error = deploy.parse_only(words)
    if error is not None:
        print(error, file=sys.stderr)
        return None, False
    return only, True


def run_deploy(path: Path, ssh_config: Path | None, dry_run_only: bool, only_arguments: list[str] | None) -> int:
    """Validate a cluster.yml, then set up the cluster it describes (after a dry run; only the dry run with
    `dry_run_only`), or only one part of it (`only_arguments`, the words after --only)."""
    only, right = read_only(only_arguments)
    if not right:
        return 1
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    # Set by the front node's automatic deploy (nanohpc-auto-deploy).
    automatic = os.environ.get("NANOHPC_AUTOMATIC") == "1"
    config, errors = load_config(path, True, automatic)
    if errors:
        print(f"{path}: {len(errors)} error(s); nothing was changed", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    if config["cluster"].get("mode") == "monitor":
        print("This cluster.yml uses monitor mode: run nanohpc deploy-monitor instead.", file=sys.stderr)
        return 1
    # A simulated cluster's front node deploys itself like `nanohpc sim deploy` (set on its automatic deploy
    # service only on simulated clusters; test use).
    simulated = os.environ.get("NANOHPC_SIMULATED") == "1"
    fake_gpus = [name for name in os.environ.get("NANOHPC_FAKE_GPUS", "").split(",") if name] if simulated else []
    return deploy.deploy(config, ssh_config, simulated, fake_gpus, automatic, dry_run_only, only)


def run_deploy_monitor(
    path: Path,
    ssh_config: Path | None,
    dry_run_only: bool,
    only_arguments: list[str] | None,
    accept_hardware_change: str | None,
) -> int:
    """Validate and deploy monitoring without Slurm or account management."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, True, False)
    if errors:
        print(f"{path}: {len(errors)} error(s); nothing was changed", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    if config["cluster"].get("mode") != "monitor":
        print("This cluster.yml uses Slurm mode: run nanohpc deploy instead.", file=sys.stderr)
        return 1
    only, right = read_only(only_arguments)
    if not right:
        return 1
    if only is not None and only.part != "node":
        print("Monitor mode --only supports node NAME only.", file=sys.stderr)
        return 1
    return deploy.deploy_monitor(config, ssh_config, False, {}, dry_run_only, only, accept_hardware_change)


def forwarding_rules(path: Path) -> int:
    """Print the rules a lab's own web server needs to show the cluster website under the same path."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    # The rules need no certificate or logo file.
    config, errors = load_config(path, False, False)
    if errors:
        print(f"{path}: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    if config["cluster"]["website"]["path"] == "/":
        print(
            f"{path}: the website uses the whole hostname (path /); to show it on a lab website,"
            " set cluster.website.path, for example /cluster/",
            file=sys.stderr,
        )
        return 1
    print(render_forwarding_rules(config), end="")
    return 0


def simulate(
    action: str, path: Path, ssh_config: Path | None, dry_run_only: bool, only_arguments: list[str] | None
) -> int:
    """Start (`up`), set up with nanoHPC (`deploy`, or only one part of it with `only_arguments`), or remove
    (`down`) the simulated test cluster of a sim file."""
    if dry_run_only and action != "deploy":
        print("--dry-run works only with nanohpc sim deploy", file=sys.stderr)
        return 1
    if only_arguments is not None and action != "deploy":
        print("--only works only with nanohpc sim deploy", file=sys.stderr)
        return 1
    only, right = read_only(only_arguments)
    if not right:
        return 1
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    if shutil.which("limactl") is None:
        print("limactl not found: install Lima (https://lima-vm.io)", file=sys.stderr)
        return 1
    plan, errors = sim.load_sim(path)
    if action == "down":
        # Down also works when the sim file or its cluster.yml changed since `up`: it uses the record `up` wrote.
        for error in errors:
            print(f"{path}: {error} (removing what sim up recorded)", file=sys.stderr)
        sim.down(path.stem, plan)
        print(f"simulated cluster {path.stem} is removed")
        return 0
    if plan is None:
        print(f"{path}: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    if action == "deploy":
        state = sim.STATE_ROOT / plan.name
        if not (state / "cluster.yml").is_file():
            print(f"{plan.name} is not up: run nanohpc sim up {path} first", file=sys.stderr)
            return 1
        config, errors = load_config(state / "cluster.yml", True, False)
        if errors:
            print("\n".join([f"{state / 'cluster.yml'}: {error}" for error in errors]), file=sys.stderr)
            return 1
        if config["cluster"].get("mode") == "monitor":
            if not isinstance(plan.fake_gpus, dict):
                print("monitor simulation needs a fake_gpus mapping", file=sys.stderr)
                return 1
            return deploy.deploy_monitor(
                config, ssh_config or state / "ssh_config", True, plan.fake_gpus, dry_run_only, only, None
            )
        if not isinstance(plan.fake_gpus, list):
            print("Slurm simulation needs a fake_gpus list", file=sys.stderr)
            return 1
        return deploy.deploy(
            config, ssh_config or state / "ssh_config", True, plan.fake_gpus, False, dry_run_only, only
        )
    state = sim.up(plan)
    print(f"simulated cluster {plan.name} is up: {state}/cluster.yml, {state}/ssh_config")
    return 0


def main() -> None:
    """Parse the command line and run the chosen subcommand."""
    parser = argparse.ArgumentParser(prog="nanohpc", description="Slurm cluster with monitoring, from one cluster.yml.")
    parser.add_argument("--version", action="version", version=f"nanohpc {importlib.metadata.version('nanohpc')}")
    commands = parser.add_subparsers(dest="command", required=True)
    validation = commands.add_parser("validate", help="check a cluster.yml without touching any machine")
    validation.add_argument("path", type=Path, help="path to cluster.yml")
    setup = commands.add_parser("deploy", help="set up the cluster described by a cluster.yml")
    setup.add_argument("path", type=Path, help="path to cluster.yml")
    setup.add_argument("--ssh-config", type=Path, help="SSH config file to reach the machines (default: your own)")
    setup.add_argument(
        "--dry-run", action="store_true", help="only the dry run: show what would change, and change nothing"
    )
    setup.add_argument("--only", nargs="+", metavar="PART", help=ONLY_HELP)
    monitor_setup = commands.add_parser("deploy-monitor", help="deploy monitoring without Slurm")
    monitor_setup.add_argument("path", type=Path, help="monitor-mode cluster.yml")
    monitor_setup.add_argument("--ssh-config", type=Path, help="SSH config file to reach the machines")
    monitor_setup.add_argument("--dry-run", action="store_true", help="show changes without applying them")
    monitor_setup.add_argument("--only", nargs=2, metavar=("NODE", "NAME"), help="add or update one machine")
    monitor_setup.add_argument("--accept-hardware-change", metavar="NAME", help="accept a changed GPU inventory")
    report = commands.add_parser(
        "check", help="report how every machine compares with cluster.yml, without changing anything"
    )
    report.add_argument("path", type=Path, help="path to cluster.yml")
    report.add_argument("--ssh-config", type=Path, help="SSH config file to reach the machines (default: your own)")
    report.add_argument("--before-restart", action="store_true", help="check saved boot settings on every machine")
    rules = commands.add_parser(
        "forwarding-rules", help="print rules for the lab's own web server to show the cluster website"
    )
    rules.add_argument("path", type=Path, help="path to cluster.yml")
    simulation = commands.add_parser("sim", help="simulated test cluster of Lima VMs (for testing nanoHPC)")
    simulation.add_argument(
        "action", choices=("up", "deploy", "down"), help="start, set up, or remove the simulated cluster"
    )
    simulation.add_argument("path", type=Path, help="path to a sim file, e.g. tests/sim/everyday.yml")
    simulation.add_argument(
        "--ssh-config", type=Path, help="deploy only: SSH config to reach the VMs (default: the one sim up wrote)"
    )
    simulation.add_argument("--dry-run", action="store_true", help="deploy only: only the dry run, change nothing")
    simulation.add_argument("--only", nargs="+", metavar="PART", help="deploy only: " + ONLY_HELP)
    fix = commands.add_parser(
        "fix-uid", help="renumber a user on a machine to their UID in cluster.yml (dry run unless --apply)"
    )
    fix.add_argument("path", type=Path, help="path to cluster.yml")
    fix.add_argument("user", help="user name in cluster.yml")
    fix.add_argument("machine", help="machine name in cluster.yml")
    fix.add_argument("--ssh-config", type=Path, help="SSH config file to reach the machine (default: your own)")
    fix.add_argument("--apply", action="store_true", help="apply the plan (without it, nothing is changed)")
    init = commands.add_parser("init", help="setup wizard: write a new cluster.yml, or change an existing one")
    init.add_argument("path", type=Path, help="path to cluster.yml (created if missing)")
    init.add_argument("--ssh-config", type=Path, help="SSH config file to reach the machines (default: your own)")
    init.add_argument("--mode", choices=("monitor",), help="create a monitor-only configuration")
    arguments = parser.parse_args()
    if arguments.command == "validate":
        sys.exit(validate(arguments.path))
    if arguments.command == "deploy":
        sys.exit(run_deploy(arguments.path, arguments.ssh_config, arguments.dry_run, arguments.only))
    if arguments.command == "deploy-monitor":
        sys.exit(
            run_deploy_monitor(
                arguments.path,
                arguments.ssh_config,
                arguments.dry_run,
                arguments.only,
                arguments.accept_hardware_change,
            )
        )
    if arguments.command == "check":
        sys.exit(
            restart_check.run(arguments.path, arguments.ssh_config)
            if arguments.before_restart
            else check.run(arguments.path, arguments.ssh_config)
        )
    if arguments.command == "forwarding-rules":
        sys.exit(forwarding_rules(arguments.path))
    if arguments.command == "sim":
        sys.exit(simulate(arguments.action, arguments.path, arguments.ssh_config, arguments.dry_run, arguments.only))
    if arguments.command == "fix-uid":
        sys.exit(fixuid.run(arguments.path, arguments.user, arguments.machine, arguments.ssh_config, arguments.apply))
    if arguments.command == "init":
        # Imported here: the terminal app (Textual) is only needed by this command.
        from nanohpc import wizard

        dependencies = wizard.Dependencies(
            probe.probe_machine, probe.user_ids, probe.uid_problems, probe.uid_owner, fixuid.plan_fix, fixuid.apply_fix
        )
        sys.exit(wizard.run(arguments.path, arguments.ssh_config, dependencies, arguments.mode))
