"""`nanohpc fix-uid`: renumber one user on one machine to the UID cluster.yml gives them, and their own primary
group to the same number.

The plan (`plan_fix`) only reads: it checks over SSH that the change is safe and lists what it would do. The
change (`apply_fix`) runs only a plan that passed, as root with sudo (the SSH agent is forwarded, so an
administrator's key can unlock it): first a root-only record of the old IDs, then groupmod, usermod (which also
changes the home folder when it is local), then chown/chgrp of the remaining files on the machine's local
filesystems. The record is removed when everything worked; if a run stops halfway, the next plan reads it and plans
the remaining steps. A home folder on a network mount is not changed from this machine: usermod runs where that
mount is not mounted.
"""

import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from nanohpc.config import load_config
from nanohpc.probe import PROBE_TIMEOUT, getent, remote, run_remote, ssh_failure, uid_owner, user_ids

# Filesystems searched for the user's files: local filesystems that keep a file owner. Everything else is left
# out: network filesystems (nfs, nfs4, cifs, autofs, fuse.*), memory and kernel filesystems (tmpfs, proc, sysfs),
# and filesystems without owners (vfat).
LOCAL_FILESYSTEMS = ("ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs")
NETWORK_FILESYSTEMS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "ceph", "glusterfs", "lustre", "9p", "autofs")
EXAMPLES = 5
FIND_TIMEOUT = 15 * 60  # seconds, for counting the user's files
APPLY_TIMEOUT = 30 * 60  # seconds, for each command of a plan
# The root-only record of a change in progress, one file per user (<user>.json).
RECORD_FOLDER = "/var/lib/nanohpc/fix-uid"
SUDO = "sudo -S -p ''"  # as deploy: an empty stdin, so a forwarded key unlocks sudo and a password prompt fails

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
    old_uid: int | None  # the UID the files belong to (from the record of a run that stopped halfway); None: no account
    old_gid: int | None
    group: str | None  # name of the user's primary group
    ok: bool
    reasons: list[str]  # why it cannot proceed (empty when ok)
    notes: list[str]  # what the administrator should know (a network home, finishing a run that stopped halfway)
    filesystems: list[str]  # local filesystems searched and changed
    file_count: int  # paths on them owned by the old UID or GID (the home folder included when local)
    examples: list[str]  # a few of those paths
    home_network: bool  # /home or the user's home folder is on a network mount on this machine
    commands: list[list[str]]  # run as root, in this order


def record_path(user: str) -> str:
    """Return where the record of a change in progress for `user` is kept on the machine."""
    return f"{RECORD_FOLDER}/{user}.json"


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


def is_network(fstype: str) -> bool:
    """Whether a filesystem type is a network (or automounted network) filesystem."""
    return fstype in NETWORK_FILESYSTEMS or fstype.startswith("fuse.")


def containing(mounts: list[tuple[str, str, str]], path: str) -> list[tuple[str, str, str]]:
    """Return the mounts that hold `path` (their mount point is the path or a folder above it), shortest first."""
    above = [mount for mount in mounts if mount[0] == path or path.startswith(mount[0].rstrip("/") + "/")]
    return sorted(above, key=lambda mount: len(mount[0]))


