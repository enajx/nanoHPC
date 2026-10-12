# Administrator guide

This guide sets up a new nanoHPC Slurm cluster. For monitoring existing machines without Slurm or account changes, use the monitor-mode steps in the [README](../README.md) (at the repo root) and [monitor example](../examples/monitor.yml) (at the repo root).

## Requirements

- One front node and at least one compute node, each running Ubuntu 22.04, 24.04, or 26.04. Compute nodes may be CPU-only or have NVIDIA GPUs. Set up the NVIDIA driver on GPU machines before nanoHPC; `nvidia-smi` must see the GPUs.
- A machine from which you can SSH to every node as an account allowed to use `sudo`. This can be your workstation or the front node. Install [uv](https://docs.astral.sh/uv/) there, then install nanoHPC as shown in the [README](../README.md) (at the repo root). nanoHPC needs Python 3.12 or newer; `uv tool install` can provide it.
- An SSH key for each administrator and each user. Load your administrator key with `ssh-add` before a deploy. nanoHPC forwards your SSH agent to the nodes so your key can unlock sudo after the first setup. `ssh-add -c` is optional: it asks you to confirm each use of the key, which can mean many prompts during a deploy.
- A cluster address for every machine. Choose a hostname and HTTPS setup for the website. Let's Encrypt needs the website hostname to reach the front node on port 80; you can instead provide your own certificate and key.
- A plan for `/home` storage and each compute node's local scratch. The front node can serve `/home`, or a separate storage machine can. nanoHPC does not format disks. If you name a separate disk in `cluster.yml`, prepare its filesystem first and check its device path. A compute node that already has local files under `/home` needs those files moved before an NFS `/home` is mounted there.

The [setup wizard](../SETUP-for-AGENTS.md) (at the repo root) and the [full example](../examples/cluster.yml) (at the repo root) describe each setting. The agent guide also lists read-only machine checks and the storage choices. Use a faster network, preferably 10 GbE or more, before adding shared storage.

## Describe the cluster

Run the wizard from the machine that can SSH to the nodes:

```sh
nanohpc init cluster.yml
nanohpc validate cluster.yml
```

The wizard probes machines without changing them. It records their roles, addresses, CPU, memory, and GPUs; the home and scratch layout; users and their UIDs; partitions and policy; and the website. You can instead copy [examples/minimal.yml](../examples/minimal.yml) (at the repo root) or the [full example](../examples/cluster.yml) (at the repo root) and edit it. Keep `cluster.yml` as the source of truth for later changes.

Check these values before the first deploy:

1. `cluster.admins` names users whose keys can administer every machine. Each user has the same UID on every machine. The wizard shows UID conflicts; `nanohpc fix-uid cluster.yml USER MACHINE` lists the local files it would change without changing them. Review that list before using `--apply`.
2. `machines` gives one machine the `front` role, at least one the `compute` role, and one machine the `home` role. The front node can also be the home server. Add a `backup` role or an outside SSH backup destination if wanted. For each GPU compute node, set its type and count to match `nvidia-smi`.
3. `home` sets usage notice and new-job threshold values. `scratch` sets cleanup and kept-job retention. `partitions` and `policy` set where and how jobs run. The example comments show the available fields.
4. `cluster.website` sets the hostname, path, HTTPS choice, and optional network allow list. If you use your own certificate, its files must be available on the machine running nanoHPC.
5. Keep tokens and webhook secrets in `.env` next to `cluster.yml`; do not commit that file. Optional automatic deploys need a configuration repository and a pinned `nanohpc_version`.

Run `nanohpc validate cluster.yml` again after every edit. It checks the file without connecting to machines.

## First deploy and administrator access

Use a terminal for the first deploy, so you can type a sudo password if the nodes still need one:

```sh
nanohpc deploy cluster.yml --dry-run
nanohpc deploy cluster.yml
nanohpc check cluster.yml
```

The dry run checks every machine before applying changes. It may refresh APT package lists, but it does not apply the setup. The normal deploy performs its own dry run first. If the front node or home server fails that check, nothing is deployed. If another machine fails, it is left unchanged and listed at the end. A failure after a successful dry run may leave some machines partly changed; read the reported machine and task before trying again.

nanoHPC connects using your normal `ssh MACHINE` account and adds no passwordless sudo rule. On a new machine, that account needs its existing sudo access. If sudo needs a password, nanoHPC asks once in an interactive terminal and keeps it only for that run. Without a terminal, it stops before changing anything and explains how to run the command yourself.

After setup, an administrator's loaded SSH key can unlock sudo through the forwarded agent. Administrators in `cluster.yml` also get key-only root login on every machine for recovery, with their keys stored locally so root can log in even if `/home` or the front node is down. With automatic deploys enabled, the front node gets its own root key, accepted only from the front node's address. Ordinary users log in to the front node; they do not get direct compute-node login.

On the front node, ordinary users can forward local SSH connections only to compute machines listed in `cluster.yml`. Remote, X11, and socket forwarding are off for them. Desktop VS Code Remote-SSH cannot connect to the front node. The website's interactive notebook helper uses an allowed tunnel to a compute machine for Jupyter or browser-based VS Code inside a Slurm job. Administrators and root keep unrestricted forwarding.

If `ssh-add -c` is enabled for your key, approve each requested use during a deploy. If you cannot answer the prompts, load a key without that option for this run.

## Add or change a machine

Add the machine to `cluster.yml` with its role, address, hardware, and storage. Make sure SSH, sudo, any disk filesystem, and the NVIDIA driver are ready. Then run:

```sh
nanohpc validate cluster.yml
nanohpc deploy cluster.yml --only node NAME --dry-run
nanohpc deploy cluster.yml --only node NAME
nanohpc check cluster.yml
```

`--only node NAME` sets up one new or changed compute node and updates the shared configuration on the other machines. For a new or changed front, home, or backup machine, or changed machine roles, use a full `nanohpc deploy cluster.yml`. For changes only to users, policy, or partitions, `--only users`, `--only policy`, and `--only partitions` are available. Each deploy runs its checks first.

## Common problems

| What you see | What to check |
| --- | --- |
| SSH cannot reach a machine | Check `ssh MACHINE` from the deploy machine, its address or SSH config, and the loaded key. Use `--ssh-config FILE` if the cluster needs a separate SSH config. |
| Sudo needs a password and no terminal is available | Run the deploy command yourself in a terminal. The first setup can ask once; later, load an administrator key with `ssh-add` so the forwarded key can unlock sudo. |
| A user has different UIDs across machines | Run `nanohpc fix-uid cluster.yml USER MACHINE` to see the affected local files. It refuses while that user has running processes. After review and once their processes have stopped, run it with `--apply`, then validate and deploy again. |
| A root SSH key would be removed | nanoHPC refuses an unlisted root key before changing SSH. Add its public key to an administrator in `cluster.yml`, or remove the old key from the named file after confirming how you will retain access. Retry the dry run. |
| GPU count or driver check fails | Run `nvidia-smi` on that machine. Install or repair the driver, then make `machines.NAME.gpu.count` match the installed GPUs. |
| `/home` or scratch mount check fails | Check that the named device has the expected filesystem and that no existing `/home` data would be hidden. nanoHPC will not format a disk. If a live NFS `/home` mount is busy during a changed mount option, wait for use to stop and retry; the compute node remains drained after a failed switch. |
| Website certificate or access fails | Check the configured hostname, port 80 for Let's Encrypt, own-certificate file paths, and `cluster.website.allow`. Run `nanohpc check cluster.yml` for machine and service problems. |
| Deploy reports exit code 3 or 4 | Code 3 means one or more machines failed the dry run and were left out. Code 4 means a later apply failed and some changes may already be present. Use the machine and task in the output, correct the cause, and run the dry run again. |

For the test cluster and its VM checks, see [testing.md](testing.md). For an agent-led installation, use [SETUP-for-AGENTS.md](../SETUP-for-AGENTS.md) (at the repo root).
