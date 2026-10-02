# Done

Archive of completed `TODO.md` items: what was built and the key files touched.

## 2026-09-30: design decisions for the port

- Agreed with the user how SLURM-REAL is ported into nanoHPC: scope (up to about 100 heterogeneous machines), role layout (optional separate `/home` storage and backup machines), Slurm built from source, Ubuntu 22.04/24.04/26.04, local users, `cluster.yml` with a wizard, admin-defined partitions, kept extras, HTTPS, rsync backup, MIT license.
- Agreed the testing setup: Lima VMs on macOS and Linux, a 6-node everyday test cluster, up to 20 compute nodes, fake GPUs.
- Files: `PROJECT.md`, `TODO.md`, `md/plan-port.md`, `md/testing.md` (was `md/testing-options.md`).

## 2026-09-30: `cluster.yml` format and validation (M1)

- Agreed the `cluster.yml` format with the user: roles per machine (`front`, `home`, `backup`, `compute`), optional aliases, optional home disk, CPU-only nodes, scratch as a disk or an image file, admin-defined partitions, policy with defaults.
- `nanohpc validate cluster.yml` reports every error with the field path, exits 1 on errors, and changes no machine. It catches duplicate YAML keys, wrong types, unsafe values (SSH key lines, system user names and UIDs, loopback addresses, `/` as backup path), storage roles combined with compute, and limits no machine can meet.
- Files: `pyproject.toml`, `src/nanohpc/config.py` (validator, ported from SLURM-REAL's `filter_plugins/cluster_machine_config.py`), `src/nanohpc/cli.py`, `examples/cluster.yml`, `examples/minimal.yml`, `tests/test_config.py`.

## 2026-09-30: simulated test cluster (M2)

- `nanohpc sim up <sim file>` creates and starts one Lima VM per machine on Lima's user-v2 network, with extra disks for `home.device` and `scratch.device`. It writes `.nanohpc-sim/<name>/cluster.yml` (real VM addresses), `ssh_config` (machines by name), and `fake-gpus.yml`. `nanohpc sim down` removes the VMs, disks, and files. Changed VM sizes are refused on reuse.
- Sim files: `tests/sim/everyday.yml` (front, 4 compute nodes, storage machine as backup target), `tests/sim/home-on-storage.yml` (`/home` on the storage machine, no backup), `tests/sim/large.yml` (front and 20 compute nodes). All three passed the real-VM test on the M1 Mac (2 to 6 minutes each). The everyday cluster also passed on a Linux x86 host (GitHub Actions runner with KVM, about 7 minutes); that workflow runs by hand only.
- Files: `src/nanohpc/sim.py`, `src/nanohpc/cli.py`, `tests/test_sim.py`, `tests/sim/`, `.github/workflows/sim-linux.yml`, `md/testing.md`.

## 2026-09-30: Slurm, users, SSH access, sudo, and Munge (M3a)

- `nanohpc deploy cluster.yml` (and `nanohpc sim deploy` for the simulated cluster) sets up a cluster with Ansible. It reads each machine's hostname and checks sudo over `ssh <machine>` first, then renders every configuration file in Python and runs the playbook.
- Slurm 26.05.4 is built from source once per Ubuntu release and CPU type and cached on the administrator's machine (`~/.cache/nanohpc/`). `slurm.conf` uses the machines' real hostnames; partitions, one QoS each, fair-share on GPU usage, and `job_submit.lua` (per-partition `jobs: batch | interactive | any`, time and GPU limits) come from `cluster.yml`. Fake GPUs get placeholder device files.
- Users are created with fixed UIDs and group IDs; SSH is key-only, with keys in `/etc/ssh/authorized_keys/<user>`; all users may log in to the front node, only administrators elsewhere. Removed users lose their keys and login. The effective SSH settings are checked before sshd is reloaded, with the old file put back on failure, and every deploy account stays allowed.
- Root for deploys: nanoHPC adds no passwordless sudo rules. Administrators' forwarded SSH keys unlock sudo (`pam_ssh_agent_auth`); the first setup of a machine needs root the normal way (password asked once at a terminal; with no terminal, deploy stops with instructions).
- Preflight stops a machine before any change on: unsupported Ubuntu, UID or group ID conflicts, a missing or broken NVIDIA driver or a wrong GPU count, a former administrator deploying.
- Checked end to end on the simulated cluster (`tests.test_sim.SimDeployTest`), reviewed twice by separate agents. Only Ubuntu 24.04 on ARM64 is tested so far.
- Files: `src/nanohpc/render.py`, `src/nanohpc/deploy.py`, `src/nanohpc/files/job_submit_rules.lua`, `src/nanohpc/ansible/` (roles preflight, base, accounts, munge, slurm_packages, slurm_controller, slurm_compute), `src/nanohpc/config.py` (partition `jobs`), `src/nanohpc/cli.py`, `tests/test_render.py`, `tests/test_deploy.py`, `tests/test_sim.py`, `md/testing.md`.

## 2026-10-01: `/home` with quotas, local scratch, and Ubuntu releases (M3b)

- `/home` is served over NFSv4 by the home machine (front node or storage machine) to every other machine except the backup machine, with per-user quotas (`setquota`). The home disk is the administrator's (never formatted by nanoHPC) or the root disk. Exports are `no_root_squash` and `/home` is `nosuid,nodev` on every machine (home disk, NFS mounts, bind mount for a root-disk `/home`), so administrators' sudo by forwarded key works on Ubuntu 26.04+, where OpenSSH keeps the agent socket in the home folder.
- Preflight stops a machine before any change when: a disk named in `cluster.yml` is missing, partitioned, of another type, already mounted elsewhere, or has no filesystem (only then with a `mkfs` hint); `/home` is mounted from another disk; or the local `/home` holds anything but the deploy accounts' home folders (whose SSH keys are kept in `/etc/ssh/authorized_keys`).
- Local `/scratch` on each compute machine, from a disk or an image file nanoHPC creates (only if 10% of the disk stays free; never resized or trimmed); per-user folders and caches (uv, Hugging Face, PyTorch); a daily cleanup timer for staged data. `/scratch` entries use `nofail`.
- Checked on the simulated cluster with Ubuntu 22.04 (`/home` on the root disk), 24.04 (everyday cluster and `/home` on the storage machine), and 26.04. The simulated cluster now formats its test disks, turns off Lima's containerd, and fixes 26.04's vsock sshd key path.
- Files: `src/nanohpc/ansible/roles/{home_server,home_client,scratch}/`, `src/nanohpc/ansible/roles/preflight/` (disk and `/home` checks), `src/nanohpc/ansible/roles/accounts/tasks/main.yml`, `src/nanohpc/files/scratch-cleanup`, `src/nanohpc/render.py`, `src/nanohpc/deploy.py`, `src/nanohpc/sim.py`, `tests/sim/`, `tests/test_render.py`, `tests/test_deploy.py`, `tests/test_sim.py`, `md/testing.md`.

## 2026-10-01: job modes, uv, and health checks (M3c)

- `cluster-submit` runs a job in a private copy of the project on the compute machine's `/scratch` (Git-tracked files with their current contents, or the files named by `#CLUSTER include=`) and copies back the paths named by `#CLUSTER copy-back=`; `--mode shared` is plain `sbatch`. A copy must leave 1 GiB and 15% of the disk and 10,000 inodes free.
- `stage-dataset --private PATH` stages a folder from the user's home on scratch for reuse across jobs (content-versioned, private, removed by the daily cleanup when unused).
- uv 0.12.21 for x86_64 and ARM64, checked against its SHA-256, owned by root, on the front node and the compute machines.
- `cluster-health` checks each machine for its roles (services, mounts, Slurm, nodes, disks, NFS answering). Every deploy ends by running it and printing the report: broken services or mounts fail the deploy; a drained node or a full disk is a warning.
- Checked on the simulated cluster (everyday cluster, `/home` on the storage machine, Ubuntu 22.04 and 26.04), and reviewed by a separate agent.
- Files: `src/nanohpc/files/{cluster-submit,stage-dataset,cluster-health}`, `src/nanohpc/ansible/roles/{job_modes,uv,health}/`, `src/nanohpc/ansible/roles/{base,scratch}/tasks/main.yml`, `src/nanohpc/ansible/site.yml`, `src/nanohpc/deploy.py`, `tests/test_cluster_submit.py`, `tests/stage_dataset_test.sh`, `tests/test_sim.py`, `md/testing.md`.

## 2026-10-02: metrics (M4a)

- A private certificate authority on the front node (key never leaves it) issues one certificate per machine; each deploy issues missing, expiring (under 30 days), or wrong certificates again, and renews the authority itself when under a year is left. The front node keeps a copy of each issued certificate so `cluster-health` warns 30 days before any expires.
- node_exporter 1.12.1 on every machine: localhost on the front node; elsewhere on the cluster address with TLS 1.3, client certificates required, only the front node's certificate and address accepted. Collectors write textfiles: machine specs (every 5 minutes; works without GPUs), GPU readings every 30 seconds (from nvidia-smi, or made-up values for a simulated machine's fake GPUs).
- Prometheus 3.15.0 on the front node: detail instance (every machine every 30 seconds, 90 days) with per-minute recorded values and daily summaries that scale to large clusters (no 24-hour subqueries), and a history instance keeping the daily summaries for 5 years. Configuration and rules checked with promtool before use.
- `cluster-health` on the front node warns about missing or unreadable metrics, stale GPU readings and machine specs, failing rules, an empty history after two days, and expiring certificates.
- Checked on the simulated cluster (everyday cluster, `/home` on the storage machine, Ubuntu 22.04 and 26.04) and reviewed by a separate agent.
- Files: `src/nanohpc/ansible/roles/{metrics_tls,node_metrics,machine_metrics,prometheus}/`, `src/nanohpc/files/{cluster-gpu-metrics,cluster-fake-gpu-metrics,cluster-machine-specs,prometheus-daily-rules.yml,cluster-health}`, `src/nanohpc/render.py`, `src/nanohpc/deploy.py`, tests `test_gpu_metrics.py`, `test_machine_specs.py`, `test_prometheus_rules.py`, `prometheus_daily_rules_test.yml`, `test_render.py`, `test_deploy.py`, `test_sim.py`.

