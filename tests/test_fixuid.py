"""Tests for `nanohpc fix-uid` (nanohpc.fixuid): the read-only plan, applying it, and the command.

These tests use fakes (see test_probe.py): a fake `ssh` runs the remote commands locally against a fake machine
folder, where fake getent, pgrep, who, findmnt, and find read fake files, and a fake sudo logs each command it runs
as root; fake usermod and groupmod change the fake passwd and group files. The fake ssh puts /var/lib/nanohpc (where
fix-uid keeps its record of a change in progress) in the fake machine folder. The real check on a machine is the
simulated cluster test (tests/test_sim.py).
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nanohpc.fixuid import applied_message, apply_fix, plan_fix
from tests.test_probe import FAKE_IDMOD, UBUNTU, fake_machine, write_command  # pyrefly: ignore[missing-import]

ROOT = Path(__file__).resolve().parents[1]

CHOWN_ROOT = ["find", "/", "-xdev", "-uid", "1001", "-exec", "chown", "-h", "2000", "{}", "+"]
CHOWN_SCRATCH = ["find", "/scratch", "-xdev", "-uid", "1001", "-exec", "chown", "-h", "2000", "{}", "+"]
CHGRP_ROOT = ["find", "/", "-xdev", "-gid", "1001", "-exec", "chgrp", "-h", "2000", "{}", "+"]
CHGRP_SCRATCH = ["find", "/scratch", "-xdev", "-gid", "1001", "-exec", "chgrp", "-h", "2000", "{}", "+"]


class FixUidTest(unittest.TestCase):
    """alice has UID and GID 1001 on the fake machine; cluster.yml says 2000."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.environment = fake_machine(self.folder, [], UBUNTU)
        self.root = self.folder / "root"
        self.record = self.root / "var/lib/nanohpc/fix-uid/alice.json"
        with (self.root / "mounts").open("a") as mounts:
            mounts.write("/scratch xfs /dev/sda1\n/mnt/lab nfs4 nas:/lab\n")
        with (self.root / "owned").open("a") as owned:
            owned.write("/scratch\t/scratch/alice\n")
        self.patch = mock.patch.dict(os.environ, self.environment)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def root_commands(self) -> list[str]:
        """Return the commands the fake sudo ran, without the plan's read-only ones (reading the record, find)."""
        path = self.root / "sudo.log"
        lines = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [line for line in lines if not line.startswith(("python3 ", "sh -c 'if [ -e "))]

    def expected(self, commands: list[list[str]]) -> list[str]:
        """Return planned commands as the fake sudo logs them (with /var/lib/nanohpc in the fake machine folder)."""
        fake = str(self.root / "var/lib/nanohpc")
        return [shlex.join(command).replace("/var/lib/nanohpc", fake) for command in commands]

    def test_clean_plan(self) -> None:
        plan = plan_fix("node7", None, "alice", 2000)
        self.assertEqual(plan.reasons, [])
        self.assertTrue(plan.ok)
        self.assertEqual((plan.old_uid, plan.old_gid, plan.group), (1001, 1001, "alice"))
        self.assertEqual(plan.filesystems, ["/", "/scratch"])
        self.assertEqual(plan.file_count, 4)
        self.assertEqual(plan.examples[:2], ["/home/alice", "/home/alice/notes.txt"])
        self.assertFalse(plan.home_network)
        self.assertEqual(plan.notes, [])
        # First, a record of the old IDs, so a run that stops halfway can be finished.
        self.assertEqual(plan.commands[0][:2], ["sh", "-c"])
        self.assertIn("/var/lib/nanohpc/fix-uid/alice.json", plan.commands[0][2])
        self.assertEqual(
            plan.commands[1:],
            [
                ["groupmod", "-g", "2000", "alice"],
                ["usermod", "-u", "2000", "alice"],
                CHOWN_ROOT,
                CHOWN_SCRATCH,
                CHGRP_ROOT,
                CHGRP_SCRATCH,
            ],
        )
        # The plan changed nothing.
        self.assertEqual(self.root_commands(), [])
        self.assertFalse(self.record.parent.exists())
        self.assertIn("alice:x:1001:1001:", (self.root / "etc/passwd").read_text())

    def test_uid_and_gid_taken_by_another_account(self) -> None:
        plan = plan_fix("node7", None, "alice", 2002)
        self.assertFalse(plan.ok)
        self.assertEqual(
            plan.reasons,
            ["UID 2002 is taken by the account carol on node7", "GID 2002 is taken by the group carol on node7"],
        )

    def test_running_processes_and_login(self) -> None:
        with (self.root / "processes").open("a") as processes:
            processes.write("1001 4242 python\n")
        with (self.root / "who").open("a") as who:
            who.write("alice    pts/1        2026-10-03 10:00 (192.168.1.3)\n")
        plan = plan_fix("node7", None, "alice", 2000)
        self.assertFalse(plan.ok)
        self.assertEqual(
            plan.reasons,
            [
                "alice has running processes on node7 (4242 python): stop them first",
                "alice is logged in on node7: log out first",
            ],
        )

    def test_missing_account_and_nothing_to_do(self) -> None:
        missing = plan_fix("node7", None, "bob", 2001)
        self.assertFalse(missing.ok)
        self.assertEqual(missing.reasons, ["bob has no account on node7"])
        done = plan_fix("node7", None, "carol", 2002)
        self.assertFalse(done.ok)
        self.assertEqual(done.reasons, ["carol already has UID 2002 and GID 2002 on node7: nothing to change"])

    def test_directory_account_is_refused(self) -> None:
        (self.root / "etc/passwd.ldap").write_text("bob:x:5000:5000::/home/bob:/bin/bash\n")
        (self.root / "etc/group.ldap").write_text("bob:x:5000:\n")
        plan = plan_fix("node7", None, "bob", 2001)
        self.assertFalse(plan.ok)
        self.assertEqual(
            plan.reasons,
            [
                (
                    "bob is not in the local /etc/passwd on node7 (an account from LDAP or another directory): "
                    "fix-uid only changes local accounts"
                )
            ],
        )

    def test_sudo_needs_password(self) -> None:
        (self.root / "sudo_password").write_text("")
        plan = plan_fix("node7", None, "alice", 2000)
        self.assertFalse(plan.ok)
        self.assertIn("sudo on node7 asks for a password", plan.reasons[0])

    def test_unreachable_machine(self) -> None:
        plan = plan_fix("unreachable", None, "alice", 2000)
        self.assertFalse(plan.ok)
        self.assertEqual(len(plan.reasons), 1)
        self.assertIn("Connection refused", plan.reasons[0])

    def test_search_timeout_is_a_reason(self) -> None:
        write_command(self.folder / "bin", "find", "exec sleep 5\n")
        with mock.patch("nanohpc.fixuid.FIND_TIMEOUT", 1):
            plan = plan_fix("node7", None, "alice", 2000)
        self.assertFalse(plan.ok)
        self.assertEqual(
            plan.reasons, ["searching the files on node7 failed: SSH failed: `ssh node7`: no answer within 1 seconds"]
        )

    def test_network_home(self) -> None:
        with (self.root / "mounts").open("a") as mounts:
            mounts.write("/home nfs4 front:/home\n")
        plan = plan_fix("node7", None, "alice", 2000)
        self.assertTrue(plan.ok, plan.reasons)
        self.assertTrue(plan.home_network)
        self.assertEqual(plan.filesystems, ["/", "/scratch"])
        # usermod runs where /home is not mounted, so it cannot change the files of the shared /home.
        usermod = ["unshare", "--mount", "--propagation", "private", "sh", "-c"]
        self.assertEqual(plan.commands[2], [*usermod, "umount --lazy /home && usermod -u 2000 alice"])
        self.assertEqual(len(plan.notes), 1)
        self.assertIn("/home on node7 is a network mount (nfs4 from front:/home)", plan.notes[0])
        self.assertIn("UID 2000", plan.notes[0])
        self.assertEqual(apply_fix("node7", None, plan)[-1], applied_message(plan))
        self.assertIn("umount --lazy /home\nusermod -u 2000 alice\n", (self.root / "log").read_text())

    def test_home_folder_on_autofs(self) -> None:
        passwd = (self.root / "etc/passwd").read_text().replace("::/home/alice:", "::/nfs/home/alice:")
        (self.root / "etc/passwd").write_text(passwd)
        with (self.root / "mounts").open("a") as mounts:
            mounts.write("/nfs autofs auto.nfs\n/nfs/home nfs4 server:/export/home\n")
        plan = plan_fix("node7", None, "alice", 2000)
        self.assertTrue(plan.ok, plan.reasons)
        self.assertTrue(plan.home_network)
        usermod = ["unshare", "--mount", "--propagation", "private", "sh", "-c"]
        self.assertEqual(plan.commands[2], [*usermod, "umount --lazy /nfs && usermod -u 2000 alice"])
        self.assertEqual(len(plan.notes), 1)
        self.assertIn("/nfs/home on node7 is a network mount (nfs4 from server:/export/home)", plan.notes[0])

    def test_apply_runs_the_planned_commands_in_order(self) -> None:
        plan = plan_fix("node7", None, "alice", 2000)
        messages = apply_fix("node7", None, plan)
        self.assertEqual(self.root_commands(), [*self.expected(plan.commands), f"rm -f {self.record}"])
        self.assertEqual(messages[-1], "alice on node7 now has UID 2000 and GID 2000")
        self.assertIn("ran as root on node7: groupmod -g 2000 alice", messages)
        self.assertIn("alice:x:2000:2000:", (self.root / "etc/passwd").read_text())
        self.assertIn("alice:x:2000:", (self.root / "etc/group").read_text())
        self.assertFalse(self.record.exists())

    def test_apply_refuses_a_plan_that_is_not_ok(self) -> None:
        plan = plan_fix("node7", None, "alice", 2002)
        messages = apply_fix("node7", None, plan)
        self.assertEqual(messages, ["Nothing was changed: the plan for alice on node7 cannot proceed."])
        self.assertEqual(self.root_commands(), [])

    def test_halfway_failure_then_rerun_finishes(self) -> None:
        plan = plan_fix("node7", None, "alice", 2000)
        write_command(self.folder / "bin", "usermod", "echo 'usermod: user alice is currently used' >&2\nexit 8\n")
        messages = apply_fix("node7", None, plan)
        self.assertNotEqual(messages[-1], applied_message(plan))
        self.assertEqual(self.root_commands(), self.expected(plan.commands[:3]))
        self.assertIn("failed (exit code 8): usermod: user alice is currently used", messages)
        self.assertIn(f"  {shlex.join(CHGRP_SCRATCH)}", messages)
        # The record keeps the old IDs; groupmod has already run.
        self.assertEqual(
            json.loads(self.record.read_text()),
            {"user": "alice", "old_uid": 1001, "old_gid": 1001, "group": "alice", "new_id": 2000},
        )
        self.assertIn("alice:x:1001:2000:", (self.root / "etc/passwd").read_text())
        write_command(self.folder / "bin", "usermod", FAKE_IDMOD)
        (self.root / "sudo.log").unlink()
        rerun = plan_fix("node7", None, "alice", 2000)
        self.assertTrue(rerun.ok, rerun.reasons)
        self.assertEqual((rerun.old_uid, rerun.old_gid, rerun.group), (1001, 1001, "alice"))
        self.assertEqual(
            rerun.commands,
            [["usermod", "-u", "2000", "alice"], CHOWN_ROOT, CHOWN_SCRATCH, CHGRP_ROOT, CHGRP_SCRATCH],
        )
        self.assertIn("stopped halfway", rerun.notes[0])
        messages = apply_fix("node7", None, rerun)
        self.assertEqual(messages[-1], applied_message(rerun), messages)
        self.assertEqual(self.root_commands(), [*self.expected(rerun.commands), f"rm -f {self.record}"])
        self.assertIn("alice:x:2000:2000:", (self.root / "etc/passwd").read_text())
        self.assertFalse(self.record.exists())

    def test_command_dry_run_then_apply(self) -> None:
        cluster = self.folder / "cluster.yml"
        shutil.copy(ROOT / "examples" / "cluster.yml", cluster)
        command = [
            sys.executable,
            "-c",
            "from nanohpc.cli import main; main()",
            "fix-uid",
            str(cluster),
            "alice",
            "gpu2",
        ]
        dry = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("groupmod -g 2000 alice", dry.stdout)
        self.assertIn("nothing was changed; run again with --apply to apply this plan", dry.stdout)
        self.assertEqual(self.root_commands(), [])
        applied = subprocess.run([*command, "--apply"], capture_output=True, text=True, check=False)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("ran as root on gpu2: groupmod -g 2000 alice", applied.stdout)
        self.assertIn("alice on gpu2 now has UID 2000 and GID 2000", applied.stdout)
        self.assertEqual(self.root_commands()[1:3], ["groupmod -g 2000 alice", "usermod -u 2000 alice"])

    def test_command_unknown_user_or_machine(self) -> None:
        cluster = self.folder / "cluster.yml"
        shutil.copy(ROOT / "examples" / "cluster.yml", cluster)
        base = [sys.executable, "-c", "from nanohpc.cli import main; main()", "fix-uid", str(cluster)]
        for arguments, message in (
            (["mallory", "gpu2"], "mallory is not a user in"),
            (["alice", "gpu9"], "gpu9 is not a machine in"),
        ):
            result = subprocess.run([*base, *arguments], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
