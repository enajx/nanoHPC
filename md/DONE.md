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
