"""The `nanohpc` command."""

import argparse
import shutil
import sys
from pathlib import Path

from nanohpc import sim
from nanohpc.config import load_config


def validate(path: Path) -> int:
    """Check a cluster.yml, print every error, and return the exit code."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path)
    if errors:
        print(f"{path}: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    machines = config["machines"].values()
    compute = sum("compute" in machine["roles"] for machine in machines)
    print(f"{path}: valid (machines: {len(machines)}, compute: {compute}, users: {len(config['users'])})")
    return 0


def simulate(action: str, path: Path) -> int:
    """Start (`up`) or remove (`down`) the simulated test cluster described by a sim file."""
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
    state = sim.up(plan)
    print(f"simulated cluster {plan.name} is up: {state}/cluster.yml, {state}/ssh_config")
    return 0


def main() -> None:
    """Parse the command line and run the chosen subcommand."""
    parser = argparse.ArgumentParser(prog="nanohpc", description="Slurm cluster with monitoring, from one cluster.yml.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="check a cluster.yml without touching any machine")
    check.add_argument("path", type=Path, help="path to cluster.yml")
    simulation = commands.add_parser("sim", help="simulated test cluster of Lima VMs (for testing nanoHPC)")
    simulation.add_argument("action", choices=("up", "down"), help="start or remove the simulated cluster")
    simulation.add_argument("path", type=Path, help="path to a sim file, e.g. tests/sim/everyday.yml")
    arguments = parser.parse_args()
    if arguments.command == "validate":
        sys.exit(validate(arguments.path))
    if arguments.command == "sim":
        sys.exit(simulate(arguments.action, arguments.path))
