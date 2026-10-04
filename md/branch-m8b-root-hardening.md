# Branch m8b-root-hardening: key-only root login for administrators

This file exists only on this branch. When the branch is merged into `main`, move what is still true into
`PROJECT.md`, write the `md/DONE.md` entry, mark the `TODO.md` item done, and delete this file.

## What the branch holds

It contains `m8b-root-login` and `m8b-root-keys` (both pushed, superseded by this branch):

- Administrators' keys from `cluster.yml` in `/etc/ssh/authorized_keys/root` on every machine, next to the front
  node's automatic deploy key with its `from=` limit. sshd reads root's keys only from that file (`Match User root`
  in `roles/accounts/templates/sshd_nanohpc.conf.j2`), with passwords, key commands, and certificate authorities
  turned off for root there too, whatever other sshd files say.
- The deploy checks `sshd -T` for root and for the login account, and puts the old settings back when the check
  fails (also when another file refuses root with `DenyUsers`/`DenyGroups`).
- The dry run stops on a machine when the files sshd reads for root today hold keys that are not administrators'
  keys (agreed with the user 2026-10-04). cloud-init's exact refusal key ("Please login as the user ...", exit
  142) does not count. The stop runs in a full deploy, `--only users`, and the new machine of `--only node`, not
  in `--only policy` or `--only partitions`.

## Verification status

- Fast tests pass (`tests/test_root_login.py`, `test_playbook_check_mode.py`, `test_partial_deploy.py`, full suite).
- A separate review was done; its six findings are fixed on this branch.
- `SimRedeployTest` ran on `m8b-root-login` before the root-key stop and the review fixes: root login worked as
  designed; two subtests failed because the test logged in as bob with the shared key after giving him his own
  (fixed in the test, commit 103fd90). The later steps of that subtest (a key in `/root/.ssh` ignored, root login
  on gpu4 with the front's NFS server stopped) have not run yet.
- Not run yet on this branch: `SimRedeployTest`, then `SimAutoDeployTest` (one at a time, Ubuntu 24.04).

## Left to do before the merge

1. Run `NANOHPC_SIM=1 uv run python -m unittest tests.test_sim.SimRedeployTest`, then `SimAutoDeployTest`, from
   this worktree (`npm ci` in `src/nanohpc/website-source` first). Fix failures.
2. Merge into `main`. In `PROJECT.md`: the automatic deploys line says "root logins only from the front node";
   correct it (the front's key is limited to the front; administrators log in as root from anywhere with their
   keys), and add the built line for root login.
