# {{cluster_name}} documentation

> Plain Markdown copy of the How to page.

[Open the website view](./#docs)

## 0. Setup

### SSH access to {{cluster_name}}

On your laptop, use an existing SSH key or create one at an unused path:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_{{cluster_name}}
```

Send the public key (.pub) to the cluster admin and ask for an account on the {{cluster_name}} front node. Keep the private key on your laptop. Add your assigned username and key path to ~/.ssh/config:

```bash
Host {{cluster_name}}
    HostName {{login_address}}
    User USER
    IdentityFile ~/.ssh/id_ed25519_{{cluster_name}}
```

## 1. SSH into {{cluster_name}}

```bash
ssh -A {{cluster_name}}
```

### Get your code onto {{cluster_name}}

Forward your laptop’s SSH agent, then clone or pull from GitHub. This uses your loaded laptop key without copying its private key to {{cluster_name}}. Only forward your agent to a machine you trust. (Other options: a read-only deploy key for one repository, or gh auth login on {{cluster_name}}.)

```bash
git clone git@github.com:OWNER/REPOSITORY.git
cd REPOSITORY
```

## 2. Submit a job

A job script asks Slurm for resources, then runs your commands. A job goes to the default partition unless you name another one with --partition. Each partition has a maximum run time; if you leave out --time, a job gets that maximum. Default memory per CPU and CPUs per GPU are also set per partition, and memory is enforced.

The partitions, their time limits, and their machines are live values: see them on the [Cluster policy page](./#policy).

### job.sh: save this file in your project

```bash
#!/bin/bash
#SBATCH --gpus=1
#SBATCH --time=00:05:00

set -euo pipefail
uv run --locked python script.py
```

set -euo pipefail stops the script when a command fails, an unset variable is used, or a command in a pipeline fails. Replace uv run --locked python script.py with the command that runs your code. uv is installed on the front node and the compute machines. If jobs have outbound internet access, uv can download locked packages; downloads use job time. Use nvidia-smi when you only want to check the GPU assigned to the job.

### Terminal: type these commands after saving job.sh

We recommend submitting from a git worktree: a separate checkout of one commit, so editing or pulling in your project does not change the code of a waiting or running job. The worktree holds the last commit only, so commit job.sh and your code first, and add .worktrees/ and logs/ to .gitignore.

```bash
cd ~/YOUR_PROJECT
mkdir -p logs
RUN=.worktrees/$(date +%Y%m%d-%H%M%S)
git worktree add --detach "$RUN"
(cd "$RUN" && sbatch --output="$HOME/YOUR_PROJECT/logs/job-%j.log" job.sh)
squeue --me
```

Write results to an absolute path in your home, such as $HOME/YOUR_PROJECT/results, not inside the worktree. When the job has ended, remove the worktree from your project folder with git worktree remove "$RUN" (git worktree list shows them all).

/home is shared between {{cluster_name}} and the compute machines. Your home has a soft quota of {{home_quota_soft_gb}} GB and a hard quota of {{home_quota_hard_gb}} GB: you can go above the soft quota for a limited time, never above the hard quota. sbatch reads and writes the files in the folder you submit from. /scratch/$USER is fast local temporary storage; for jobs that read or write a lot, use cluster-submit --mode=scratch job.sh to let the cluster manage a private copy on the compute machine. Both use Slurm and follow the same resource requests.

Do not name a node or use --constraint for normal jobs. Slurm selects a suitable compute machine.

[Current machines](machines.md) · [Cluster policy](policy.md)

## Example job scripts

- [Download gpu-check-job.sh](job-examples/gpu-check-job.sh): one GPU, checks the assigned GPU.
- [Download one-gpu-uv-job.sh](job-examples/one-gpu-uv-job.sh): one GPU, runs a uv Python script.
- [Download four-gpu-torchrun-job.sh](job-examples/four-gpu-torchrun-job.sh): four GPUs, runs PyTorch distributed training.
- [Download cpu-memory-job.sh](job-examples/cpu-memory-job.sh): one GPU with explicit CPU and RAM requests.

## Batch vs interactive

### Batch job

Save your commands in a job script and submit it with sbatch, preferably from a git worktree (see Shared vs scratch). Slurm starts it when the requested resources are free, in the default partition unless you name another one, and stops it at its time limit. You can log out while it waits or runs; its output goes to the file named by --output.

When to use: training runs, sweeps, and anything that runs without you watching it.

```bash
cd ~/YOUR_PROJECT/.worktrees/RUN
sbatch job.sh
squeue --me
```

### Interactive shell

Partitions set up for interactive shells accept only this command form. Replace PARTITION with such a partition; the Cluster policy page lists the partitions. You can change the resource amounts and time. Slurm waits for those resources, selects the compute node, and opens Bash there. Exit the shell to release the resources. Direct SSH to compute nodes is for administrators only.

When to use: debugging, short tests, and checking that your code and environment work on a GPU before you submit a long batch job.

```bash
srun --partition=PARTITION --gpus=1 --cpus-per-task=4 --time=01:00:00 --pty bash -l
```

## Shared vs scratch

We recommend shared mode with a git worktree for most jobs. Use scratch mode for jobs that read or write a lot.

### Shared mode with a git worktree (recommended)

Make a separate git worktree for each run and submit from it with sbatch. The job runs that fixed copy of the code, so you can keep editing and pulling in your main checkout. The worktree holds the last commit only, so commit your changes first, and add .worktrees/ and logs/ to .gitignore. Write results to an absolute path in your home outside the worktree, such as $HOME/YOUR_PROJECT/results: they are saved as the job writes them, and a job stopped at its time limit loses only the work in progress. Cost: each worktree is a full copy of the code in your home, and uv sync builds a separate .venv in it. The job reads both over the network, and building the .venv copies the whole environment into your home, which takes job time for large packages such as PyTorch. Remove the worktree when the job has ended; git worktree remove refuses if it holds files you have not committed.

When to use: most jobs: training runs and sweeps, especially long ones or many at once while you keep changing the code.

```bash
cd ~/YOUR_PROJECT
mkdir -p logs
RUN=.worktrees/$(date +%Y%m%d-%H%M%S)
git worktree add --detach "$RUN"
(cd "$RUN" && sbatch --output="$HOME/YOUR_PROJECT/logs/job-%j.log" job.sh)

# After the job has ended:
git worktree remove "$RUN"
```

### Shared mode

Use this when you want the job to read and write the shared project directly. There is no project copy or output copy-back. A git pull, branch switch, or file edit can change code used by processes that start later in a pending or running job.

When to use: jobs that spend their time on CPU or GPU work and read or write few files, when you will not edit the project while the job waits or runs.

```bash
cd ~/YOUR_PROJECT
sbatch job.sh
```

### Shared mode with local scratch

Keep code and results in your home, and put large datasets and temporary files on the compute machine’s local disk. Add these lines to job.sh. stage-dataset --private copies a dataset folder from your home (path relative to your home) to local scratch once, and later jobs on the same machine reuse it. A staged copy that no job has used for {{scratch_cleanup_days}} days is deleted automatically. Files in TMPDIR are not copied back; the last line deletes them.

When to use: large datasets that are read many times, or jobs that write many temporary files, where reading and writing over the shared home would slow the job.

```bash
export TMPDIR=/scratch/$USER/tmp/$SLURM_JOB_ID
mkdir -p "$TMPDIR"
DATA=$(stage-dataset --private datasets/MY_DATASET)
uv run --locked python train.py --data "$DATA" --output "$HOME/YOUR_PROJECT/results/$SLURM_JOB_ID"
rm -rf "$TMPDIR"
```

### Scratch mode

Use this when you want a private project copy while the job runs. cluster-submit copies Git-tracked files when the job starts, including uncommitted edits. Changes made while the job waits may be copied; later changes do not affect the running copy. Add #CLUSTER include= for other inputs and #CLUSTER copy-back= for results to return, including after an application failure. Git commands such as git rev-parse do not work in the scratch copy, which has no .git folder. --mode is required (--mode=shared is the same as plain sbatch). sbatch options go before the script in the --name=value form, for example cluster-submit --mode=scratch --output=$HOME/YOUR_PROJECT/logs/job-%j.log job.sh.

When to use: projects with many small files or heavy reading and writing inside the project folder, or when the job should use a fixed copy of the project including uncommitted edits.

```bash
cd ~/YOUR_PROJECT
cluster-submit --mode=scratch job.sh
```

### Scratch mode with results in home

Run from a private scratch copy, but write results and checkpoints to an absolute path in your home. They are saved as the job writes them, so they do not depend on copy-back at the end of the job, and a time limit or crash loses only the work in progress. These paths need no #CLUSTER copy-back= line.

When to use: long scratch-mode runs whose checkpoints you cannot afford to lose.

```bash
#!/bin/bash
#SBATCH --gpus=1
#SBATCH --time=12:00:00
#SBATCH --output=training-%j.log
set -euo pipefail
uv run --locked python train.py --output "$HOME/YOUR_PROJECT/results/$SLURM_JOB_ID"

# Submit with:
# cd ~/YOUR_PROJECT
# cluster-submit --mode=scratch job.sh
```

### Select extra inputs and outputs

For scratch mode. Add these lines to the job script. Outputs return to the same relative path inside the original project. Choose a different output directory for each run. Non-Git projects need explicit inputs.

```bash
#CLUSTER include=datasets/small-test
#CLUSTER copy-back=results/
```

### Recover partial results

For scratch mode. Failed jobs attempt to return declared outputs and keep their scratch directory. Read the job log for the recovery path. Node loss or forced termination can prevent copy-back. Write important checkpoints to your experiment’s persistent output path during execution.

```bash
less training-JOB_ID.log
```

## Other settings

### Inspect your jobs

squeue shows queued and running jobs. sacct includes completed jobs. sstat shows CPU and memory use so far. An overlapping job step can inspect the assigned GPUs. seff is not installed; use sacct and sstat. Terminal output files and application checkpoints are separate outputs.

```bash
squeue --me
sacct --starttime today --format=JobID,JobName,State,Elapsed,AllocTRES
sstat -j JOB_ID.batch --format=JobID,AveCPU,MaxRSS
srun --jobid=JOB_ID --overlap nvidia-smi
less training-JOB_ID.log
```

## Jobs examples

### One GPU

Submit with sbatch from a git worktree, as shown in Shared vs scratch. The job goes to the default partition. The program must accept --output, or change that argument to match your program. Results go to an absolute path in your home, so they are saved as the job writes them.

```bash
#!/bin/bash
#SBATCH --gpus=1
#SBATCH --time=02:00:00
set -euo pipefail
uv run --locked python train.py --output "$HOME/YOUR_PROJECT/results/$SLURM_JOB_ID"
```

### Four GPUs

For training code that already supports four GPU workers. Requesting four GPUs does not make single-GPU code use them. Submit this script with sbatch job.sh from the shared project.

```bash
#!/bin/bash
#SBATCH --gpus=4
#SBATCH --cpus-per-task=16
#SBATCH --time=02:00:00
#SBATCH --output=training-%j.log
set -euo pipefail
uv run --locked torchrun --nproc-per-node=4 train.py
```

### More CPUs and RAM

Request 8 CPUs and 64 GiB of RAM for one GPU. The request must fit on one compute machine; the Machines page lists each machine’s CPU cores and RAM. Submit with sbatch job.sh from the shared project.

```bash
#!/bin/bash
#SBATCH --gpus=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=02:00:00
#SBATCH --output=training-%j.log
set -euo pipefail
uv run --locked python train.py
```
