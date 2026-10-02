#!/bin/bash
# Save as job.sh, then run: sbatch job.sh
# This checks the GPU assigned to this job.
#SBATCH --gpus=1
#SBATCH --time=00:05:00
#SBATCH --output=gpu-check-%j.log

set -euo pipefail
nvidia-smi
