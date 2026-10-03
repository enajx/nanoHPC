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

## M4 (monitoring): choices made by the agent, to review

The user said to go ahead (2026-10-01). These follow the source deployment's design (two Prometheus instances, node exporters over mutually authenticated TLS, GPU and machine-spec collectors, the 30-second status collector, read-only Grafana) and are recorded for the user to review. M4 is done in two steps: M4a metrics, M4b status collector and Grafana.

- Certificates are created and renewed by nanoHPC: a private certificate authority on the front node (its key never leaves the front node) signs one certificate per machine; a deploy renews any certificate with less than 30 days left. The source deployment exchanged self-signed certificates by hand and never renewed them.
- Prometheus scrapes every machine (compute, storage, and backup machines), not only the compute machines, so the home disk is monitored too.
- Prometheus 3.15.0, node_exporter 1.12.1, and Grafana 13.2.3, for x86_64 and ARM64, checked against their published SHA-256.
- On the simulated cluster, machines with fake GPUs run a fake GPU collector that reports made-up utilization, memory, temperature, and power. The machine-spec collector works on machines without GPUs.

- M4b (status collector and Grafana): the source deployment's 30-second status collector (`status.json` for the website, `machines.md`, and Slurm figures for Prometheus), machine status rules, and home quota reader, without its backup card (nanoHPC's backup is M6) and with the accounting start taken from the cluster's first deploy instead of a fixed date. Grafana 13.2.3, anonymous read-only, on the front node's localhost, served under `/grafana/` for the website (M5); the source deployment's six dashboards with `nanohpc-` identifiers and no site names, working for any number of machines.
  - Home quotas reach the front node through Prometheus: the quota reader runs on the home machine (it needs the disk) and writes `cluster_home_quota_*` metrics, so the user cards show quotas in both `/home` layouts. The source deployment wrote a file on the front node, which only works when /home is there.
  - The status collector runs as its own account and writes its Slurm figures into its own metrics folder (`/var/lib/nanohpc/monitor-textfile`, read by the front node's exporter), so it cannot touch the other collectors' files.
  - What each machine must run and mount is generated from `cluster.yml` (front: Slurm controller, accounting, MariaDB; compute: slurmd, `/scratch`; the home machine: NFS server; `/home` wherever it is mounted).
  - Storage machines (home, backup) appear in the status with the role "Storage". CPU-only machines show GPU use "Not applicable".
  - GPU-hour totals count from the first deploy, recorded once in `/etc/nanohpc/accounting-start`.

## Agreed for M5 (website), 2026-10-02

- **Build**: the website ships prebuilt in the nanoHPC package (no Node on the cluster; the content comes from `cluster.yml` when the page loads). `cluster.yml` can choose to build it on the front node instead.
- **Address**: the front node serves the website itself over HTTPS at `https://<hostname>/<path>/`. The path is configurable, default `/cluster/`; Grafana is under it (`<path>grafana/`). nanoHPC prints ready-made forwarding rules (nginx, Apache, Caddy) so a lab's own website can show the cluster site at `labwebsite.com/cluster/`. The wizard (M7) asks for these settings.
- **Access**: by default anyone who reaches the site can read it, no login, read-only. `cluster.yml` can limit it to listed networks. The wizard recommends (says it is not required) a private network such as WireGuard or Tailscale, for security and simpler administrator access to the cluster.
- **Settings** (`cluster.website`, agreed 2026-10-02): `path` (default `/cluster/`), `allow` (networks that may open the site; empty means anyone), `forwarded_by` (address of the lab's web server that forwards to the site; only from it is the passed-on visitor address trusted for `allow`), `build` (`package` or `front`, default `package`), next to the existing `hostname`, `https`, `certificate`, `certificate_key`, `logo`, `login_address`.
- **HTTPS testing**: the simulated cluster runs Pebble (Let's Encrypt's test server) so the real certificate request and renewal are tested; the administrator's own certificate is tested with a test certificate.
- From the source deployment: its website (pages, `status.json` reading, Grafana embedding, user guide), with every site-specific part (names, logo, hostnames, the forum software front layer, Slurm-web) removed or taken from `cluster.yml`.

## M5: choices made by the agent, to review

- nginx from Ubuntu's packages, one site file (`/etc/nginx/conf.d/nanohpc-website.conf`); a configuration that fails `nginx -t` is put back and the deploy stops. Port 80 only answers Let's Encrypt's challenge and redirects to HTTPS. The routes, read-only methods, rate limits, security headers, and the Grafana route list follow the source deployment.
- Let's Encrypt: certbot 5.8.0 (pinned, in its own uv environment), webroot challenge, no email address registered, renewal checked twice a day with an nginx reload after a new certificate. Let's Encrypt must reach the front node's hostname on port 80; a front node that is not reachable from the internet uses `https: own`.
- Certificate, key, and logo paths in `cluster.yml` are on the administrator's machine, relative to `cluster.yml`, and `nanohpc validate` reports missing files.
- `build: front`: Node 24.21.0 (pinned), `npm ci --ignore-scripts` and the build run as an unprivileged account, only when the source changed; the source ships in the nanoHPC package.
- `cluster-health` on the front node: nginx running and the site answering over HTTPS (fail), the certificate valid for 14 more days and the last renewal check (warn), the renewal timer (fail).
- Simulated cluster: Pebble and its test DNS server run on the front node; for `https: own`, `sim up` makes a test certificate. The test clusters cover the default path with Let's Encrypt (everyday), `path: /` with an own certificate, a logo, and `build: front` (Ubuntu 22.04), and another path with `allow` (Ubuntu 26.04).

## Agreed for M6 and admin access, 2026-10-02

- **Automatic deploys**: full-cluster pull. The source deployment's front node polls its GitHub repository (read-only deploy key, clean checkout, fast-forward only, a lock, failed commits not retried) and runs Ansible locally as root, but only for the front node's safe parts. nanoHPC goes further: the front node deploys every machine, so it gets root SSH access to every machine (the user accepted that a compromised front node then reaches every machine). `nanohpc deploy` from the administrator's machine stays. Trigger, key restrictions, and what runs unattended are planned when M6 starts.
- **Dry run first** (agreed 2026-10-02): the cluster is critical infrastructure, so every deploy, manual or automatic, first runs a read-only dry run (Ansible check mode). `nanohpc deploy` continues to the real run on its own when the dry run passed; an automatic deploy applies only if the dry run passed, and otherwise stops and alerts.
- **M6 (agreed 2026-10-02)**: nightly rsync backup as one mirror of `/home`; Slack alerts sent by the front node only (every machine reports its health checks as metrics), on a new failure or warning and on recovery, plus failed backups and automatic deploys, with the webhook in a `.env` next to `cluster.yml` copied to the front node; automatic deploys check a branch every 10 minutes (set in `cluster.yml`, default `main`, a stable or release branch suggested), with an optional GitHub webhook, and use the nanoHPC version pinned in the configuration repository. The dry run comes in M8; automatic deploys apply without it until then.
  - `cluster.yml` (agreed 2026-10-02): top-level `nanohpc_version`; `auto_deploy.branch` (default `main`), `auto_deploy.every_minutes` (default 10), `auto_deploy.webhook` (default false; GitHub calls `https://<website hostname><path>deploy-hook`, signed with `NANOHPC_DEPLOY_WEBHOOK_SECRET` from `.env`); `alerts.slack` reads `NANOHPC_SLACK_WEBHOOK` from `.env` next to `cluster.yml`. Done in two steps: M6a backup and alerts, M6b automatic deploys.
- **Admin recovery access**: key-only root login for the administrators in `cluster.yml` on every machine, keys on local disk, so it works when the front node or `/home` is down (like the source deployment's `ssh_access`).

## M6a (backup and alerts): choices made by the agent, to review

- Alerts: every machine runs `cluster-health` every 5 minutes and writes each check's state as metrics; Prometheus alert rules (a check failing or warning, health checks not running, a machine's metrics missing, a failed or old backup) go to Alertmanager 0.34.1 on the front node's localhost, which posts to Slack when an alert starts and when it resolves, and does not repeat it. The webhook is a root-owned file on the front node that only Alertmanager can read. The source deployment's alerts were an hourly check of its backup and scratch use, without recovery messages.
- Backup to the cluster's backup machine: the home machine pushes `/home` with rsync over SSH (its own key) to an unprivileged `nanohpc-backup` account on the backup machine. That key may only run rsync into the backup folder, from the home machine's address; file owners and permissions are kept with rsync's `--fake-super` (stored as extended attributes), so the backup machine needs no root login. Restoring uses the same key from the home machine.
- Backup to an outside SSH server (`user@host:/path`): the deploy prints the home machine's public backup key to install there; owners are kept only if that account can keep them.
- The backup's result (time of the last success, exit code) is a metric; `cluster-health` on the home machine warns when the last successful backup is more than 26 hours old.

## Agreed for M6b (automatic deploys), 2026-10-02

- The front node installs the nanoHPC version pinned in the configuration repository the same way the administrator installed nanoHPC: from the nanoHPC GitHub repository at tag `v<version>` (the default; public by then), from PyPI, or, for a local checkout (development, the simulated cluster), from a wheel the deploy copies. A small install script on the front node does it.
- The front node logs in to every other machine as root with its own key, accepted only from the front node's address (key-only root login, keys on local disk).

## M6b: choices made by the agent, to review

- The configuration repository is any Git repository over SSH (`git@github.com:lab/cluster-config.git`), with `cluster.yml` at its root; the front node reads it with its own read-only key (GitHub "deploy key"; the deploy prints it to add). The front node's checkout stays clean and fast-forwards only; the `.env` with the secrets is copied next to it by `nanohpc deploy` and kept out of Git.
- A run takes a lock (one deploy at a time; a manual `nanohpc deploy` on the front node waits), deploys a new commit once, does not retry a commit that failed until a newer one arrives, and records the result as metrics (an alert when it fails).
- An own website certificate (`https: own`): its files are on the administrator's machine, so automatic deploys keep the certificate and key already on the front node (from the last `nanohpc deploy`); a new certificate needs a manual deploy. Private keys never go into the configuration repository.

## Agreed for M7 (setup wizard), 2026-10-03

- `nanohpc init` is a full-screen terminal app built with Textual (Python, installed with nanoHPC, tested headless); the look follows the website's neobrutalism style where it fits.
- The administrator lists the machines (names or addresses, or hosts from their ~/.ssh/config); the wizard checks SSH and probes each one.
- It writes a new `cluster.yml` or opens an existing one to change it.
- The wizard is for people. Agents write `cluster.yml` from the examples, following `SETUP-for-AGENTS.md`, which lists the same steps as the wizard; AGENTS.md links to it.
- Steps: machines (SSH check and probe, roles, GPU types), storage (where /home lives, scratch, the commands to make filesystems with the option to skip, storage advice), users (names, UIDs, SSH keys, administrators, UID check on every machine), partitions and policy (simple defaults), website (hostname, path, HTTPS, logo, access; a private network such as WireGuard or Tailscale recommended, not required), extras (backup, Slack, automatic deploys, the pinned nanoHPC version), review (the cluster.yml, validated, the next step `nanohpc deploy`, and the `ssh-add -c` tip). Opening an existing cluster.yml shows the same steps, filled in.
- Layout (agreed 2026-10-03): the seven steps in a sidebar on the left with their state, the current step's form on the right, and a footer with the keys; a second footer line says that for an installation by an AI agent, see `<path>/SETUP-for-AGENTS.md`.
- (Agent's choice, to review) `cluster.yml` is read and written with ruamel.yaml, so editing an existing file keeps its comments and order; Textual 8.2.8 for the terminal app.
- `nanohpc fix-uid` is built in M7: the wizard explains a UID conflict and, after the administrator confirms, renumbers that user on that machine (after a read-only check).

## Agreed for M8 (commands and release), 2026-10-03

- Order: the dry run before every deploy first, then partial deploys, then key-only root login for administrators, then the release work.
- Partial deploys (`nanohpc deploy --only users|policy|partitions`, `--only node NAME`) replace the separate add-node, users, policy, and partitions commands.
- `nanohpc check` stays a command of its own: read-only, it reports what differs between the machines and `cluster.yml` (services, users, mounts, GPU count). The dry run before every deploy is separate.

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
