"""`nanohpc fix-uid`: renumber one user on one machine to the UID cluster.yml gives them, and their own primary
group to the same number.

The plan (`plan_fix`) only reads: it checks over SSH that the change is safe and lists what it would do. The
change (`apply_fix`) runs only a plan that passed, as root with sudo: groupmod, usermod (which also changes the
home folder when it is local), then chown/chgrp of the remaining files on the machine's local filesystems.
A /home that is a network mount is not changed from this machine: usermod runs where /home is not mounted.
"""

import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from nanohpc.config import load_config
from nanohpc.probe import getent, remote, run_remote, uid_owner, user_ids

# Filesystems searched for the user's files: local filesystems that keep a file owner. Everything else is left
# out: network filesystems (nfs, nfs4, cifs, fuse.*), memory and kernel filesystems (tmpfs, proc, sysfs), and
# filesystems without owners (vfat).
LOCAL_FILESYSTEMS = ("ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs")
NETWORK_FILESYSTEMS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "ceph", "glusterfs", "lustre", "9p")
EXAMPLES = 5

# Runs on the machine as root (read-only): counts the files owned by UID argv[1] or GID argv[2] on each local
# filesystem argv[3:], keeps a few example paths, and prints JSON. Paths are read NUL-separated, so any name works.
FIND_PROGRAM = r"""
import json, subprocess, sys, threading
uid, gid = sys.argv[1], sys.argv[2]
result = {"count": 0, "examples": [], "errors": []}
for mount in sys.argv[3:]:
    command = ["find", mount, "-xdev", "(", "-uid", uid, "-o", "-gid", gid, ")", "-print0"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    errors = []
    reader = threading.Thread(target=lambda: errors.append(process.stderr.read()))
    reader.start()
    rest = b""
    for chunk in iter(lambda: process.stdout.read(65536), b""):
        paths = (rest + chunk).split(b"\0")
        rest = paths.pop()
        result["count"] += len(paths)
        for path in paths[: max(0, 5 - len(result["examples"]))]:
            result["examples"].append(path.decode(errors="replace"))
    reader.join()
    if process.wait() != 0:
        text = errors[0].decode(errors="replace").strip().splitlines()
        result["errors"].append(f"find {mount}: " + (text[0] if text else f"exit code {process.returncode}"))
print(json.dumps(result))
"""


@dataclass(frozen=True)
class FixPlan:
    """What `nanohpc fix-uid` found on a machine and would do there. It may run only when `ok`."""

    target: str  # the machine (as reached with ssh)
    user: str
    new_id: int  # the UID from cluster.yml, also the new GID of the user's own group
    old_uid: int | None  # None when the account is missing
    old_gid: int | None
    group: str | None  # name of the user's primary group
    ok: bool
    reasons: list[str]  # why it cannot proceed (empty when ok)
    notes: list[str]  # what the administrator should know (a network /home)
    filesystems: list[str]  # local filesystems searched and changed
    file_count: int  # paths on them owned by the old UID or GID (the home folder included when local)
    examples: list[str]  # a few of those paths
    home_network: bool  # /home is a network mount on this machine
    commands: list[list[str]]  # run as root, in this order


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    """Return (target, fstype, source) from `findmnt -rn -o TARGET,FSTYPE,SOURCE` (which writes a space as \\x20)."""

    def unescape(value: str) -> str:
        return re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), value)

    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts.append((unescape(fields[0]), fields[1], unescape(fields[2])))
    return mounts


def local_filesystems(mounts: list[tuple[str, str, str]]) -> list[str]:
    """Return the mount points of local filesystems with file owners, without bind mounts of a folder whose
    filesystem is mounted too (findmnt shows them as device[/folder]); find -xdev covers those already."""
    devices = {source for _, _, source in mounts}
    return [
        target
        for target, fstype, source in mounts
        if fstype in LOCAL_FILESYSTEMS and not (source.endswith("]") and source.split("[")[0] in devices)
    ]


