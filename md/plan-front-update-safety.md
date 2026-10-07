# Temporary plan: front-node update and home recovery

This file holds the decisions agreed for the queued front-node update and `/home` recovery work. Delete it when those TODO items are complete.

- A front-node update allows ordinary updates while compute jobs run. Named care-group updates require an empty queue before they start. Pause new job starts during those updates, let new submissions wait, restore scheduling after checks pass, and leave it paused on failure.
- Use the existing `nanohpc update CLUSTER_YML MACHINE` command, with its matching dry run and confirmed update flow, for the front node too. Check fresh access, Slurm, `/home`, and front-node services after installation. A front-node restart is a separate procedure.
- Updates and all deploy paths, including the automatic timer and webhook, use a shared maintenance lock so they cannot overlap. A stopped operation releases the lock.
- If the local disk serving `/home` is missing at boot, the machine reaches root SSH for recovery and NFS does not export the empty mount point. A missing-disk Ubuntu 24.04 VM test checks this, and a restored disk recovers the original data and export.
