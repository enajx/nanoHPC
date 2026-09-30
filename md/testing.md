# Testing setup

How nanoHPC is tested without touching a real cluster. Agreed with the user on 2026-09-30. Status: see the simulated cluster items in [TODO.md](../TODO.md) (at the repo root).

## Principle

nanoHPC only reaches machines over SSH, through Ansible. The tests do the same: they take a list of SSH machines and a `cluster.yml` that describes them. Where the machines come from does not matter. By default they are Lima VMs made by `nanohpc sim up`, but cloud VMs or spare lab machines work too. Never the production cluster.

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
- A separate sim file (`tests/sim/large.yml`) scales it up to 20 compute nodes, for occasional checks that configuration, Slurm, dashboards, and the website handle more machines. With 1 GB per VM this needs about 22 GB of RAM.

## Using the simulated cluster

Requires [Lima](https://lima-vm.io) (`brew install lima` on macOS). Each test cluster is a sim file in `tests/sim/`, a test-only file that names a `cluster.yml`, the Ubuntu version, the machines with fake GPUs, and the VM sizes:

| Sim file | Cluster |
| --- | --- |
| `tests/sim/everyday.yml` | [examples/cluster.yml](../examples/cluster.yml) (at the repo root): `/home` on the front node, backup to the storage machine |
| `tests/sim/home-on-storage.yml` | `tests/sim/cluster-home-on-storage.yml`: `/home` on the storage machine, no backup |
| `tests/sim/large.yml` | `tests/sim/cluster-large.yml`: front node and 20 compute nodes |

```
uv run nanohpc sim up tests/sim/everyday.yml     # create and start the VMs (reuses running or stopped ones)
ssh -F .nanohpc-sim/everyday/ssh_config gpu4     # log in to a machine by its name
uv run nanohpc sim down tests/sim/everyday.yml   # delete the VMs, their disks, and .nanohpc-sim/everyday/
```

`sim up` writes, under `.nanohpc-sim/<sim name>/` (not tracked by Git):

- `cluster.yml`: the configuration with each machine's real VM address. Lima's user-v2 network gives addresses by DHCP, so they differ from the ones in the source file.
- `ssh_config`: reaches each machine by its name (`Host gpu4`) through the port Lima forwards, as the VM's default user, who has sudo.
- `fake-gpus.yml`: the machines whose GPUs are simulated.

`sim up` refuses to reuse a VM or disk whose size no longer matches the sim file: run `sim down` first. `sim down` removes the VMs and disks that `sim up` recorded in `.nanohpc-sim/<sim name>/lima.yml`, so it also works after the sim file or its `cluster.yml` changed.

These two files, `cluster.yml` and `ssh_config`, are all the tests need, so a real set of test machines works too.

How the VMs are made:

- One Lima VM per machine, on Lima's user-v2 network: the VMs reach each other, and no sudo is needed on the host.
- Extra disks: each device path in the configuration (`home.device`, `scratch.device`) becomes an extra Lima disk. A VM's extra disks appear as `/dev/vdb`, `/dev/vdc`, ... in the order they are attached, so a simulated `cluster.yml` must use those names in that order. The validator of the sim file checks this.
- The VM's CPUs and memory are set in the sim file and are smaller than the hardware written in `cluster.yml`. Slurm must be told to accept the configured values on simulated machines.

Real-VM test (slow, off by default): `NANOHPC_SIM=1 uv run python -m unittest tests.test_sim.SimClusterTest`. It brings a cluster up, checks SSH, sudo, the cluster network, and the disks on every machine, then brings it down. `NANOHPC_SIM_FILE=large` picks another sim file.

## Fake GPUs

A real NVIDIA GPU cannot be simulated. The two things nanoHPC depends on are faked. Which machines have fake GPUs is set in a separate test-only file, so `cluster.yml` only describes real setups.

- **Scheduling**: Slurm accepts GPUs defined only by a count, with no device files (`Gres=gpu:4` and a `gres.conf` line without `File=`). Jobs that request GPUs are scheduled, queued, and counted in fair-share like on real GPUs.
- **Metrics**: a small fake GPU exporter reports made-up utilization and memory, so the collector, machine status rules, and Grafana GPU charts work the same.

Not covered: the NVIDIA driver, CUDA, and binding a job to a specific GPU. These need real hardware.

## Linux host: GitHub Actions

The workflow [sim-linux.yml](../.github/workflows/sim-linux.yml) (at the repo root) runs the unit tests and the real-VM test on GitHub's Linux x86 runner, which has KVM. It runs on pushes to `main` that change code or tests, and by hand (`workflow_dispatch`). This checks that `nanohpc sim up/down` works on a Linux host with x86 VMs. The repository is private, so the runner has 2 CPUs and about 7 GB of memory, and the run uses Actions minutes.

## CPU types

The Mac makes ARM64 VMs. Most lab machines are x86. nanoHPC builds Slurm from source for each CPU type, so both work. Before a release, the full setup is also run on x86 machines (a Linux host with Lima, or cloud VMs), then once on a real GPU machine for the driver and CUDA.
