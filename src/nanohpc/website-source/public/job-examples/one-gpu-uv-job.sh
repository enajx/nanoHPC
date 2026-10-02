#!/bin/bash
# Save as job.sh, change script.py to your program, then run:
# sbatch job.sh (from a git worktree; see Shared vs scratch on the How to page)
# Keep pyproject.toml and uv.lock in the project for --locked.
#SBATCH --gpus=1
#SBATCH --time=02:00:00
#SBATCH --output=one-gpu-%j.log

set -euo pipefail
uv run --locked python script.py
