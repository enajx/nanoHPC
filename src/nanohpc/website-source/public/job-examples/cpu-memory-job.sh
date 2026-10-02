#!/bin/bash
# Save as job.sh, change script.py to your program, then run: sbatch job.sh
# This requests one GPU, 8 CPUs, and 64 GiB of RAM. The request must fit on one compute machine.
#SBATCH --gpus=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=02:00:00
#SBATCH --output=cpu-memory-%j.log

set -euo pipefail
uv run --locked python script.py
