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
