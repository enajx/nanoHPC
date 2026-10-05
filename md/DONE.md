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

## 2026-10-03: automatic deploys (M6b)

- With `auto_deploy.enabled`, the front node checks a branch of the configuration repository every `every_minutes` (default 10, `main` by default), optionally right after a push through a GitHub webhook (signed, behind the website's nginx), and deploys the whole cluster by itself: no one logs in and no password is typed. It reads the repository with its own read-only key (the deploy prints it to add as a GitHub deploy key) and logs in to every machine as root with its own key, accepted only from the front node's address.
- A run takes a lock, builds only on the last deployed commit (a failed commit never blocks later ones, and is not retried), installs the nanoHPC version pinned in `cluster.yml` (`nanohpc_version`) the way the administrator installed nanoHPC (GitHub tag, PyPI, or a copied wheel), and records the result as metrics; a failure raises an alert and a `cluster-health` warning.
- Before any change, a deploy refuses another nanoHPC version than the pinned one, and an automatic deploy cannot turn automatic deploys off. A manual deploy pauses automatic deploys while it runs. Secrets from `.env` reach the front node even before their feature is on; an automatic deploy keeps the website's own certificate.
- Checked on the simulated cluster (a stand-in repository on the front node; the everyday cluster, the redeploy safety checks, and the less common settings on Ubuntu 24.04) and reviewed by a separate agent.
- Files: `src/nanohpc/ansible/roles/auto_deploy/`, `src/nanohpc/ansible/roles/{accounts,website}/`, `src/nanohpc/ansible/site.yml`, `src/nanohpc/files/{nanohpc-auto-deploy,nanohpc-deploy-hook,nanohpc-install,cluster-health,prometheus-alert-rules.yml}`, `src/nanohpc/{config,deploy,cli,sim}.py`, tests `test_auto_deploy.py`, `test_deploy_hook.py`, `test_nanohpc_install.py`, `test_config.py`, `test_deploy.py`, `test_website_nginx.py`, `test_prometheus_rules.py`, `test_sim.py`, `tests/sim/{auto-deploy,cluster-auto}.yml`, `md/testing.md`.

## 2026-10-03: setup wizard (M7)

- `nanohpc init CLUSTER_YML`: a full-screen terminal wizard (Textual) with a steps sidebar, the current step's form, and a footer pointing to `SETUP-for-AGENTS.md` for an installation by an AI agent. Seven steps: machines (probed over SSH, read-only: CPUs, memory, GPUs, disks, addresses, Ubuntu; a preparation checklist with hints and skip), storage (/home and scratch; disks in use never offered; mkfs commands shown, never run; storage advice), users (UIDs checked on every machine with the conflicts the deploy stops on), partitions and policy, website (with the private network recommendation), extras (backup, Slack, automatic deploys), review (valid means `nanohpc validate` passes; the next steps with the `ssh-add -c` tip). It writes a new `cluster.yml` or edits an existing one, keeping its comments; it asks before saving.
- `nanohpc fix-uid CLUSTER_YML USER MACHINE [--apply]`: a read-only plan, then the renumbering of that user and their files after `--apply`; records the old IDs first, so a run that stopped halfway can be finished; a shared /home over the network is never changed from that machine.
- `SETUP-for-AGENTS.md`: the same steps for agents writing `cluster.yml` from the examples.
- Checked with headless tests and on the simulated cluster (the probe, fix-uid, and the real wizard probing the machines) and reviewed by a separate agent.
- Files: `src/nanohpc/wizard/`, `src/nanohpc/{clusterfile,probe,fixuid,cli}.py`, `SETUP-for-AGENTS.md`, tests `test_wizard.py`, `test_clusterfile.py`, `test_probe.py`, `test_fixuid.py`, `test_sim.py` (SimSetupTest).

## 2026-10-04: dry run before every deploy (M8a)

- Every deploy, manual or automatic, first runs the playbook in check mode on every machine and prints what it would change there (including changes made by commands, and the validators such as visudo, promtool, amtool, and the Lua check run on the rendered files). Nothing changes in the dry run except apt's package lists. `nanohpc deploy --dry-run` stops after it.
- Machines whose dry run fails are left out of the real run and listed at the end (exit code 3); when the front node or the home machine fails, nothing is deployed. A failure in the real run says which machines may be partly changed (exit code 4); the automatic deploy's journal and alert say which run failed. A manual deploy stops before changing anything if an automatic deploy ran during its dry run.
- A first deploy of new machines can only be partly previewed (services, downloads, the Slurm build, and accounting that depend on earlier changes); the preflight checks always run in full.
- Checked on the simulated cluster (everyday, redeploy safety checks, automatic deploys, the release cluster, /home on the storage machine; a dry run leaves the machines' files and services unchanged) and verified by separate agents.
- Files: `src/nanohpc/deploy.py`, `src/nanohpc/cli.py`, `src/nanohpc/ansible/callback_plugins/nanohpc_record.py`, `src/nanohpc/ansible/site.yml`, `src/nanohpc/ansible/roles/*` (check mode, the dry_run_check role), `src/nanohpc/files/{nanohpc-auto-deploy,prometheus-alert-rules.yml}`, tests `test_deploy.py`, `test_playbook_check_mode.py`, `test_sim.py`, `md/testing.md`, `SETUP-for-AGENTS.md`.

## 2026-10-04: partial deploys (M8b)

- `nanohpc deploy CLUSTER_YML --only users|policy|partitions` or `--only node NAME` (also `nanohpc sim deploy`) runs one part, with the same preflight checks and the dry run first. `ONLY` in `deploy.py` maps each part to its tag in `ansible/partial.yml` and to the machines it runs on (`--limit`); the plays run role task files that `site.yml` also runs, so the result is the same as a full deploy for that part. `--only users` also refreshes the website's `site.json` and the docs and policy pages filled from it (home quotas, scratch cleanup days) on the front node, through `roles/website/tasks/site_data.yml`, which the website role's `main.yml` imports too.
- users: accounts, SSH and sudo keys, AllowUsers on every machine; home folders and quotas; scratch folders and caches; Slurm accounting users; the user lists of the status collector and the quota reader. policy and partitions (the same plays): slurm.conf and gres.conf on the Slurm machines, job_submit.lua and the QoS on the front node, Slurm restarted where its configuration changed, a fresh status snapshot. node NAME: everything on NAME as for a compute machine in a full deploy; on the others /etc/hosts, slurm.conf and the Slurm restart, the /home exports, the Prometheus targets, and the machine lists of the status collector and of automatic deploys.
- Refused before any change: `--only node` for a machine that is not a compute machine (the front node, the home machine, the backup machine), and any machine other than the new one that was never deployed or was deployed with other roles (read from `/etc/nanohpc/roles` by the probe). Nothing is deployed when the machine of `--only node` fails its dry run.
- Checked on the simulated cluster from fresh VMs (`SimPartialDeployTest`: a new user, a partition's time limit, a new machine, then a full deploy with no changes) and reviewed by a separate agent.
- Files: `src/nanohpc/{deploy,cli}.py`, `src/nanohpc/ansible/{partial,site}.yml`, task files in `src/nanohpc/ansible/roles/{base,home_server,scratch,slurm_controller,slurm_compute,monitoring,auto_deploy,health}/tasks/`, tests `test_partial_deploy.py`, `test_deploy.py`, `test_playbook_check_mode.py`, `test_sim.py`, `md/testing.md`, `SETUP-for-AGENTS.md`.

## 2026-10-04: `nanohpc check` (M8b)

- `nanohpc check CLUSTER_YML [--ssh-config PATH]` validates `cluster.yml`, then reads every machine in parallel over one SSH call each and changes nothing. It prints a table (ssh, health, users, mounts, gpus, slurm, version), machines with problems first, then lists of problems, warnings, and notes. Exit code 1 on any problem; warnings (cluster-health WARN lines, drained or down Slurm nodes) and notes do not change it.
- Health: cluster-health with sudo (tried the same way as the deploy), or as the login user with a note. Users: `getent` names and UIDs against `cluster.yml` (`probe.account_problems`, split from `probe.uid_problems`). Mounts: `/home` local on the home machine and NFS from it elsewhere (not on the backup machine), `/scratch` on compute machines. GPUs: nvidia-smi count, or the `/dev/nvidiaN` devices with a note. Slurm: `sinfo` on the front node. Version: `/etc/nanohpc/version`, which every deploy now writes. A machine with no cluster-health shows only "not deployed yet".
- Checked on the simulated cluster (`SimDeployTest`: no problems after a deploy, the version and GPU counts shown, nothing changed on the machines, a stopped munge on cpu1 reported first with exit code 1) and reviewed by a separate agent.
- Files: `src/nanohpc/{check,cli,probe,deploy}.py`, `src/nanohpc/ansible/roles/base/tasks/main.yml`, tests `test_check.py`, `test_deploy.py`, `test_sim.py`, `SETUP-for-AGENTS.md`.

## 2026-10-04: key-only root login for administrators (M8b)

- Administrators' keys from `cluster.yml` go into `/etc/ssh/authorized_keys/root` on every machine, next to the front node's automatic deploy key with its front-only limit. sshd reads root's keys only there, with password login, key commands, and certificate authorities disabled for root. Removing an administrator from `cluster.yml` removes their root key at the next deploy.
- Before nanoHPC's root setting is active, the dry run stops on a machine if a root key that sshd currently reads would lose access. It names the file, key type, fingerprint, and comment, with a hint. Cloud-init's exact refusal key is excluded. The stop applies to full deploys, `--only users`, and the new machine of `--only node`. The accounts role checks effective sshd settings and restores the old settings if validation fails.
- `SimRedeployTest` and `SimAutoDeployTest` each passed on Ubuntu 24.04 VMs. The redeploy test checked root login on every machine and on a compute machine while the front node's NFS server was stopped. A separate agent reviewed the feature, and its findings were fixed before these runs. The automatic deploy VM test now expects exit code 3 when a manual deploy stops before changes because an automatic deploy ran during its dry run.
- Files: `src/nanohpc/ansible/roles/{accounts,preflight}/`, `tests/test_root_login.py`, `tests/test_sim.py`, `SETUP-for-AGENTS.md`, `md/testing.md`.

## 2026-10-04: README cluster image

- The user chose turquoise pipes from three GIF previews using the website's default coral theme, then chose a static image with a transparent background. The README image shows Threadripper (a two-layer CPU machine), three 4-GPU machines all labelled H100 in the center, and FPGA on the right with no GPU bar. The live website map was not changed.
- The original GIF was checked in Chromium and decoded; it has been replaced by `assets/cluster.png`.
- The Tech stack badges sit on one source line, so Markdown readers do not turn each badge into a separate row. The user removed the Python, Ubuntu, React, Playwright, Vite, TypeScript, and uv badges.
- Files: `README.md`, `assets/cluster.png`.

## 2026-10-04: README feature descriptions

- Rewrote Features as eight short bullets, without bold lead-ins: one `cluster.yml`, Slurm scheduling, the front-end monitor, health checks, playbooks for cluster changes and backups, shared and scratch storage, Slack notifications, and an isometric cluster view. The cluster image follows the last bullet, before Set up.
- Checked the claims against `PROJECT.md` and `TODO.md`; the text describes supported policy changes without claiming system package updates.
- File: `README.md`.

## 2026-10-04: kept scratch job cleanup

- The daily scratch cleanup runs as each user on compute machines. It removes only kept `cluster-submit` job folders whose Slurm job ended more than `scratch.job_retention_days` days ago (7 by default). Recent, running, and unknown jobs stay. A failed accounting query stops before any deletion and makes the service fail. There is no preview mode.
- `cluster.yml` validation, the example, and the wizard expose the separate retention setting. A full deploy and `--only users` install the command, settings, wrapper, and service. Slurm job IDs are queried in batches.
- Direct command, config, deploy, and wizard tests passed. `SimDeployTest` and `SimPartialDeployTest` passed on Ubuntu 24.04 VMs: the first ran a failed job and its daily cleanup service, and the second restored the command and service through `--only users`. A separate agent reviewed the feature. The VM runs also led to a longer Alertmanager download timeout and a dry-run snapshot filter for the on-demand `fwupd` service.
- Files: `src/nanohpc/files/scratch-job-cleanup`, `src/nanohpc/ansible/roles/scratch/tasks/{main,users}.yml`, `src/nanohpc/{config,deploy}.py`, `src/nanohpc/wizard/{machines,state}.py`, `src/nanohpc/ansible/roles/alerts/tasks/main.yml`, `examples/cluster.yml`, `tests/{test_scratch_job_cleanup,test_config,test_deploy,test_sim,test_wizard}.py`, `md/testing.md`.

## 2026-10-05: apply changed mount options during deploy

- When nanoHPC changes an `/etc/fstab` entry for an already mounted `/home` or `/scratch`, it remounts that path. A new mount still uses `mount /home` or `mount /scratch`. The root-disk `/home` bind uses `remount,bind,nosuid,nodev`. The roles check the live `nosuid,nodev` flags and fail if they are missing.
- `SimHomeOnStorageTest` passed on Ubuntu 24.04 VMs: home disk, NFS `/home`, scratch disk, and scratch image gained `nosuid,nodev` in the same deploy after their fstab entries changed; a dry run changed nothing and a later deploy found no work. `SimHomeBindRemountTest` passed on Ubuntu 24.04 VMs for `/home` on the root disk. A separate agent reviewed the changed tasks.
- NFS-specific options such as transport and version cannot generally change through a remount. That remaining case is queued in [TODO.md](../TODO.md) (at the repo root).
- Files: `src/nanohpc/ansible/roles/{home_client,home_server,scratch}/tasks/main.yml`, `tests/test_sim.py`, `md/testing.md`.

## 2026-10-05: storage and quotas after reboot

- Ubuntu 24.04 VM checks passed from a fresh cluster: restart the home server, front node, and two compute nodes one at a time after a deploy. They check the home disk, NFS mounts, active quota limits, scratch disk and image, and files read and written after boot. A separate VM test passed after restarting the front node when `/home` is a bind mount on its root disk.
- The reboot test refreshes Lima's forwarded SSH port after each restart. `sim up` checks for the known stuck `systemd-logind` process on fresh Ubuntu VMs and restarts it when CPU use stays high.
- Files: `tests/test_sim.py`, `src/nanohpc/sim.py`, `md/testing.md`.

## 2026-10-05: XFS disks and NFS quota enforcement

- A fresh Ubuntu 24.04 VM cluster passed with an XFS home disk and XFS scratch disk. The test checks the configured home quota limits, reads and writes files through NFS and on local scratch, then confirms that an over-limit write through NFS fails with a quota error.
- The test uses its own simulated cluster so it can format test disks without touching another test's disks. A separate agent reviewed the test and its quota check.
- Files: `tests/sim/xfs-quota.yml`, `tests/test_sim.py`, `md/testing.md`.

## 2026-10-05: monitor only, without Slurm

- `nanohpc deploy-monitor` installs machine and GPU metrics, health checks, alerts, a status snapshot, Grafana, and an HTTPS website on directly used Ubuntu machines. One named monitor host serves the shared services and can also run work. It does not set up Slurm, accounts, SSH access, or storage. Existing nanoHPC Slurm setups are refused.
- `cluster.mode: monitor` uses machine addresses, optional aliases, and existing login names. The example, `nanohpc init --mode monitor` wizard, validation, `nanohpc check`, dry run, `--only node NAME`, and explicit GPU hardware change acceptance support this mode. The website shows Overview, Machines, Usage, and Users, with measured GPU data and monitor-specific history. It has no job or queue claims.
- A separate agent reviewed the implementation. The focused tests passed. `SimMonitorDeployTest` passed on three Ubuntu 24.04 VMs, checking the live HTTPS site, snapshot, measured fake-GPU metrics, and absence of Slurm and listed user accounts. A node-only dry run and apply also passed on the VMs.
- Files: `src/nanohpc/{cli,config,clusterfile,deploy,render,sim,check}.py`, `src/nanohpc/wizard/`, `src/nanohpc/ansible/monitor.yml`, monitoring roles and collectors, monitor Prometheus rules and Grafana history, `src/nanohpc/website-source/`, `examples/monitor.yml`, `tests/`, `README.md`, `md/testing.md`.

## 2026-10-05: home quotas across kernel upgrades

- The home server installs the extra-module package for its running kernel and a package that tracks quota modules with later Ubuntu kernel updates. It uses the installed kernel image package's update track and stops with a clear error if tracks conflict or the matching package is unavailable.
- A fresh Ubuntu 24.04 VM test passed: deploy twice, upgrade to a newer kernel, reboot, check the separate `/home` disk, active quotas, and a user's file, then deploy again. A separate agent reviewed the package selection.
- Files: `src/nanohpc/ansible/roles/home_server/tasks/main.yml`, `tests/sim/kernel-quota.yml`, `tests/test_sim.py`, `md/testing.md`.

## 2026-10-05: restart checks before a restart

- `nanohpc check CLUSTER_YML --before-restart` reads every machine over SSH and reports pass or failure for fstab, GRUB's selected kernel, the NVIDIA module on GPU machines, saved versus live network settings, and automatic update restart settings. A separate `/boot` needs `nofail` and a live mount. Netplan and persistent NetworkManager profiles support static and DHCP settings; temporary or unverifiable settings fail the check. The command changes nothing.
- The focused CLI tests passed. `SimRestartCheckTest` passed on three Ubuntu 24.04 VMs: it reached every machine, reported an injected fstab problem on the front node, and left fstab unchanged. Lima's cloud-init Netplan file changed after boot, so the strict network check reported that it could not verify those VM settings. A separate agent reviewed the checks, and its findings were fixed.
- Files: `src/nanohpc/{cli,restart_check}.py`, `tests/{test_restart_check,test_sim}.py`, `md/testing.md`.

## 2026-10-05: confirmed restart of one compute machine

- `nanohpc restart CLUSTER_YML MACHINE --confirm MACHINE` accepts one compute machine, never the front node. It checks the SSH target's configured address, takes a cluster-wide lock, drains the Slurm node, and waits for its allocated jobs to finish. It requires the saved boot checks to pass before requesting a reboot, then checks a new boot ID and the target address again.
- After reboot it checks fresh root and the invoking administrator's login, the selected kernel, every configured GPU through `nvidia-smi`, shared `/home`, `/scratch`, and `slurmd`. The caller must log in as an administrator listed in `cluster.yml`; checking another administrator's fresh login would need that person's private key. A temporary Slurm maintenance reservation keeps ordinary jobs off the node while it runs a 1-GPU test job, or a CPU job on a CPU-only node, as the invoking administrator. It removes the reservation and unlocks after the test passes. Any failed check or job re-drains the node; if re-draining fails, it keeps the reservation. Front-node restarts remain manual after the queue is empty, users are told, and the read-only restart checks pass.
- The focused CLI tests passed and a separate agent reviewed the safety paths. On Ubuntu 24.04 VMs, an unsafe fstab entry stopped before reboot and left the CPU node drained. A fresh-cluster test waited for a real running job to finish, rebooted the CPU node, and completed a real Slurm test job. Lima's base image changes saved boot settings after startup, so the success test bypassed only the boot-settings precheck; the production command did not.
- Files: `src/nanohpc/{cli,restart}.py`, `tests/{test_restart,test_sim}.py`, `md/testing.md`.
