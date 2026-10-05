# Rogue users checks

Checks that a cluster's setup holds against users who bypass the rules or the intended use,
usually without meaning harm: someone who installs Tailscale to reach their job from home, keeps
a service running after their job ends, or fills a shared disk. Found on the source deployment on
2026-10-05 (read-only checks on the front node and five GPU compute nodes). Each check below says
what a user can do, why it matters, the default behaviour nanoHPC should suggest, and how to test it.

The intended use these checks protect: users log in only to the front node; all work on compute
nodes goes through Slurm, which keeps each job's processes, cores, memory, and GPUs together and
stops every process when the job ends.

General rule (decided 2026-10-05): start restrictive and open things up as users need them.

## What already holds in the source deployment

Worth keeping as nanoHPC defaults, and worth a test each:

- Only administrators (and root, by key) may SSH to compute nodes (`AllowUsers`).
- Only administrators are in `sudo`, `docker`, and `lxd`, and `/etc/sudoers` names no user.
- Slurm uses `proctrack/cgroup` and `task/cgroup` with `ConstrainCores`, `ConstrainRAMSpace`, and
  `ConstrainDevices`, so a job sees only its own GPUs and leaves nothing behind.
- Login limits on the front node: every user's login processes together get a CPU and memory cap
  (a `user-.slice` drop-in), root is exempt.
- Homes are private (`700` or `750`), and secrets (Munge key, `slurmdbd.conf`, JWT key, `.env`
  files) are readable only by root or their service.
- Apptainer runs without root (no setuid starter).

## 1. Lingering and user services

**What a user can do.** On Ubuntu any user may run `loginctl enable-linger` for themselves (polkit
action `org.freedesktop.login1.set-self-linger` is `yes` for everyone). Their user manager then
starts at boot and runs their services from `~/.config/systemd/user`. A Slurm job can turn this on
for a compute node. Those services run outside Slurm: all GPUs visible, no limits, not in
accounting, and they survive the job's end and reboots. On the source deployment a removed user's
Syncthing kept running this way.

**Default (decided 2026-10-05): block it for everyone except administrators.**
Deny `set-self-linger` to `unix-user:*`, then allow it again in one section per administrator
(`unix-user:<name>`). Seen on Ubuntu 22.04 (polkit 0.105): a `unix-group:sudo` section and a
one-line list `unix-user:a;unix-user:b` both left the administrators refused. Root is never asked.
On Ubuntu 22.04 polkit reads `.pkla` files (`/etc/polkit-1/localauthority/50-local.d/`); from 24.04
it reads JavaScript rules (`/etc/polkit-1/rules.d/`): check the polkit version and stop if it differs. Before turning it on, list `/var/lib/systemd/linger` and turn off any
lingering that is not wanted.

**Test.** As a normal user, on the front node and inside a job on a compute node:
`loginctl enable-linger <user>` must fail, and `/var/lib/systemd/linger` stays empty. Give the user
name: without it `loginctl` needs a login session, which a job (or `runuser`) does not have.

## 2. Cron and at

**What a user can do.** Without `/etc/cron.allow`, every user can use `crontab`. Ubuntu's cron PAM
setup (`common-session-noninteractive`) does not include `pam_systemd`, so cron jobs run in
`cron.service`, not in the user's slice: on the front node they get around the login limits, and a
Slurm job can add a crontab on a compute node that keeps running outside Slurm with all GPUs. `at`
works the same way when `atd` runs.

**Default (decided 2026-10-05): block both for everyone except administrators.**
`/etc/cron.allow` and `/etc/at.allow` list `root` and the administrators. The `crontab` command
checks these files, but the cron daemon still runs crontabs that already exist, so list
`/var/spool/cron/crontabs` and remove those of normal users (after telling them) first.

**Test.** As a normal user, on the front node and inside a job: `crontab -l` and `crontab <file>`
are refused, and `at now` is refused.

## 3. Outgoing tunnels

**What a user can do.** The machines can reach the internet freely, which users need (package
installs, model downloads, experiment trackers). So a user can start Tailscale (it works without
root, in userspace mode), cloudflared, ngrok, frp, zrok, bore, chisel, a VS Code Remote Tunnel
(`code tunnel`), or a reverse SSH connection (`ssh -R`) to a machine outside the cluster, from the
front node or inside a job. Whoever holds that tunnel can reach the job or the user's account
without passing the cluster's SSH, and an inbound firewall cannot see it. Blocking all outgoing
traffic is not practical.

