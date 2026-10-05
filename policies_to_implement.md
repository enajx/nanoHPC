# Policies to implement

More decisions agreed with the user on 2026-10-04 and 2026-10-05 on the REAL cluster
(`~/code/SLURM-REAL`), after the restart and update items already in `TODO.md`. Each section says
which `TODO.md` item it adds to or changes. Once these are in `TODO.md`, delete this file.

The REAL versions, all used live: `playbooks/restart-check.yml`, `restart.yml`, `update-report.yml`,
`update.yml`, `auto-updates.yml`, `front-home.yml`, `front-update.yml`, `front-restart.yml`, and
`roles/update_report/files/update-report`. The written steps are in
`~/code/SLURM-REAL/md/critical-lessons-learned.md`, section 4.

## Restart of a compute machine (adds to the restart item)

- The yes is the machine's name given in the run (`--confirm=HOST`); the run refuses without it.
- The check of `/home` after a restart lists `/home` and reads the NFS mount's source. Root cannot
  read inside a user's home on an NFS `/home` (root squash), so checking `/home/<admin>` always fails.
- Before the first live use, try every after-restart step by hand on the running machine.

## Updates (adds to the update item)

- Before installing, simulate the install (`apt-get -s`) and refuse it if it would remove a package
  or bring in a package from a care group or extra source that was not named for this run.
- NetworkManager's own libraries (`libnm`, `gir1.2-nm`) belong to the network group: upgrading
  them pulls in `network-manager`.
- MariaDB (it holds Slurm's job records) is a care group too.
- Install with `--only-upgrade`, keep existing settings files (`--force-confold`), and let nothing
  restart services during the install (`NEEDRESTART_MODE=l`).
- After installing, also check that no unit failed that did not fail before, and run the restart
  checks (the machine must still be safe to restart).
- If a restart is needed, do it with the restart procedure before updating the next machine.

## SSH server upgrades (new)

- Upgrade the SSH server only when the user names the SSH group for that run, one machine at a time.
- Before it: two separate background root connections to the machine, kept open (a running
  connection survives the SSH service restart), and an automatic undo on the machine: a timer set
  for 15 minutes that restarts SSH if it is not running and listening on port 22, and if that fails
  puts back a saved copy of the files the installed OpenSSH packages own, plus `/etc/ssh`.
- After it: check that the SSH listener runs the new program, `sshd -t`, and fresh root and admin
  logins; only then cancel the undo.

## Automatic security updates (changes the automatic-updates item)

- On every machine they refresh the package lists and install security updates daily, never
  restart, and never install NVIDIA or CUDA packages (unattended-upgrades' package block list).
- Where the NVIDIA module is a distribution package per kernel (on REAL: disco, Ubuntu's
  `linux-modules-nvidia-*`), they also skip kernel packages, so the kernel and the driver change
  only together through the update procedure. Where the module is built by `dkms`, kernels may
  update automatically.
- The live check asks the installed `unattended-upgrade` program itself (its matching function and
  the machine's apt settings) what it skips.

## Front node (changes "Front-node restarts stay manual" and the empty-`/home` item)

- The front node's `/home` line gets `nofail` and `x-systemd.device-timeout=30s`. The NFS server
  then still refuses to start without `/home`, because Ubuntu's generator adds
  `RequiresMountsFor=/home` from `/etc/exports`; so compute machines see no `/home` rather than an
  empty one. Any copy that mirrors `/home` must refuse to run when `/home` is not its own disk.
- The front node gets its own update procedure: plain updates while compute jobs run; care groups
  only when named and only with an empty queue; checks also cover the Slurm controller and
  accounting, the `/home` export, the website, and the forum.
- The front node gets its own restart procedure, used only after a backup front node can take over:
  restart checks, empty queue, then the queue is paused by draining the compute machines not already
  drained (node drains survive a Slurm controller restart; partition state changes do not), a notice
  in the Slack channel (not individual messages; 15 minutes before, shorter for one run if the user
  says so), the restart, then every check including the backup timers, a test job, the queue
  resumed, and an all-clear post. A failure before the restart resumes the queue; after it, the
  machines stay drained and the channel is told.
- During a planned front-node restart, pause the backup's "front node unreachable" alert on the
  standbys and start it again after the checks, or after any failure.
- When nobody can reach the building, do not restart the front node. Instead restart only the
  programs still using replaced `libc`/OpenSSL libraries, one at a time with two background root
  connections, leaving out everything on the login or network path: `sshd`, `systemd`,
  `systemd-logind`, `systemd-journald`, `dbus`, `systemd-resolved`, NetworkManager,
  `networkd-dispatcher`, `wpa_supplicant`, and the session managers. Restarting Docker restarts the
  forum container once; restart `containerd` first, then `docker`.

## How the agent works on the machines (new)

- Before running any command on a machine, read what it does; a "check" can also act.
- When another agent session works on the same machines, ask it before running changes, and do
  nothing on the front node or standbys during its drills.
- An edit tool must refuse to write a file that looks truncated (several sessions share a checkout).
