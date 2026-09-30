"""The `nanohpc` command."""

import argparse
import sys
from pathlib import Path

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


def main() -> None:
    """Parse the command line and run the chosen subcommand."""
    parser = argparse.ArgumentParser(prog="nanohpc", description="Slurm cluster with monitoring, from one cluster.yml.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="check a cluster.yml without touching any machine")
    check.add_argument("path", type=Path, help="path to cluster.yml")
    arguments = parser.parse_args()
    if arguments.command == "validate":
        sys.exit(validate(arguments.path))
