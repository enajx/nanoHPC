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
- A separate sim file (`tests/sim/large.yml`) scales it up to 20 compute nodes, for occasional checks that configuration, Slurm, dashboards, and the website handle more machines. With 1 GB per compute VM and 3 GB for the front node this needs about 24 GB of RAM.

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
uv run nanohpc sim deploy tests/sim/everyday.yml # set it up with nanoHPC (first run builds Slurm, then cached)
# --ssh-config FILE deploys through another SSH config, e.g. as a cluster administrator with a forwarded key
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
- `sim up` makes the filesystems on the test disks, standing in for the administrator (nanoHPC never formats a disk): ext4, with quota support on the home disk.
- Lima's template installs containerd in each VM at boot; `sim up` turns that off (`.containerd.system/user = false`). On a busy host its setup outlasted Lima's boot wait and left `systemd-logind` stuck, so every fresh login waited 120 seconds.
- Lima's disk script tries to mount `/dev/vdb1` even for disks it does not format, so machines with an extra disk report `cloud-init status: error` and `systemctl is-system-running: degraded`. This is harmless for the tests.
- Ubuntu 26.04 VMs: Lima logs in over vsock through systemd's per-connection sshd, which sets its own `AuthorizedKeysFile` and so ignores `/etc/ssh/authorized_keys/<user>`. Real machines have no such path. `sim up` adds that folder to it with a systemd drop-in, so test logins behave as on a real machine.
- A VM whose first start fails (for example a slow boot on a busy host) is started once more.
- The VM's CPUs and memory are set in the sim file and are smaller than the hardware written in `cluster.yml`. `nanohpc sim deploy` sets `SlurmdParameters=config_overrides` so Slurm accepts the configured values.

Real-VM tests (slow, off by default, `NANOHPC_SIM=1`):

Run them one at a time on a laptop: with three test clusters up at once (12 VMs on a 10-CPU, 32 GB Mac), deploys failed on SSH and metrics timeouts that did not happen when each ran alone.

