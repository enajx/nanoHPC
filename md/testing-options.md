# Testing options without real machines

Options for testing nanoHPC without a set of real lab machines. Not decided yet: see the testing item in [TODO.md](../TODO.md) (at the repo root). Once the choice is made, replace this file with the agreed testing setup.

## 1. Fake machines: VMs or system containers

Run one front node and two compute nodes as local VMs, and point nanoHPC at them as if they were real machines. Ansible cannot tell the difference.

| Tool | Notes |
| --- | --- |
| Multipass (Canonical) | Easiest on a Mac. One command gives an Ubuntu VM with SSH. Good fit for a 3-machine test cluster. |
| Lima | Similar to Multipass, more flexible to configure. |
| Incus (LXD fork) | Best on a Linux host. System containers act like full machines (systemd, SSH), start in seconds, use little memory. Can also run full VMs. Not native on macOS. |
| Vagrant | Older standard. Weak on Apple Silicon. |
| Docker Compose Slurm clusters (for example `slurm-docker-cluster`) | Fast, but no proper systemd, and NFS server and disk quotas do not work well. Only good for testing the scheduler. |

Use VMs or Incus system containers, not plain Docker containers: nanoHPC changes NFS, disk quotas, SSH access rules, and systemd, and these only behave realistically in full machines.

For Ansible roles, Molecule is the standard tool to test each role on its own in a container or VM.

## 2. Fake GPUs

A real NVIDIA GPU cannot be simulated. The two things nanoHPC depends on can be faked:

- **Scheduling**: Slurm accepts GPUs defined only by a count, with no device files (`Gres=gpu:4` and a `gres.conf` line without `File=`). Jobs that request `--gpus=1` are scheduled, queued, and counted in fair-share like on real GPUs. This covers partitions, limits, priority, and the queue dashboards.
- **Metrics**: a small fake GPU exporter that reports made-up utilization and memory. The collector, machine status rules, and Grafana GPU charts work the same.

Not covered: the NVIDIA driver, CUDA, and binding a job to a specific GPU. These need real hardware.

## 3. Real hardware, only as a final check

- Cloud GPU VMs rented by the hour (for example AWS, Lambda, Hetzner): a few hours on one or two GPU VMs before each release.
- Or a spare lab machine, if one is free. Never the production cluster.

## 4. Automatic tests (CI)

GitHub Actions Linux runners support nested virtualization, so CI could start a small VM cluster on each push, run a full setup, and submit a test job. Slow, but useful before releases.

## Development machine limits

The current development machine is an Apple M1 Max with 32 GB of memory. It runs three small VMs well, but they are ARM64 Ubuntu, while most lab machines are x86. Ansible, Prometheus, Grafana, and the website do not care. Slurm packages do, so this depends on the open decision about where Slurm packages come from.

## Suggested setup (not agreed)

- Day to day: unit tests for configuration validation and generated files, plus a local 3-VM cluster (Multipass) with fake GPUs.
- Before a release: the same test on a few cheap x86 cloud VMs, then one short run on a real GPU machine for the driver and CUDA.
