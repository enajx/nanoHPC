# Setting up a cluster with nanoHPC: steps for AI agents

People set up a cluster with the wizard, `nanohpc init`. An agent follows the same steps here and writes
`cluster.yml` itself, from [examples/cluster.yml](examples/cluster.yml) (every setting, with comments) or
[examples/minimal.yml](examples/minimal.yml). Read the code when something here is not enough. Check the
result with `nanohpc validate cluster.yml` after each step.

The cluster is critical infrastructure: on the machines, only read until the administrator agrees to a change.
Show the administrator what you plan to change, and why, before you change it. Never format a disk; give the
administrator the command.

## Before you start

- You can reach every machine with `ssh <machine>` as an administrator who has sudo (key login, no prompts:
  `ssh -o BatchMode=yes`). nanoHPC uses the administrator's own SSH agent: ask them to load their key
  (`ssh-add`); `ssh-add -c` makes the agent ask before each use of the key, which you may suggest, but a deploy
  uses the key many times.
- Every machine runs Ubuntu 22.04, 24.04, or 26.04. NVIDIA drivers are installed already on GPU machines.

## 1. Machines

For each machine the administrator names (a hostname, an address, or a host from their `~/.ssh/config`):

- Read, without changing anything: `nproc`, `free -m` (MemTotal), `nvidia-smi --query-gpu=name --format=csv,noheader`,
  `lsblk -J -o NAME,PATH,SIZE,FSTYPE,MOUNTPOINT,MODEL,TYPE`, `/etc/os-release`, `ip -4 -o addr`.
- Write it under `machines:` with its address on the cluster network, `cpu`, `memory_mb` (a little below the
  total, as in the examples), and `gpu: {type, count}` for GPU machines (a short lowercase type such as
  `a6000` or `rtx4090`).
- Agree the roles with the administrator: one `front` (login, Slurm controller, monitoring, website),
  `compute` for machines that run jobs, `home` for the machine that serves `/home` (the front node, or a
  separate storage machine), and optionally `backup`.

## 2. Storage

- `/home`: on the home machine, either a disk of its own (`home.device`, for example `/dev/sdb`) or the root disk.
- Scratch on each compute machine: a disk (`scratch.device`) or an image file on the root disk
  (`scratch.image_gb`).
- An empty disk needs a filesystem before the deploy. Give the administrator the command; do not run it:
  `mkfs.ext4 -O quota <device>` for the home disk, `mkfs.ext4 <device>` for scratch.
- Explain the three kinds of storage if the administrator asks: home (small, safe, backed up), shared
  scratch (not built in nanoHPC yet), local scratch (per machine, per job). Advise a faster network
  (10 GbE or more) before adding shared storage, and moving `/home` off the front node as the cluster grows.

## 3. Users

- Under `users:`, each user's `name`, `uid` (the same number on every machine), and `ssh_keys`; administrators
  in `cluster.admins`.
- Read `getent passwd <user>` on every machine. If a user exists with another UID somewhere, run
  `nanohpc fix-uid cluster.yml <user> <machine>` (read-only: it shows what it would change), show the plan to
  the administrator, and only with their agreement run it again with `--apply`.

## 4. Partitions and policy

- Start from the defaults in the examples (`main` for any job, 24 hours). Add partitions only when the
  administrator asks (for example `interactive` on some GPU machines). Keep the policy defaults unless asked.

## 5. Website

- `cluster.website`: `hostname`, `path` (default `/cluster/`), `https` (`letsencrypt`, which needs the
  hostname to reach the front node on port 80 from the internet, or `own` with the administrator's
  certificate files), an optional `logo`, and `allow` to limit it to some networks.
- Suggest, as optional, a private network such as WireGuard or Tailscale for security and simpler
  administrator access.
- If the lab's own website should show the cluster site, `nanohpc forwarding-rules cluster.yml` prints the
  rules for their web server.

## 6. Extras

- `backup.to`: the cluster's backup machine or `user@host:/path`.
- `alerts.slack: true` with `NANOHPC_SLACK_WEBHOOK` in a `.env` next to `cluster.yml` (never commit `.env`).
- `auto_deploy` (repository over SSH, branch, minutes) and `nanohpc_version` when the administrator wants the
  front node to deploy from their configuration repository by itself.

## 7. Review and deploy

- `nanohpc validate cluster.yml` must pass. Show the administrator the whole file and agree it.
- `nanohpc deploy cluster.yml`: it first runs a dry run on every machine (it changes nothing except refreshing
  apt's package lists), then deploys the machines whose dry run passed. Machines whose dry run failed are left out,
  unchanged, and listed at the end with what failed (exit code 3); if the front node or the home machine fails its
  dry run, nothing is deployed. A failure after the dry run passed exits with code 4: those machines may be partly
  changed. Run it in a terminal the first time, if sudo still needs a password. `nanohpc deploy --dry-run
  cluster.yml` shows what would change and stops there.
- Later changes: after editing `cluster.yml`, `nanohpc deploy cluster.yml --only users` (or `policy`,
  `partitions`) applies only that part (`users` includes the website's quota and cleanup values), and
  `--only node NAME` sets up a new or changed compute machine (plus `/etc/hosts`, Slurm's configuration, the `/home`
  exports, and the monitoring lists on the others), with the same checks and dry run. Anything else (a new front node, home machine, or backup machine, or changed roles) needs a
  full `nanohpc deploy cluster.yml`.
