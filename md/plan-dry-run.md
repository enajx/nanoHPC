# Plan: dry run before every deploy (M8a), remaining work

Transient: for the TODO item "Every deploy runs a read-only dry run first"; delete it when that item is done.
Branch `m8a-dry-run` has the dry run working (commits 29b70d8, e65cfe5, 8bd9ca6, plus later ones). A review
(2026-10-03) and the user's answers left this to do.

## Agreed with the user (2026-10-03)

- The dry run may refresh apt's package lists (needed to preview installs); this is the only write. Say so in
  md/testing.md, the site.yml header, SETUP-for-AGENTS.md, and the TODO item.
- One machine failing its dry run must not block the others: machines that failed are left out of the real
  run (`--limit` to the machines that passed) and listed at the end with what failed; exit non-zero so the
  automatic deploy records a failure and alerts. Except when the front node or the home machine fails: then
  nothing is deployed, with a message saying why (every other machine depends on them).

## Review findings to fix

1. Changes made by command/shell tasks are missing from the summary (check mode skips them with "Command
   would have run if not in check mode"): record them as would-change (unless changed_when is false). VM
   check: a home.quota_soft_gb change shows on the home machine in --dry-run.
2. `validate:` never runs in check mode (visudo -cf, promtool, amtool, luac5.3 -p, findmnt --verify): in check
   mode, run each validator on the rendered content in a temporary file under /tmp, then delete it.
3. A manual deploy's dry run does not pause automatic deploys: read the front node's last deployed commit in
   the dry run; in the real run's pause, if it changed, stop before applying ("an automatic deploy ran during
   this deploy's dry run; run nanohpc deploy again").
4. A real-run failure after a passed dry run ends with a summary (machines that failed, may be partly
   changed); a different exit code for a failed dry run than for a failed real run, recorded by the automatic
   deploy.
5. `nanohpc sim up/down --dry-run` must refuse --dry-run.
6. Read-only checks skipped in every dry run (home_client "Check that /home is the shared one", home_server
   "Check that /home is the home disk", "Check that the root filesystem's entry has usrquota", "Check the uv
   version"): give them check_mode: false; add a static test (every command/shell task with changed_when:
   false has check_mode: false or a check-mode condition).
7. scratch role: two tasks register `scratch_fstab`; give them different names. Remounting on an option change
   of a mounted disk is a separate TODO follow-up.
8. VM checks: the machines' state is unchanged by --dry-run (checksums of /etc, /var/lib/nanohpc, /srv, /opt,
   /usr/local and service states; only apt lists may differ); a dry run on an existing cluster that gains a new
   machine (skip if too slow).

Then: SimDeployTest, SimRedeployTest, SimAutoDeployTest from fresh VMs (one at a time; see md/testing.md for
the systemd-logind problem on some fresh 24.04 VMs), a separate review, docs (TODO [x], md/DONE.md, PROJECT.md on
main), merge into main, push.