## 2026-10-02: status collector and Grafana (M4b)

- Every 30 seconds the front node's status collector (its own unprivileged account) reads Slurm and Prometheus and writes `status.json` and `machines.md` for the website (`/var/lib/nanohpc/monitor`) and the Slurm figures for Prometheus. Each machine's health uses the services and mounts generated from `cluster.yml`; home and backup machines show as "Storage", CPU-only machines show GPU use "Not applicable". GPU-hour totals count from the first deploy. Job names with control characters cannot break the metrics file or the accounting read.
- Every 5 minutes the home machine reads the users' `/home` quotas and reports them as metrics, so the user cards show quotas wherever `/home` is.
- Grafana 13.2.3 on the front node's localhost under `/grafana/`, anonymous read-only (no login, no editing, no snapshots), with the two Prometheus data sources and six dashboards (overview, queue, queue history, GPU usage, machines, long-term history). The machine panels include the front node; Slurm figures are hidden when the collector stops.
- `cluster-health` fails when Grafana, the status collector, or the quota reader is broken or the snapshot is older than 2 minutes, and warns about stale quotas.
- Checked on the simulated cluster (everyday cluster, `/home` on the storage machine, Ubuntu 22.04 and 26.04) and reviewed by a separate agent.
- Files: `src/nanohpc/ansible/roles/{grafana,monitoring}/`, `src/nanohpc/ansible/roles/node_metrics/tasks/main.yml`, `src/nanohpc/ansible/site.yml`, `src/nanohpc/files/{cluster-monitor-snapshot,cluster_machine_status.py,cluster-home-quotas,cluster-health}`, `src/nanohpc/files/grafana/`, `src/nanohpc/deploy.py`, tests `test_dashboards.py`, `test_monitor_snapshot.py`, `test_machine_status.py`, `test_home_quota.py`, `test_deploy.py`, `test_sim.py`, `md/testing.md`.

