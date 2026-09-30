# Done

Archive of completed `TODO.md` items: what was built and the key files touched.

## 2026-09-30: design decisions for the port

- Agreed with the user how SLURM-REAL is ported into nanoHPC: scope (up to about 100 heterogeneous machines), role layout (optional separate `/home` storage and backup machines), Slurm built from source, Ubuntu 22.04/24.04/26.04, local users, `cluster.yml` with a wizard, admin-defined partitions, kept extras, HTTPS, rsync backup, MIT license.
- Agreed the testing setup: Lima VMs on macOS and Linux, a 6-node everyday test cluster, up to 20 compute nodes, fake GPUs.
- Files: `PROJECT.md`, `TODO.md`, `md/plan-port.md`, `md/testing.md` (was `md/testing-options.md`).