**Default (decided 2026-10-05): detect, report, and stop.**
A regular check on every machine finds tunnel processes, stops only the tunnel program (a job
keeps running), and posts to the cluster's
notification channel (Slack in the source deployment), naming the user, the machine, the program,
and the job if there is one. The message ends with: "If this use is acceptable, ask the admins to
allow it." An allow list in the configuration (user, program, machines) lets administrators
permit a tunnel. The user is told the same thing directly when the channel supports it.

Detection is by program name and command line: `tailscale`, `tailscaled`, `cloudflared`, `ngrok`,
`frpc`, `zrok`, `bore`, `chisel`, `sshuttle`, `autossh`, `code tunnel` (not `code serve-web`), and
`ssh` with `-R` or `-w` (also as `-o RemoteForward` or `-o Tunnel`), which open a way back in; `ssh -L`
and `-D` do not. Administrators' tunnels are only reported, not stopped, so their work is never
broken; programs of root and system accounts (UID below 1000) are never touched, so a Tailscale the
administrators install as a system service stays (decided 2026-10-05). The check stops the program
with `SIGKILL` after checking that its PID still belongs to the same process (start time). In the
source deployment it runs as root every minute on every machine and publishes findings through
node_exporter's textfile folder; the front node reads them from Prometheus and posts each once, so
the Slack secret stays on the front node. A renamed program gets past it; the aim is users without bad intent.
VS Code's normal Remote-SSH server (`~/.vscode-server`) goes through the cluster's own SSH and is
not a tunnel. A VS Code Remote Tunnel (`code tunnel`) inside an interactive job is a tunnel.