def plan_fix(target: str, ssh_config: Path | None, user: str, uid: int) -> FixPlan:
    """Check over SSH, without changing anything, whether `user` can be renumbered to `uid` (and their own primary
    group to the same GID) on a machine, and return the plan. When an earlier run stopped halfway, its record gives
    the old IDs and the plan has only the remaining steps."""
    reasons: list[str] = []
    notes: list[str] = []

    def stop(old_uid: int | None, old_gid: int | None, group: str | None) -> FixPlan:
        return FixPlan(target, user, uid, old_uid, old_gid, group, False, reasons, notes, [], 0, [], False, [])

    record_file = shlex.quote(record_path(user))
    read_record = shlex.quote(f"if [ -e {record_file} ]; then cat {record_file}; fi")
    read = run_remote(target, ssh_config, f"{SUDO} sh -c {read_record} </dev/null", True, PROBE_TIMEOUT)
    failure = ssh_failure(target, read)
    if failure is not None:
        reasons.append(failure)
        return stop(None, None, None)
    sudo_ok = read.returncode == 0
    if not sudo_ok:
        reasons.append(
            f"sudo on {target} asks for a password or is not allowed ({read.stderr.strip() or 'no output'}): "
            "fix-uid needs sudo without a prompt (an administrator's SSH key loaded with ssh-add, or passwordless sudo)"
        )
    record = json.loads(read.stdout) if sudo_ok and read.stdout.strip() else None
    accounts = getent(target, ssh_config, "passwd", [user], False)
    if not accounts:
        reasons.append(f"{user} has no account on {target}")
        return stop(None, None, None)
    if not getent(target, ssh_config, "passwd", [user], True):
        reasons.append(
            f"{user} is not in the local /etc/passwd on {target} (an account from LDAP or another directory): "
            "fix-uid only changes local accounts"
        )
        return stop(None, None, None)
    current_uid, current_gid, folder = int(accounts[0][2]), int(accounts[0][3]), accounts[0][5]
    if record is not None:
        old_uid, old_gid, group = int(record["old_uid"]), int(record["old_gid"]), str(record["group"])
        if record["new_id"] != uid:
            reasons.append(
                f"an earlier fix-uid of {user} on {target} to UID {record['new_id']} stopped halfway (its record is "
                f"{record_path(user)}): finish it with that UID first"
            )
            return stop(old_uid, old_gid, group)
        notes.append(
            f"an earlier fix-uid of {user} on {target} stopped halfway: this plan finishes it, from the old UID "
            f"{old_uid} and GID {old_gid} kept in {record_path(user)}"
        )
    else:
        old_uid, old_gid = current_uid, current_gid
        groups = getent(target, ssh_config, "group", [str(old_gid)], False)
        group = groups[0][0] if groups else None
        if old_uid == uid and old_gid == uid:
            reasons.append(f"{user} already has UID {uid} and GID {uid} on {target}: nothing to change")
            return stop(old_uid, old_gid, group)
    group_gid = old_gid
    if group != user:
        reasons.append(
            f"the primary group of {user} on {target} is {group or 'missing'} (GID {old_gid}), not a group of its own "
            f"named {user}: change it by hand"
        )
    else:
        local_group = getent(target, ssh_config, "group", [group], True)
        if not local_group:
            reasons.append(
                f"the group {group} is not in the local /etc/group on {target}: fix-uid only changes local groups"
            )
        else:
            group_gid = int(local_group[0][2])
    if current_uid != uid:
        owner = uid_owner(target, ssh_config, uid)
        if owner is not None and owner != user:
            reasons.append(f"UID {uid} is taken by the account {owner} on {target}")
    if group_gid != uid:
        taken = getent(target, ssh_config, "group", [str(uid)], False)
        if taken and taken[0][0] != group:
            reasons.append(f"GID {uid} is taken by the group {taken[0][0]} on {target}")
    processes = remote(target, ssh_config, f"pgrep -l -u {current_uid}", False, PROBE_TIMEOUT)
    if processes.returncode not in (0, 1):
        raise RuntimeError(f"pgrep on {target} failed: {processes.stderr.strip() or 'no output'}")
    running = processes.stdout.strip().splitlines()
    if running:
        more = f", and {len(running) - EXAMPLES} more" if len(running) > EXAMPLES else ""
        reasons.append(
            f"{user} has running processes on {target} ({', '.join(running[:EXAMPLES])}{more}): stop them first"
        )
    who = remote(target, ssh_config, "who", False, PROBE_TIMEOUT)
    if who.returncode != 0:
        raise RuntimeError(f"who on {target} failed: {who.stderr.strip() or 'no output'}")
    if user in [line.split()[0] for line in who.stdout.splitlines() if line.strip()]:
        reasons.append(f"{user} is logged in on {target}: log out first")
    found = remote(target, ssh_config, "findmnt -rn -o TARGET,FSTYPE,SOURCE", False, PROBE_TIMEOUT)
    if found.returncode != 0:
        raise RuntimeError(f"findmnt on {target} failed: {found.stderr.strip() or 'no output'}")
    mounts = parse_mounts(found.stdout)
    filesystems = local_filesystems(mounts)
    # /home, and the user's home folder (which may be elsewhere): the filesystem each is on.
    network: list[tuple[str, str, str]] = []
    for path in ("/home", folder):
        above = containing(mounts, path)
        if above and is_network(above[-1][1]) and above[-1] not in network:
            network.append(above[-1])
    for mount_point, fstype, source in network:
        notes.append(
            f"{mount_point} on {target} is a network mount ({fstype} from {source}): fix-uid does not change the "
            f"files there. They are on the machine that serves it, where they must belong to UID {uid}; they do when "
            f"that machine already uses the cluster.yml UID {uid} for {user}"
        )
    # Where usermod must not reach: the outermost network mount that holds the home folder.
    folder_mounts = containing(mounts, folder)
    unmount = None
    if folder_mounts and is_network(folder_mounts[-1][1]):
        unmount = next(mount[0] for mount in folder_mounts if is_network(mount[1]))
    count = 0
    examples: list[str] = []
    if sudo_ok and filesystems:
        program = shlex.join(["python3", "-c", FIND_PROGRAM, str(old_uid), str(old_gid), *filesystems])
        result = run_remote(target, ssh_config, f"{SUDO} {program} </dev/null", True, FIND_TIMEOUT)
        if result.returncode != 0:
            failure = ssh_failure(target, result) or result.stderr.strip() or "no output"
            reasons.append(f"searching the files on {target} failed: {failure}")
        else:
            files = json.loads(result.stdout)
            count, examples = files["count"], files["examples"]
            reasons += [f"{error} (on {target})" for error in files["errors"]]
    commands: list[list[str]] = []
    if record is None:
        content = json.dumps({"user": user, "old_uid": old_uid, "old_gid": old_gid, "group": group, "new_id": uid})
        write = (
            f"mkdir -p {RECORD_FOLDER} && chmod 0700 {RECORD_FOLDER} && umask 077 && "
            f"printf '%s\\n' {shlex.quote(content)} > {record_file}"
        )
        commands.append(["sh", "-c", write])
    if group_gid != uid:
        commands.append(["groupmod", "-g", str(uid), str(group)])
    if current_uid != uid:
        usermod = ["usermod", "-u", str(uid), user]
        if unmount is not None:
            # In a private mount namespace without that mount, so usermod cannot change the shared files.
            inner = f"umount --lazy {shlex.quote(unmount)} && {shlex.join(usermod)}"
            usermod = ["unshare", "--mount", "--propagation", "private", "sh", "-c", inner]
        commands.append(usermod)
    if old_uid != uid:
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
        bool(network),
        commands,
    )


