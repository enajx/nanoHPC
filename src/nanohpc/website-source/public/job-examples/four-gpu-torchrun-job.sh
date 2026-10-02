#!/bin/bash
# Save as job.sh, change train.py to your program, then run: sbatch job.sh
# Your training code must support distributed execution.
#SBATCH --gpus=4
#SBATCH --cpus-per-task=16
#SBATCH --time=02:00:00
#SBATCH --output=four-gpu-%j.log

set -euo pipefail
uv run --locked torchrun --nproc-per-node=4 train.py
