# Plan: port SLURM-REAL into nanoHPC

Transient plan file. It records the design decisions agreed with the user on 2026-09-30 for porting SLURM-REAL into nanoHPC, so a later agent can continue the work. It is linked from the milestone items in [TODO.md](../TODO.md) (at the repo root). Delete it when the port milestones are done, after moving anything worth keeping into `PROJECT.md` or `md/`.

## Goal

nanoHPC is a ready-made Slurm + monitoring tool for small heterogeneous lab clusters (up to about 100 machines) that anyone can use. It keeps the design choices of SLURM-REAL, except those specific to that cluster.

## Decisions

- **Scope**: up to about 100 machines. NVIDIA GPU nodes of mixed models, and CPU-only nodes. AMD GPUs: later.
- **Role layout**: the front node always does login, the Slurm controller, monitoring, and the website. The `/home` server and the backup can each be on the front node or on separate machines, chosen in the configuration.
- **Slurm**: nanoHPC builds Slurm from the official source, once per CPU type, and installs it on all machines. Version: the newest stable one when the build role is written, then pinned.
- **Operating system**: Ubuntu 22.04, 24.04, and 26.04. Testing on non-Ubuntu machines: later.
- **Users**: nanoHPC creates local users with fixed UIDs and SSH keys from the configuration, on every machine. LDAP: later.
- **GPU drivers**: installed beforehand by the administrator. nanoHPC checks the GPU count and stops with a clear message if it does not match.
- **Configuration**: one `cluster.yml`. nanoHPC validates it and generates the Ansible files. The administrator does not need to know Ansible. Reuse SLURM-REAL's `filter_plugins/cluster_machine_config.py`.
- **Wizard**: `nanohpc init` writes a whole `cluster.yml` with simple defaults, and probes the machines over SSH (CPUs, memory, GPU type and count). Example YAML files are provided too.
- **Partitions**: defined by the administrator in `cluster.yml`, not fixed to `main` and `interactive`. The wizard and example files give simple defaults. Every partition has a time limit.
- **Storage roles**: the `/home` machine and the backup machine do not run jobs (they cannot also have the compute role).
- **Validation**: limits that no compute machine can meet (GPUs per user, memory per CPU, CPUs per GPU) are rejected. YAML syntax errors show Python's own error output (no `try`/`except`).
- **Command**: `nanohpc` runs from any machine with SSH access to the cluster (the administrator's laptop or the front node), installed with `uv tool install`.
- **Paths**: fixed `nanohpc` paths (`/etc/nanohpc`, `/var/lib/nanohpc`, ...). The cluster name only appears in the website, dashboards, and Slurm.
- **Extras kept**: 5-year history, auto-deploy from Git (watching the administrator's configuration repository), Slack alerts.
- **Dropped**: Slurm-web, the forum software (Discourse) coexistence, the ITU-specific backup.
- **Website HTTPS**: Let's Encrypt, or the administrator's own certificate. No plain HTTP.
- **Backup (v0.1)**: rsync copy of `/home` to the cluster's backup machine or to an outside SSH server. restic snapshots and S3-style destinations: later.
- **License**: MIT.
- **Moving the REAL cluster to nanoHPC**: maybe later, no plan now.

## Agreed for M3 (Slurm, accounts, storage), 2026-09-30

- Commands: `nanohpc deploy` runs everything. Separate commands with their own checks: `add-node NAME`, `check` (read-only), `users`, `policy`, `partitions`.
- nanoHPC never formats a disk. Home and scratch disks must already have a filesystem; nanoHPC checks the type and mounts it. The wizard (M7) guides the administrator through preparing machines, with the option to skip. The simulated cluster formats its test disks itself, standing in for the administrator.
- A listed user with a different UID on a machine, or a local `/home` with data: the deploy stops on that machine and says what to do next. `nanohpc fix-uid USER MACHINE` harmonises a UID safely when the administrator runs it. The wizard (M7) finds UID conflicts while probing and guides the administrator to harmonise them before the first deploy, so the deploy's stop is only a safety net.
- Login by SSH key only.
- Root for setup (agreed 2026-09-30): nanoHPC never adds passwordless sudo rules. It installs `pam_ssh_agent_auth` so administrators' forwarded SSH keys (`ssh -A`) unlock `sudo`, for their own use and for later deploys by a person or an agent. The first setup of a machine needs root the normal way: sudo without a password if the account already has it, else a password asked once at a terminal and kept in memory only; with no terminal (an agent), deploy stops before any change and explains the options. `ssh-add -c` (confirm each use of the key) is mentioned in the wizard and the docs as an option, not the default.
- M3 is done in three steps, each on its own branch and tested on the simulated cluster: (a) Ansible base, users, Munge, Slurm build and install, fake GPUs; (b) `/home` over NFS with quotas (both layouts) and scratch; (c) job modes (`cluster-submit`), uv, health checks.

## M3b (`/home` and scratch): choices made by the agent, to review

The user said to go ahead without a planning round (2026-10-01). These choices follow SLURM-REAL where it had an answer and are recorded here for the user to review:

- `/home` is served over NFSv4 by the home machine (front node or storage machine) to every other machine except the backup machine. Exports list each client's address (`rw,sync,no_root_squash,no_subtree_check`); clients mount with `rw,nosuid,nodev,_netdev,hard,timeo=600,retrans=2` through `/etc/fstab`. Agreed with the user (2026-10-01): `no_root_squash` (SLURM-REAL used `root_squash`) because on Ubuntu 26.04+ the forwarded SSH agent socket that unlocks administrators' sudo is in their home folder; `/home` is `nosuid,nodev` on every machine (home disk, NFS mounts, and a bind mount when `/home` is on the root disk) so no program in it can gain root.
- Quotas: ext4 or xfs. nanoHPC never formats or changes the filesystem's features: it mounts the home disk with user quotas on, creates the quota files if the filesystem has no built-in quota support, and sets each user's soft and hard limits and the grace time with `setquota` (inode limits unlimited). Without `home.device`, `/home` stays on the root disk and quotas are turned on there the same way (ext4 only).
- A local `/home` on a machine that will mount the shared one: the deploy stops on that machine if `/home` holds anything other than the home folders of the accounts nanoHPC deploys with (for example `ubuntu`) and `lost+found`, and lists what it found. Before the NFS mount hides the deploy accounts' homes, their SSH keys are copied to `/etc/ssh/authorized_keys/<account>`, so they can still log in (without a home folder on that machine).
- Scratch on a disk (`scratch.device`): the disk must already have an ext4 or xfs filesystem; nanoHPC mounts it at `/scratch`. Scratch in a file (`scratch.image_gb`): nanoHPC creates the image file under `/var/lib/nanohpc/`, makes its filesystem (a file nanoHPC owns, not one of the administrator's disks), and mounts it at `/scratch`, only if the free space left afterwards is at least 10% of the disk. As in SLURM-REAL, the image is fully allocated and never trimmed.
- Scratch layout as in SLURM-REAL: per-user folders (0700) with uv, Hugging Face, and PyTorch caches pointed there by `/etc/profile.d`, a staging area for job data, and a daily cleanup timer, on by default, that removes staged data unused for `scratch.cleanup_days` days unless a running job uses it. Dataset staging itself (`stage-dataset`) comes with `cluster-submit` in M3c.
- The simulated cluster formats its test disks itself (`sim up`), standing in for the administrator: ext4 with quota support.
- After review (2026-10-01): only accounts not in `cluster.yml` (for example `ubuntu`) may have a local home folder hidden by the mount; a disk is checked in preflight (missing device, partition table, other filesystem type, mounted elsewhere) and a `mkfs` hint is given only for an empty disk; `/scratch` entries in `/etc/fstab` have `nofail`, so a missing scratch disk cannot stop a machine from booting; the machines that mount `/home` wait for the home machine to finish; Ansible's temporary files go to the login session's private runtime folder.

## M3c (job modes, uv, health checks): choices made by the agent, to review

The user said to go ahead (2026-10-01). These follow the source deployment and are recorded for the user to review:

- `cluster-submit` as in the source deployment: `--mode scratch` (default) runs the job in a private copy of the project on the compute node's `/scratch` (Git-tracked files with their current contents, or the files named by `#CLUSTER include=`), and copies back the paths named by `#CLUSTER copy-back=`; `--mode shared` is plain `sbatch`. Installed on the front node and the compute nodes.
- Space kept free on `/scratch` when copying: at least 1 GiB, at most 85% of the disk used, and 10,000 free inodes (the source deployment's 50 GiB and 1M inodes do not fit small disks).
- `stage-dataset --private PATH` stages data from the user's home into `/scratch` for reuse across jobs (cleaned up by the daily timer). `stage-dataset --shared` needs a shared datasets area, which nanoHPC does not have yet: later.
- uv: a pinned release for x86_64 and ARM64, checked against its published SHA-256, in `/opt/uv-<version>` with `/usr/local/bin/uv` and `uvx`, on the front node and the compute nodes.
- `cluster-health` on every machine: mounts, services (Munge, Slurm daemons, MariaDB, NFS server on the home machine), Slurm answering, nodes not down or drained, disks over 90% full, the scratch cleanup timer. Every deploy ends by running it and reports problems. Stale GPU readings need monitoring (M4); alerts to Slack come with M6.

## Simulated cluster

- **Lima** VMs, on macOS (Apple Silicon) and Linux (x86).
- The tests only need a list of SSH machines, so other machines (cloud VMs, spare machines) can be used too.
- Everyday test cluster: a front node, 4 compute nodes, and a storage machine that runs no jobs:
  - 4 GPUs;
  - 2 GPUs;
  - CPU-only;
  - 4 GPUs, `interactive` partition only.
- Run with `/home` on the front node (backup to the storage machine), and with `/home` on the storage machine (no backup).
- Fake GPUs are set in a separate test-only file, not in `cluster.yml`.
- An option scales it up to 20 compute nodes.
- Fake GPUs: Slurm GPUs defined by count only, plus a fake GPU metrics exporter.
- The Linux x86 host is checked on a GitHub Actions runner (the M1 Mac cannot run VMs inside a VM).

## Work order

1. Record the decisions in `PROJECT.md`, `TODO.md`, and `md/` (on `main`).
2. Configuration: `cluster.yml` format, validation, example files. The example format is agreed with the user before code is written.
3. Simulated cluster. Details (script language, VM networking) are confirmed with the user at the start.
4. Port of the roles, then the wizard, then release, as in [TODO.md](../TODO.md).

One branch and PR per milestone. Each milestone after the configuration gets a short planning round with the user before it starts.
