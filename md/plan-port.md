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
- M3 is done in three steps, each on its own branch and tested on the simulated cluster: (a) Ansible base, users, Munge, Slurm build and install, fake GPUs; (b) `/home` over NFS with quotas (both layouts) and scratch; (c) job modes (`cluster-submit`), uv, health checks.

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