Which ones to run (agreed 2026-10-03), on Ubuntu 24.04 while developing:
- The everyday test (`SimDeployTest`) and the test of the feature being built, once from a new cluster at the end of each milestone.
- `SimHomeOnStorageTest` when storage, accounts, or backup change; `SimReleaseTest` with `NANOHPC_SIM_FILE=variations` (the less common settings) when the website or its certificates change; `SimAlertsTest` (stale GPU readings and Slack alerts, with up to half an hour of waiting) when alerts or metrics change. `SimRedeployTest` (the safety checks that each need another deploy: a missing certificate issued again, a drained node, an administrator's forwarded key, the stop with no key, a removed user, a UID conflict) when accounts, SSH, sudo, preflight, `deploy.py`, or the certificates change.
- While fixing something, `NANOHPC_SIM_KEEP=1` keeps the simulated cluster up after the test, and the next run deploys onto it again, which is much quicker. A cluster that already ran a test may not behave like a new one, so the milestone still ends with a run from scratch.
- `SimSetupTest` (about half a minute once the Ubuntu image is cached) when the setup wizard, the probe, or fix-uid change: it probes fresh machines, runs the real wizard against them, and renumbers a user with fix-uid.
- Before the release: everything, also on Ubuntu 22.04 and 26.04 (`NANOHPC_SIM_FILE=ubuntu-2204` and `ubuntu-2604`).


- `uv run python -m unittest tests.test_sim.SimClusterTest`: brings a cluster up, checks SSH, sudo, the cluster network, and the disks on every machine, then brings it down. `NANOHPC_SIM_FILE=large` picks another sim file.
- `uv run python -m unittest tests.test_sim.SimReleaseTest` with `NANOHPC_SIM_FILE=ubuntu-2204` or `ubuntu-2604`: deploys a small cluster (front node, one fake-GPU node, one CPU node) on that Ubuntu release and checks the stop for a scratch disk with no filesystem, Slurm nodes, jobs, sudo by forwarded key on every machine, the shared `/home` and quotas, scratch, a repeat deploy with no changes, and a later deploy as an administrator. The 22.04 cluster keeps `/home` on the root disk (no `home.device`). Each release builds its own Slurm packages the first time.
- `uv run python -m unittest tests.test_sim.SimHomeOnStorageTest`: `/home` served by the storage machine; checks that a machine whose local `/home` holds data stops, then the NFS mounts, shared files, a repeat deploy with no changes, and the users' quotas (read on the storage machine) in the front node's status snapshot.
- `uv run python -m unittest tests.test_sim.SimDeployTest`: deploys the everyday cluster with a key generated for the test, and checks users, real SSH logins (users on the front node, administrators everywhere), sudo by forwarded key through a test SSH agent, Munge, Slurm nodes, jobs, partition rules, fair-share, a repeat deploy with no changes, a later deploy as an administrator with no password, the stop when sudo needs a password and no one can type it, a UID conflict, `/home` sharing and quotas, scratch and its cleanup, uv, `cluster-health` on every machine, the `stage-dataset` tests (`tests/stage_dataset_test.sh`, run on a VM because they need GNU tools), a `cluster-submit` job with copy-back, a drained node reported as a warning without failing the deploy, metrics from every machine over mutual TLS (exporters refuse connections without the front node's certificate), fake GPU readings and machine specs, the 5-year history instance, the daily summary rules passing their `promtool` tests on the front node, a deleted metrics certificate issued again by the next deploy, the six Grafana dashboards for anonymous read-only viewers on localhost only (the machine panels' queries cover every machine), and the status snapshot (refreshed every 30 seconds, every machine Healthy, a submitted job listed, quotas on the user cards, Slurm figures in Prometheus).

### Website tests

- Fast: `tests/test_website_nginx.py` (the nginx site for each setting), `tests/test_website_docs.py` (the user guide filled with the cluster's values), `tests/test_website_build.py` (the prebuilt site in `src/nanohpc/website/` matches its source; run `npm run build` in `src/nanohpc/website-source/` after changing the source).
- Browser, with made-up data: `npm test` in `src/nanohpc/website-source/` (Playwright; needs `npm ci` and `npx playwright install chromium` once).
- Real VMs: every VM test that deploys checks the website over HTTPS from the front node (certificate, pages, refused routes, `allow`), then opens every page in a real browser through an SSH tunnel to the front node (`npm run test:live`: real nginx, Grafana, and security headers; no refused requests, console errors, or blocked content). The VM tests therefore need Node and Playwright's Chromium on the machine that runs them.
- Let's Encrypt on the simulated cluster: Pebble, Let's Encrypt's test server, runs on the front node with a test DNS server that answers the website hostname with the front node's address, so certbot's request, the challenge on port 80, and renewal run for real. For `https: own`, `sim up` makes a test certificate authority and a certificate for the hostname in `.nanohpc-sim/<name>/website-tls/`.
- Which test cluster covers what: everyday: Let's Encrypt, path `/cluster/`; Ubuntu 22.04: own certificate, path `/`, a logo, `build: front`; Ubuntu 26.04: path `/hpc/` and an `allow` list that refuses the other machines.

### Backup and alerts

- Fast: `tests/test_cluster_backup.py` and `tests/test_backup_receive.py` run the backup and its forced command with real rsync 3.x (set `NANOHPC_TEST_RSYNC` to an rsync 3.x; macOS's own rsync is openrsync and cannot run them; without it those tests are skipped); `tests/test_cluster_health.py` (health checks as metrics, with fake commands); `tests/test_prometheus_rules.py` (alert rules with promtool, when promtool is on PATH).
- Real VMs: the everyday test runs a backup to the backup machine (owners kept, deletions mirrored, a restore, the key refused for anything but rsync) and `SimAlertsTest` checks Slack alerts with a stand-in Slack server on the front node (a failing check on a machine, then its recovery) and stale GPU readings.

### Automatic deploys

- Fast: `tests/test_auto_deploy.py` (the run with real Git and a fake nanoHPC: lock, fast-forward from the deployed commit, a failed commit not retried, metrics), `tests/test_deploy_hook.py` (the webhook listener over real HTTP, with GitHub's signature), `tests/test_nanohpc_install.py` (the installer with a fake uv).
- Real VMs: `uv run python -m unittest tests.test_sim.SimAutoDeployTest` (sim file `auto-deploy.yml`, with its own cluster name so it can run next to the other test clusters). A bare Git repository of a cluster user on the front node stands in for GitHub. The front node deploys its commits by itself (a new user appears on the other machines), refuses a broken commit without changing anything or retrying it, deploys the fix, accepts root logins only from itself, and a manual deploy afterwards changes nothing; a commit cannot turn automatic deploys off, a manual deploy can.

## Fake GPUs

A real NVIDIA GPU cannot be simulated. The two things nanoHPC depends on are faked. Which machines have fake GPUs is set in a separate test-only file, so `cluster.yml` only describes real setups.

- **Scheduling**: Slurm ignores GPUs without device files ("Ignoring file-less GPU"), so a `gres.conf` with only a count does not work. On machines with fake GPUs, nanoHPC creates placeholder device files `/dev/nvidia0`, `/dev/nvidia1`, ... with NVIDIA's device number (195), through `/etc/tmpfiles.d/nanohpc-fake-gpus.conf`, and `gres.conf` is the same as on real GPU machines. With no driver loaded, nothing can open these files, but Slurm schedules, queues, and counts GPU jobs in fair-share like on real GPUs. The NVIDIA driver check is skipped on these machines.
- **Metrics**: a small fake GPU exporter reports made-up utilization and memory, so the collector, machine status rules, and Grafana GPU charts work the same.

Not covered: the NVIDIA driver, CUDA, and binding a job to a specific GPU. These need real hardware.

## Linux host: GitHub Actions

The workflow [sim-linux.yml](../.github/workflows/sim-linux.yml) (at the repo root) runs the unit tests and the real-VM test on GitHub's Linux x86 runner, which has KVM. It runs only when started by hand (`gh workflow run sim-linux.yml`), because free Actions minutes are limited: run it when the Linux check is needed, for example after changes to the sim code or before a release, not on every push. This checks that `nanohpc sim up/down` works on a Linux host with x86 VMs. The repository is private, so the runner has 2 CPUs and about 7 GB of memory, and the run uses Actions minutes.

## CPU types

The Mac makes ARM64 VMs. Most lab machines are x86. nanoHPC builds Slurm from source for each CPU type, so both work. Before a release, the full setup is also run on x86 machines (a Linux host with Lima, or cloud VMs), then once on a real GPU machine for the driver and CUDA.
