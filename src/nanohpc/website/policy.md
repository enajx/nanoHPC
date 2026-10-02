# {{cluster_name}} cluster policy

> Static explanation of queue policy. The partition summary changes with Slurm, so read it on the website.

[Open the live policy page](./#policy)

## Queue ranking

GPU-hours are allocated GPU count multiplied by runtime. CPU and RAM do not contribute to fair-share usage. Recent usage decays over time; users with less recent usage receive a higher fair-share factor. Waiting time also contributes to priority. Running jobs are not interrupted by ranking, and priority does not guarantee a start time.

```bash
sshare -al
sprio
```

## Live partition summary

The partition names, default partition, time limits, and machines are live values. Open the website policy page for the current partition summary.