def format_plan(plan: FixPlan) -> str:
    """Return the plan as text for the administrator."""
    lines = [f"fix-uid {plan.user} on {plan.target} (read-only check)"]
    if plan.old_uid is not None:
        lines.append(
            f"  old: UID {plan.old_uid}, primary group {plan.group} (GID {plan.old_gid}); cluster.yml: UID {plan.new_id}"
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


def applied_message(plan: FixPlan) -> str:
    """Return the last message of an apply_fix that worked."""
    return f"{plan.user} on {plan.target} now has UID {plan.new_id} and GID {plan.new_id}"


def apply_fix(target: str, ssh_config: Path | None, plan: FixPlan) -> list[str]:
    """Run a plan that passed (`plan.ok`) as root with sudo, then read the account again to confirm it and remove
    the record. Return the messages for the administrator: the commands run, then the first failure and the
    commands not run, or, when everything worked, applied_message(plan) as the last line. Prints nothing. Stops at
    the first command that fails, keeping the record so the next plan finishes the change."""
    if plan.target != target:
        raise ValueError(f"the plan is for {plan.target}, not {target}")
    if not plan.ok:
        return [f"Nothing was changed: the plan for {plan.user} on {target} cannot proceed."]
    messages: list[str] = []
    for index, command in enumerate(plan.commands):
        result = run_remote(target, ssh_config, f"{SUDO} {shlex.join(command)} </dev/null", True, APPLY_TIMEOUT)
        messages.append(f"ran as root on {target}: {shlex.join(command)}")
        if result.returncode != 0:
            failure = ssh_failure(target, result) or result.stderr.strip() or "no output"
            messages.append(f"failed (exit code {result.returncode}): {failure}")
            messages.append(
                "The commands before it were applied. Not run yet (run nanohpc fix-uid again to plan them):"
            )
            messages += [f"  {shlex.join(rest)}" for rest in plan.commands[index + 1 :]]
            return messages
    ids = user_ids(target, ssh_config, [plan.user])[plan.user]
    if ids != (plan.new_id, plan.new_id):
        messages.append(f"{plan.user} on {target} has (UID, GID) {ids}, not {plan.new_id}: check it by hand")
        return messages
    remove = shlex.join(["rm", "-f", record_path(plan.user)])
    removed = run_remote(target, ssh_config, f"{SUDO} {remove} </dev/null", True, PROBE_TIMEOUT)
    if removed.returncode != 0:
        failure = ssh_failure(target, removed) or removed.stderr.strip() or "no output"
        messages.append(f"could not remove {record_path(plan.user)} on {target} ({failure}): remove it by hand")
        return messages
    messages.append(applied_message(plan))
    return messages


def run(path: Path, user: str, machine: str, ssh_config: Path | None, apply: bool) -> int:
    """`nanohpc fix-uid`: print the plan for renumbering `user` on `machine` to their UID in cluster.yml (a dry
    run that changes nothing), and with `apply`, apply it and print what happened. Return the exit code."""
    if not path.is_file():
        print(f"{path}: file not found", file=sys.stderr)
        return 1
    # Only the users and machines are needed: no certificate, logo, or .env check.
    config, errors = load_config(path, False, False)
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
    messages = apply_fix(machine, ssh_config, plan)
    worked = messages[-1] == applied_message(plan)
    for message in messages:
        print(message, file=sys.stdout if worked else sys.stderr)
    return 0 if worked else 1