VS Code in a job without a tunnel (decided 2026-10-05): the guide offers `code serve-web` (VS Code
in the browser, from VS Code's own `code` command-line tool) inside an interactive job, reached
like Jupyter with `ssh -L` through the front node, and desktop VS Code's "Existing Jupyter server"
with the Jupyter link for notebooks. Nothing VS Code runs on the front node. Later option: users may
SSH to a compute node only while they have a job there (`pam_slurm_adopt`, through the front
node), so desktop VS Code Remote-SSH reaches the job.

**Test.** As a normal user, inside a job and on the front node, start a harmless stand-in that
matches a tunnel pattern (for example a script named `cloudflared` that sleeps): it is stopped
within one check interval and one message is posted. With an allow-list entry it keeps running and
nothing is posted.

## 4. SSH forwarding on the front node

**What a user can do.** With OpenSSH defaults (`AllowTcpForwarding yes`, `PermitOpen any`), a user
can turn the front node into a proxy (`ssh -D`) into the institution's network from anywhere.

**Fair use.** `ssh -L` to a Jupyter or TensorBoard inside a job (on a compute node's cluster
address), and agent forwarding (`ssh -A`) for `git` with the laptop's key; agent forwarding is a
separate setting and is not affected by `PermitOpen`. VS Code Remote-SSH to the front node also
uses forwarding (to the front node's own `localhost`, through `-D` by default), so a list without
`localhost` stops it; that fits a guide that says not to run VS Code sessions on the front node.
X11 is rarely needed.

**Default (decided 2026-10-05, live in the source deployment): only what users need.** `PermitOpen` limited to the compute
nodes' cluster addresses (this also limits `-D`), no `-R` (`PermitListen none`), no X11, agent
forwarding kept. Administrators are exempt (a `Match` block), so a mistake never cuts off admin SSH,
and the regular check reports when an administrator does something users may not. It changes SSH
settings, so it goes the careful route. Put the `Match` block in the last file of
`sshd_config.d`: seen on OpenSSH 8.9, it then applies to users only and does not change the main
`sshd_config`'s later lines. List each compute node's address and every name users may type
(`PermitOpen` matches the name as given, without resolving it).

Notebooks never need to run on the front node: Jupyter runs inside an interactive job, and the
laptop reaches it with `ssh -L` through the front node. VS Code users can open the same link as an
"Existing Jupyter server", without a tunnel.

**Test.** `ssh -L` to a port on a compute node works; `ssh -D` to an outside host is refused.

## 5. Shared disk space without a quota

**What a user can do.** `/home` has quotas, but `/tmp` and `/var/tmp` are on the system disk with
none. On the front node, the system disk also holds the Slurm state, the accounting database, and
other services: one user who fills it (large temporary files from a package build, a dataset
downloaded to `/tmp`, core dumps) stops them for everyone. Jobs on compute nodes write to the
system disk's `/tmp` too, unless `TMPDIR` points to scratch.

**Default for the front node (decided 2026-10-05): a fixed-size `/tmp` plus an alert.** `/tmp`
is a `tmpfs` of a fixed size (16 GB in the source deployment, which has 125 GB of RAM) (its pages also count against each user's login memory limit), set
up at a planned restart because mounting over a live `/tmp` breaks open sockets (tmux, SSH agent).
The regular check warns when a system disk passes a threshold (90%) and names the largest users of
`/tmp` and `/var/tmp`.

**Jobs (decided 2026-10-05): `TMPDIR` points to the user's scratch folder** (`/scratch/$USER/tmp`,
made and set in the task prolog), so jobs do not write temporary files to the compute nodes'
system disks; the scratch cleanup removes it after some days unused, never while a job runs. A private `/tmp`
per job (Slurm `job_container/tmpfs`) is the other option.

A large download (say 50 GB) is not affected: it is written where the user saves it (home, under
its quota, or scratch in a job), not to `/tmp`. Only tools that stage files in `/tmp` first hit the
limit; the user then sets `TMPDIR` to a folder in home, or runs it in a job.

**Test.** A normal user writing to `/tmp` on the front node stops at the fixed size and the
system disk does not change; filling a test disk past the threshold posts one alert.

## 6. Other findings, not decided yet

- **Seeing other users' command lines (decided 2026-10-05: hide them).** Without `hidepid`, every
  user sees every process's arguments, including tokens typed on the command line. Mount `/proc`
  with `hidepid=invisible` on every machine (jobs of different users share compute nodes), with a `gid=` exemption group for administrators and the services that
  need it. Users rarely need others' command lines; `squeue` shows jobs. It does not touch SSH and
  is undone with `mount -o remount,hidepid=off /proc`. Seen in the source deployment: polkit runs
  as root on Ubuntu 22.04 and no monitoring service reads other accounts' processes, so only the
  administrators needed the group. Write the fstab line as
  `proc /proc proc nosuid,nodev,noexec,relatime,hidepid=invisible,gid=<number> 0 0`: with
  `defaults`, a remount drops systemd's nosuid, nodev, and noexec. Use the group's number, not its name.
- **Users adding SSH keys (decided 2026-10-05: not now, decide later).** A user can let someone
  else into their account by adding that person's key to `~/.ssh/authorized_keys`. The option is
  keys only from a list in the configuration (`AuthorizedKeysFile /etc/ssh/authorized_keys/%u`):
  only approved keys log in, and logins no longer need `/home`; but a user with a new laptop waits
  for an administrator, and a key not copied locks that user out. A lighter option: keep user keys
  and report new ones.
- **Leftover remote-desktop and tunnel packages** (VNC servers, a system Tailscale): remove them
  when a machine joins, or list them in the machine review (decided 2026-10-05: removed).
- **Accounts that are not people.** Some service accounts get UIDs above 1000 (`slurm` 64030,
  `munge`, Nix's `nixbld*`). Count only accounts with a login shell as users, or every check
  reports Slurm's own ports and processes.
- **Ports users open.** A Jupyter or web server on all addresses is reachable by the internet
  without an inbound firewall, and by every other user once cluster machines trust each other.
  Tell users to use a token (rule below).

## Regular check and report

**Default (decided 2026-10-05): yes.** A read-only check on every machine, on a timer, posts to
the notification channel when it finds:

- user processes on a compute node outside a Slurm job;
- crontabs, `at` jobs, lingering, or unit files in `~/.config/systemd/user` for normal users;
- tunnel programs or outgoing SSH tunnels (and stops them, section 3);
- ports opened by normal users;
- a system disk above its threshold, with the largest users of `/tmp` and `/var/tmp`.

A tunnel is posted once in the channel. A lasting finding is posted when first seen and again daily
while it lasts, in the channel and as a friendly private message to the user that says to check in
with the administrators if in doubt (decided 2026-10-05). Administrators are reported only for
what users may not do (a VS Code server on the front node, a tunnel), never for normal SSH sessions
or administration. Prometheus leaves out a label whose value is empty: the poster must read a
missing label as empty.

## Rules for users

**Default (decided 2026-10-05): yes.** The user guide's do's and don'ts say:

- no tunnels (Tailscale, cloudflared, ngrok, VS Code tunnels, reverse SSH); ask the admins if you
  need one;
- no services of your own, crontabs, or lingering; run work as Slurm jobs;
- a Jupyter or web server inside a job always uses a token or password.
