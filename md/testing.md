# Testing setup

How nanoHPC is tested without touching a real cluster. Agreed with the user on 2026-09-30. Not built yet: see the simulated cluster items in [TODO.md](../TODO.md) (at the repo root).

## Principle

nanoHPC only reaches machines over SSH, through Ansible. The tests do the same: they take a list of SSH machines and a `cluster.yml` that describes them. Where the machines come from does not matter. By default they are Lima VMs made by a small script, but cloud VMs or spare lab machines work too. Never the production cluster.

## Simulated cluster: Lima

[Lima](https://lima-vm.io) makes Linux VMs with the same commands on macOS (Apple Virtualization) and Linux (QEMU with KVM). It must work on both:

- macOS on Apple Silicon (ARM64 VMs);
- Linux on x86 (x86_64 VMs).

Every node is a full VM, so NFS, disk quotas, systemd, SSH rules, and Slurm's cgroups behave as on real machines on both platforms.

Other tools were considered and not chosen:

- Incus system containers: Linux only. On an M1 Mac they would need all nodes as containers inside one VM, where an NFS server and quotas do not behave realistically.
- Multipass: works, but does not add anything over Lima for our two platforms.
- Vagrant: weak on Apple Silicon, no longer open source.
- Plain Docker: no proper systemd, NFS, or quotas.

## Test clusters

Everyday cluster: a front node, 4 compute nodes, and a storage machine (about 7 to 8 GB of RAM in total):

| Node | Role |
| --- | --- |
| front | login, Slurm controller, monitoring, website |
| 4-GPU node | fake GPUs, all partitions |
| 2-GPU node | fake GPUs |
| CPU-only node | no GPUs |
| storage machine | backup target or `/home` server, runs no jobs |
| 4-GPU interactive node | fake GPUs, `interactive` partition only |

- It is run with both `/home` layouts: served from the front node with the backup going to the storage machine, and served from the storage machine with no backup.
- An option scales it up to 20 compute nodes, for occasional checks that configuration, Slurm, dashboards, and the website handle more machines. With 1 GB per VM this needs about 22 GB of RAM.

## Fake GPUs

A real NVIDIA GPU cannot be simulated. The two things nanoHPC depends on are faked. Which machines have fake GPUs is set in a separate test-only file, so `cluster.yml` only describes real setups.

- **Scheduling**: Slurm accepts GPUs defined only by a count, with no device files (`Gres=gpu:4` and a `gres.conf` line without `File=`). Jobs that request GPUs are scheduled, queued, and counted in fair-share like on real GPUs.
- **Metrics**: a small fake GPU exporter reports made-up utilization and memory, so the collector, machine status rules, and Grafana GPU charts work the same.

Not covered: the NVIDIA driver, CUDA, and binding a job to a specific GPU. These need real hardware.

## CPU types

The Mac makes ARM64 VMs. Most lab machines are x86. nanoHPC builds Slurm from source for each CPU type, so both work. Before a release, the full setup is also run on x86 machines (a Linux host with Lima, or cloud VMs), then once on a real GPU machine for the driver and CUDA.
