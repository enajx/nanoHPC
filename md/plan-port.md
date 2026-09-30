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
- **Partitions**: defined by the administrator in `cluster.yml`, not fixed to `main` and `interactive`. The wizard and example files give simple defaults.
- **Command**: `nanohpc` runs from any machine with SSH access to the cluster (the administrator's laptop or the front node), installed with `uv tool install`.
- **Paths**: fixed `nanohpc` paths (`/etc/nanohpc`, `/var/lib/nanohpc`, ...). The cluster name only appears in the website, dashboards, and Slurm.
- **Extras kept**: 5-year history, auto-deploy from Git (watching the administrator's configuration repository), Slack alerts.
- **Dropped**: Slurm-web, the forum software (Discourse) coexistence, the ITU-specific backup.
- **Website HTTPS**: Let's Encrypt, or the administrator's own certificate. No plain HTTP.
- **Backup (v0.1)**: rsync copy of `/home` to the cluster's backup machine or to an outside SSH server. restic snapshots and S3-style destinations: later.
- **License**: MIT.
- **Moving the REAL cluster to nanoHPC**: maybe later, no plan now.

## Simulated cluster

- **Lima** VMs, on macOS (Apple Silicon) and Linux (x86).
- The tests only need a list of SSH machines, so other machines (cloud VMs, spare machines) can be used too.
- Everyday test cluster: a front node and 5 compute nodes:
  - 4 GPUs;
  - 2 GPUs;
  - CPU-only;
  - backup storage;
  - 4 GPUs, `interactive` partition only.
- Run with `/home` both on the front node and on a separate storage machine.
- An option scales it up to 20 compute nodes.
- Fake GPUs: Slurm GPUs defined by count only, plus a fake GPU metrics exporter.

## Work order

1. Record the decisions in `PROJECT.md`, `TODO.md`, and `md/` (on `main`).
2. Configuration: `cluster.yml` format, validation, example files. The example format is agreed with the user before code is written.
3. Simulated cluster. Details (script language, VM networking) are confirmed with the user at the start.
4. Port of the roles, then the wizard, then release, as in [TODO.md](../TODO.md).

One branch and PR per milestone. Each milestone after the configuration gets a short planning round with the user before it starts.