def home_mount(mounts: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """Return the mount that holds /home (the longest mount point above it)."""
    above = [mount for mount in mounts if mount[0] == "/home" or "/home".startswith(mount[0].rstrip("/") + "/")]
    return max(above, key=lambda mount: len(mount[0])) if above else None


def plan_fix(target: str, ssh_config: Path | None, user: str, uid: int) -> FixPlan:
    """Check over SSH, without changing anything, whether `user` can be renumbered to `uid` (and their own primary
    group to the same GID) on a machine, and return the plan."""
    reasons: list[str] = []
    notes: list[str] = []

    def stop(old_uid: int | None, old_gid: int | None, group: str | None) -> FixPlan:
        return FixPlan(target, user, uid, old_uid, old_gid, group, False, reasons, notes, [], 0, [], False, [])

    sudo = run_remote(target, ssh_config, "sudo -S -p '' true </dev/null")
    if sudo.returncode == 255:
        reasons.append(f"SSH failed: `ssh {target}`: {sudo.stderr.strip() or 'no output'}")
        return stop(None, None, None)
    sudo_ok = sudo.returncode == 0
    if not sudo_ok:
        reasons.append(
            f"sudo on {target} asks for a password or is not allowed ({sudo.stderr.strip() or 'no output'}): "
            "fix-uid needs sudo without a prompt (an administrator's SSH key loaded with ssh-add, or passwordless sudo)"
        )
    ids = user_ids(target, ssh_config, [user])[user]
    if ids is None:
        reasons.append(f"{user} has no account on {target}")
        return stop(None, None, None)
    old_uid, old_gid = ids
    groups = getent(target, ssh_config, "group", [str(old_gid)])
    group = groups[0][0] if groups else None
    if old_uid == uid and old_gid == uid:
        reasons.append(f"{user} already has UID {uid} and GID {uid} on {target}: nothing to change")
        return stop(old_uid, old_gid, group)
    if group != user:
        reasons.append(
            f"the primary group of {user} on {target} is {group or 'missing'} (GID {old_gid}), not a group of its own "
            f"named {user}: change it by hand"
        )
    if old_uid != uid:
        owner = uid_owner(target, ssh_config, uid)
        if owner is not None and owner != user:
            reasons.append(f"UID {uid} is taken by the account {owner} on {target}")
    if old_gid != uid:
        taken = getent(target, ssh_config, "group", [str(uid)])
        if taken and taken[0][0] != group:
            reasons.append(f"GID {uid} is taken by the group {taken[0][0]} on {target}")
    processes = remote(target, ssh_config, f"pgrep -l -u {old_uid}")
    if processes.returncode not in (0, 1):
        raise RuntimeError(f"pgrep on {target} failed: {processes.stderr.strip() or 'no output'}")
    running = processes.stdout.strip().splitlines()
    if running:
        listed = ", ".join(running[:EXAMPLES]) + (
            f", and {len(running) - EXAMPLES} more" if len(running) > EXAMPLES else ""
        )
        reasons.append(f"{user} has running processes on {target} ({listed}): stop them first")
    who = remote(target, ssh_config, "who")
    if who.returncode != 0:
        raise RuntimeError(f"who on {target} failed: {who.stderr.strip() or 'no output'}")
    if user in [line.split()[0] for line in who.stdout.splitlines() if line.strip()]:
        reasons.append(f"{user} is logged in on {target}: log out first")
    found = remote(target, ssh_config, "findmnt -rn -o TARGET,FSTYPE,SOURCE")
    if found.returncode != 0:
        raise RuntimeError(f"findmnt on {target} failed: {found.stderr.strip() or 'no output'}")
    mounts = parse_mounts(found.stdout)
    filesystems = local_filesystems(mounts)
    home = home_mount(mounts)
    home_network = home is not None and (home[1] in NETWORK_FILESYSTEMS or home[1].startswith("fuse."))
    if home is not None and home_network:
        server = home[2].split(":")[0]
        notes.append(
            f"/home on {target} is a network mount ({home[1]} from {home[2]}): fix-uid does not change the files "
            f"there. They are on {server}, where they must belong to UID {uid}; they do when {server} already "
            f"uses the cluster.yml UID {uid} for {user}"
        )
    count = 0
    examples: list[str] = []
    if sudo_ok and filesystems:
        program = shlex.join(["python3", "-c", FIND_PROGRAM, str(old_uid), str(old_gid), *filesystems])
        result = remote(target, ssh_config, f"sudo -S -p '' {program} </dev/null")
        if result.returncode != 0:
            raise RuntimeError(f"searching the files on {target} failed: {result.stderr.strip() or 'no output'}")
        files = json.loads(result.stdout)
        count, examples = files["count"], files["examples"]
        reasons += [f"{error} (on {target})" for error in files["errors"]]
    commands: list[list[str]] = []
    if old_gid != uid:
        commands.append(["groupmod", "-g", str(uid), str(group)])
    if old_uid != uid:
        usermod = ["usermod", "-u", str(uid), user]
        if home is not None and home_network:
            # In a private mount namespace without /home, so usermod cannot change the shared /home's files.
            inner = f"umount --lazy {shlex.quote(home[0])} && {shlex.join(usermod)}"
            usermod = ["unshare", "--mount", "--propagation", "private", "sh", "-c", inner]
        commands.append(usermod)
        commands += [
            ["find", path, "-xdev", "-uid", str(old_uid), "-exec", "chown", "-h", str(uid), "{}", "+"]
            for path in filesystems
        ]
    if old_gid != uid:
        commands += [
            ["find", path, "-xdev", "-gid", str(old_gid), "-exec", "chgrp", "-h", str(uid), "{}", "+"]
            for path in filesystems
        ]
    return FixPlan(
        target,
        user,
        uid,
        old_uid,
        old_gid,
        group,
        not reasons,
        reasons,
        notes,
        filesystems,
        count,
        examples,
        home_network,
        commands,
    )


def format_plan(plan: FixPlan) -> str:
    """Return the plan as text for the administrator."""
    lines = [f"fix-uid {plan.user} on {plan.target} (read-only check)"]
    if plan.old_uid is not None:
        lines.append(
            f"  now: UID {plan.old_uid}, primary group {plan.group} (GID {plan.old_gid}); cluster.yml: UID {plan.new_id}"
        )
    if plan.filesystems:
        lines.append(
            f"  files owned by UID {plan.old_uid} or GID {plan.old_gid} on local filesystems "
            f"({', '.join(plan.filesystems)}): {plan.file_count}"
        )
        lines += [f"    {path}" for path in plan.examples]
    lines += [f"  note: {note}" for note in plan.notes]
    if plan.ok:
        lines.append("  commands to run as root, in order:")
        lines += [f"    {shlex.join(command)}" for command in plan.commands]
    else:
        lines.append("  cannot proceed:")
        lines += [f"    - {reason}" for reason in plan.reasons]
    return "\n".join(lines)


def apply_fix(target: str, ssh_config: Path | None, plan: FixPlan) -> int:
    """Run a plan that passed (`plan.ok`) as root with sudo, then read the account again to confirm it. Return 0
    when the user now has the new UID and GID, 1 otherwise. Stops at the first command that fails."""
    if plan.target != target:
        raise ValueError(f"the plan is for {plan.target}, not {target}")
    if not plan.ok:
        print(f"Nothing was changed: the plan for {plan.user} on {target} cannot proceed.", file=sys.stderr)
        return 1
    for index, command in enumerate(plan.commands):
        print(f"running as root on {target}: {shlex.join(command)}")
        result = remote(target, ssh_config, f"sudo -S -p '' {shlex.join(command)} </dev/null")
        if result.returncode != 0:
            print(f"failed (exit code {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
            print("The commands before it were applied. Not run yet:", file=sys.stderr)
            for rest in plan.commands[index + 1 :]:
                print(f"  {shlex.join(rest)}", file=sys.stderr)
            return 1
    ids = user_ids(target, ssh_config, [plan.user])[plan.user]
    if ids != (plan.new_id, plan.new_id):
        print(f"{plan.user} on {target} has (UID, GID) {ids}, not {plan.new_id}: check it by hand", file=sys.stderr)
        return 1
    print(f"{plan.user} on {target} now has UID {plan.new_id} and GID {plan.new_id}")
    return 0


def run(path: Path, user: str, machine: str, ssh_config: Path | None, apply: bool) -> int:
    """`nanohpc fix-uid`: print the plan for renumbering `user` on `machine` to their UID in cluster.yml (a dry
    run that changes nothing), and with `apply`, apply it and confirm. Return the exit code."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    config, errors = load_config(path, True, False)
    if errors:
        print(f"{path}: {len(errors)} error(s); nothing was changed", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    uids = {entry["name"]: entry["uid"] for entry in config["users"]}
    if user not in uids:
        print(f"{user} is not a user in {path}; nothing was changed", file=sys.stderr)
        return 1
    if machine not in config["machines"]:
        print(f"{machine} is not a machine in {path}; nothing was changed", file=sys.stderr)
        return 1
    plan = plan_fix(machine, ssh_config, user, uids[user])
    print(format_plan(plan))
    if not plan.ok:
        print("Nothing was changed.", file=sys.stderr)
        return 1
    if not apply:
        print("nothing was changed; run again with --apply to apply this plan")
        return 0
    return apply_fix(machine, ssh_config, plan)
