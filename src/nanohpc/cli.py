"""The `nanohpc` command."""

import argparse
import importlib.metadata
import os
import shutil
import sys
from pathlib import Path

from nanohpc import deploy, sim
from nanohpc.config import load_config
from nanohpc.render import render_forwarding_rules


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
    compute = sum("compute" in machine["roles"] for machine in machines)
    print(f"{path}: valid (machines: {len(machines)}, compute: {compute}, users: {len(config['users'])})")
    return 0


def run_deploy(path: Path, ssh_config: Path | None) -> int:
    """Validate a cluster.yml, then set up the cluster it describes."""
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
    # A simulated cluster's front node deploys itself like `nanohpc sim deploy` (set on its automatic deploy
    # service only on simulated clusters; test use).
    simulated = os.environ.get("NANOHPC_SIMULATED") == "1"
    fake_gpus = [name for name in os.environ.get("NANOHPC_FAKE_GPUS", "").split(",") if name] if simulated else []
    return deploy.deploy(config, ssh_config, simulated, fake_gpus, automatic)


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


def simulate(action: str, path: Path, ssh_config: Path | None) -> int:
    """Start (`up`), set up with nanoHPC (`deploy`), or remove (`down`) the simulated test cluster of a sim file."""
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
        return deploy.deploy(config, ssh_config or state / "ssh_config", True, plan.fake_gpus, False)
    state = sim.up(plan)
    print(f"simulated cluster {plan.name} is up: {state}/cluster.yml, {state}/ssh_config")
    return 0


def main() -> None:
    """Parse the command line and run the chosen subcommand."""
    parser = argparse.ArgumentParser(prog="nanohpc", description="Slurm cluster with monitoring, from one cluster.yml.")
    parser.add_argument("--version", action="version", version=f"nanohpc {importlib.metadata.version('nanohpc')}")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="check a cluster.yml without touching any machine")
    check.add_argument("path", type=Path, help="path to cluster.yml")
    setup = commands.add_parser("deploy", help="set up the cluster described by a cluster.yml")
    setup.add_argument("path", type=Path, help="path to cluster.yml")
    setup.add_argument("--ssh-config", type=Path, help="SSH config file to reach the machines (default: your own)")
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
    arguments = parser.parse_args()
    if arguments.command == "validate":
        sys.exit(validate(arguments.path))
    if arguments.command == "deploy":
        sys.exit(run_deploy(arguments.path, arguments.ssh_config))
    if arguments.command == "forwarding-rules":
        sys.exit(forwarding_rules(arguments.path))
    if arguments.command == "sim":
        sys.exit(simulate(arguments.action, arguments.path, arguments.ssh_config))