## 2026-10-02: website from the configuration, over HTTPS (M5)

- The website (overview, user guide, jobs, machines, users, cluster usage with the 5-year history, policy) for any cluster: name, logo, login address, quotas, and cleanup days come from `cluster.yml` (`site.json`, and the user guide's `docs.md`/`policy.md` filled on the front node); machines, jobs, partitions, and policies from the 30-second status snapshot.
- Ships prebuilt in the nanoHPC package; `website.build: front` builds it on the front node from the source in the package (Node 24.21.0 pinned, `npm ci --ignore-scripts`, unprivileged account, only when the source changed).
- nginx on the front node at `https://<hostname><path>` (default `/cluster/`, configurable, `/` for the whole hostname), Grafana under `<path>grafana/` limited to the six dashboards and the calls they need, GET only, rate limits, security headers. HTTPS with the administrator's own certificate (checked on the administrator's machine before deploying) or Let's Encrypt (certbot 5.8.0, renewal twice a day). `allow` limits the site to listed networks; `forwarded_by` trusts the visitor address passed on by a lab web server.
- `nanohpc forwarding-rules cluster.yml` prints nginx, Apache, and Caddy rules so a lab website shows the site at `labwebsite.com/<path>`.
- `cluster-health` checks nginx, the pages over HTTPS, the certificate's expiry, and its renewal.
- Checked on the simulated cluster (everyday: Let's Encrypt through Pebble, renewal; Ubuntu 22.04: own certificate, path `/`, logo, front build; Ubuntu 26.04: `/hpc/`, `allow`; `/home` on the storage machine), with a real browser opening every page through the front node's nginx and Grafana; reviewed by a separate agent.
- Files: `src/nanohpc/website-source/`, `src/nanohpc/website/`, `src/nanohpc/ansible/roles/{website,test_acme}/`, `src/nanohpc/ansible/roles/grafana/tasks/main.yml`, `src/nanohpc/files/{cluster-website-docs,cluster-health}`, `src/nanohpc/{config,render,deploy,sim,cli}.py`, tests `test_website_nginx.py`, `test_website_docs.py`, `test_website_build.py`, `test_config.py`, `test_render.py`, `test_deploy.py`, `test_sim.py`, `tests/sim/`, `md/testing.md`.

## 2026-10-03: backup and Slack alerts (M6a)

- Every night the home machine mirrors `/home` with rsync over SSH (one mirror: deletions follow; excludes from `cluster.yml`) to the cluster's backup machine or to an outside SSH server. On a backup machine, an unprivileged `nanohpc-backup` account receives it through a forced command that only allows rsync into or out of the backup folder (and refuses restores through backed-up links), from the home machine's address; owners and permissions are kept as extended attributes (`--fake-super`). Restoring uses the same key. The result is a metric; a failed or old backup warns.
- Every machine runs `cluster-health` every 5 minutes and reports each check as a metric. Alert rules (a check failing or warning, health checks not running or never reported, machine metrics missing, failed or old backups) go to Alertmanager 0.34.1 on the front node, which posts to Slack when an alert starts and when it resolves (off by default; the webhook is `NANOHPC_SLACK_WEBHOOK` in a `.env` next to `cluster.yml`, kept out of Git and of the deploy's variables). Failed deliveries are reported by `cluster-health`.
- Checked on the simulated cluster (everyday cluster with a stand-in Slack server; Ubuntu 22.04 and 26.04; `/home` on the storage machine) and reviewed by a separate agent.
- Files: `src/nanohpc/ansible/roles/{backup,alerts}/`, `src/nanohpc/ansible/roles/{health,prometheus,accounts}/tasks/main.yml`, `src/nanohpc/files/{cluster-backup,nanohpc-backup-receive,cluster-health,prometheus-alert-rules.yml}`, `src/nanohpc/{config,deploy,render}.py`, tests `test_cluster_backup.py`, `test_backup_receive.py`, `test_cluster_health.py`, `test_prometheus_rules.py`, `prometheus_alert_rules_test.yml`, `test_config.py`, `test_deploy.py`, `test_render.py`, `test_sim.py`, `md/testing.md`.
