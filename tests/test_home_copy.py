"""The nightly copy of each user's listed files into the own /home of front and login nodes (home_mount: own): the
home machine's nanohpc-home-copy sends them, and the node's nanohpc-home-copy-receive (root's forced command for that
key) lets rsync write only into one user's home folder, as that user. Run through the real scripts with a fake rsync
and a fake setpriv that record their arguments."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEND = ROOT / "src/nanohpc/files/nanohpc-home-copy"
RECEIVE = ROOT / "src/nanohpc/files/nanohpc-home-copy-receive"
BACKUP = ROOT / "src/nanohpc/files/cluster-backup"
# As an rsync 3 client sends it for --recursive --times --perms --delete --timeout=600 --max-size=10M.
LINE = "rsync --server -tpre.iLsfxCIvu --timeout=600 --delete-during --max-size=10485760 . /home/{user}/"


def fake_programs(folder: Path, exit_code: int) -> dict[str, str]:
    """Fake rsync and setpriv first on PATH; each appends its name and arguments, one call per line, to
    folder/calls. setpriv then runs the rest of its command line after `--`."""
    bin_folder = folder / "bin"
    bin_folder.mkdir(exist_ok=True)
    calls = folder / "calls"
    (bin_folder / "rsync").write_text(f"#!/bin/sh\necho \"rsync $*\" >> '{calls}'\nexit {exit_code}\n")
    (bin_folder / "setpriv").write_text(
        f'#!/bin/sh\necho "setpriv $*" >> \'{calls}\'\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n'
    )
    for name in ("rsync", "setpriv"):
        (bin_folder / name).chmod(0o755)
    return {**os.environ, "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}"}


def calls(folder: Path) -> list[str]:
    path = folder / "calls"
    return path.read_text().splitlines() if path.exists() else []


class SendTest(unittest.TestCase):
    def send(self, folder: Path, exit_code: int) -> subprocess.CompletedProcess[str]:
        home = folder / "home"
        for user in ("alice", "bob"):
            (home / user).mkdir(parents=True)
        return subprocess.run(
            [sys.executable, str(SEND), "--source", str(home), "--to", "login1=10.0.0.51", "--to", "front=10.0.0.10",
             "--users", "alice", "bob", "--key", "k", "--known-hosts", "h"],
            env=fake_programs(folder, exit_code), capture_output=True, text=True, check=False,
        )  # fmt: skip

    def test_each_user_gets_the_backups_listed_files_without_deleting_others(self) -> None:
        """One rsync per node and user, into that user's home, with the backup's file list (one source: the list in
        cluster-backup), plain files only (no links, devices, or owners), no setuid or setgid bits, no file over
        10 MB, and --delete without --delete-excluded, so the node's other files stay."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.send(folder, 0)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = calls(folder)
            self.assertEqual(len(lines), 4)
            self.assertTrue(lines[0].endswith(f"{folder}/home/alice/ root@10.0.0.51:/home/alice/"), lines[0])
            self.assertTrue(lines[3].endswith(f"{folder}/home/bob/ root@10.0.0.10:/home/bob/"), lines[3])
            words = lines[0].split(" ")
            for option in ("--recursive", "--times", "--perms", "--chmod=ug-s", "--delete", "--max-size=10M"):
                self.assertIn(option, words)
            for option in ("--archive", "--links", "--devices", "--owner", "--group", "--delete-excluded"):
                self.assertNotIn(option, words)
            self.assertIn("StrictHostKeyChecking=yes", lines[0])
            loader = importlib.machinery.SourceFileLoader("cluster_backup", str(BACKUP))
            spec = importlib.util.spec_from_loader("cluster_backup", loader)
            assert spec is not None
            backup = importlib.util.module_from_spec(spec)
            loader.exec_module(backup)
            rules = [word for word in words if word.startswith("--include=")]
            self.assertEqual(rules, backup.home_rules("/"))
            self.assertIn("--include=/.ssh/***", rules)
            self.assertIn("--include=/.config/", rules)
            self.assertEqual(words[-3], "--exclude=*")

    def test_a_failed_copy_fails_the_run_after_trying_every_node_and_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.send(folder, 12)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(len(calls(folder)), 4)
            self.assertIn("login1:alice, login1:bob, front:alice, front:bob", result.stderr)

    def test_files_that_vanished_while_copying_are_fine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.send(Path(directory), 24)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_home_folder_is_a_failure_not_an_empty_copy(self) -> None:
        """A user without a home folder here is not copied (nothing deleted there) and fails the run."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "home/alice").mkdir(parents=True)
            result = subprocess.run(
                [sys.executable, str(SEND), "--source", str(folder / "home"), "--to", "login1=10.0.0.51",
                 "--users", "alice", "carol", "--key", "k", "--known-hosts", "h"],
                env=fake_programs(folder, 0), capture_output=True, text=True, check=False,
            )  # fmt: skip
            self.assertEqual(result.returncode, 1)
            self.assertEqual(len(calls(folder)), 1)
            self.assertIn("login1:carol", result.stderr)


class ReceiveTest(unittest.TestCase):
    def receive(self, folder: Path, command: str) -> subprocess.CompletedProcess[str]:
        home = folder / "home"
        (home / "alice").mkdir(parents=True, exist_ok=True)
        environment = {
            **fake_programs(folder, 0),
            "SSH_ORIGINAL_COMMAND": command.replace("/home/", f"{home}/"),
            "NANOHPC_HOME_COPY_ROOT": str(home),
        }
        return subprocess.run([sys.executable, str(RECEIVE)], env=environment, capture_output=True, text=True,
                              check=False)  # fmt: skip

    def test_rsync_runs_as_the_owner_of_that_home_folder(self) -> None:
        """root drops to the folder's owner before rsync, so a copy can only write what that user could."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.receive(folder, LINE.format(user="alice"))
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = calls(folder)
            self.assertEqual(len(lines), 2)
            owner = (folder / "home/alice").stat()
            self.assertEqual(
                lines[0].split(" -- ")[0],
                f"setpriv --reuid={owner.st_uid} --regid={owner.st_gid} --init-groups --no-new-privs",
            )
            self.assertEqual(lines[1], "rsync " + LINE.format(user="alice").replace("/home/", f"{folder}/home/")[6:])

    def test_anything_else_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "home").mkdir()
            (folder / "home/escape").symlink_to("/etc")
            for command in (
                "",
                "id",
                LINE.format(user="alice") + "; id",
                LINE.format(user="..").replace("/home/../", "/home/../etc/"),
                LINE.format(user="alice").replace("/home/alice/", "/home/"),
                LINE.format(user="alice").replace("/home/alice/", "/root/"),
                LINE.format(user="alice").replace("/home/alice/", "/home/alice/.ssh/"),
                LINE.format(user="escape"),
                LINE.format(user="nobody-here"),
                LINE.format(user="alice").replace("--server", "--server --sender"),
                LINE.format(user="alice").replace("--timeout=600", "--temp-dir=/tmp"),
                LINE.format(user="alice").replace("--timeout=600", "--delete-excluded"),
                # Links, device files, owners, and groups are never received.
                LINE.format(user="alice").replace("-tpre", "-ltpre"),
                LINE.format(user="alice").replace("-tpre", "-Dtpre"),
                LINE.format(user="alice").replace("-tpre", "-ogtpre"),
                "rsync --server . /home/alice/",
            ):
                with self.subTest(command):
                    result = self.receive(folder, command)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("refused", result.stderr)
            self.assertEqual(calls(folder), [])


ANSIBLE = ROOT / "src/nanohpc/ansible"


def task(path: Path, name: str) -> dict[str, Any]:
    return next(item for item in yaml.safe_load(path.read_text()) if item["name"] == name)


def rendered(text: str, values: dict[str, Any]) -> str:
    """Render a task's content as Ansible does (trim_blocks)."""
    return jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined).from_string(text).render(**values)


class WiringTest(unittest.TestCase):
    """The playbooks run these programs on the right machines, with the right arguments (rendered from the tasks)."""

    def values(self) -> dict[str, Any]:
        machines = {"login1": {"address": "10.0.0.51"}, "login2": {"address": "10.0.0.52"}}
        nanohpc = {"home": {"own": ["login2"]}, "machines": machines, "users": [{"name": "alice"}, {"name": "bob"}]}
        return {"nanohpc": nanohpc, "groups": {"role_login": ["login1", "login2"]}}

    def test_the_home_machine_copies_to_every_own_home_at_night(self) -> None:
        content = task(ANSIBLE / "roles/home_own/tasks/send.yml", "Install the copy service and its nightly timer")
        service = rendered(content["ansible.builtin.copy"]["content"], {**self.values(), "item": "service"})
        self.assertIn(
            "ExecStart=/usr/local/bin/nanohpc-home-copy --source /home --to login2=10.0.0.52 --users alice bob "
            "--key /etc/nanohpc/home-copy/id_ed25519 --known-hosts /etc/nanohpc/home-copy/known_hosts",
            service,
        )
        self.assertIn("RequiresMountsFor=/home", service)
        timer = rendered(content["ansible.builtin.copy"]["content"], {**self.values(), "item": "timer"})
        self.assertIn("OnCalendar=*-*-* 02:30:00", timer)

    def test_the_front_node_sends_only_the_snapshot_to_an_own_home_login_node(self) -> None:
        content = task(ANSIBLE / "roles/login_node/tasks/send.yml", "Install the copy service and timer")
        service = rendered(content["ansible.builtin.copy"]["content"], {**self.values(), "item": "service"})
        self.assertIn("--login login1=10.0.0.51 --login login2=10.0.0.52 --own-home login2 --key", service)

    def test_plays_reach_the_machines_with_their_own_home(self) -> None:
        site = {play["name"]: play for play in yaml.safe_load((ANSIBLE / "site.yml").read_text())}
        own = [play for play in site.values() if play["hosts"] == "home_own"]
        self.assertEqual([play.get("roles") for play in own], [["home_own"]])
        # The cleanup never runs on the home machine, whose quota units it would remove.
        cleanup = [play for play in site.values() if "home_own" in json.dumps(play.get("tasks", [])) and
                   "off" in json.dumps(play.get("tasks", []))]  # fmt: skip
        self.assertEqual([play["hosts"] for play in cleanup], ["role_front:role_login:!home_own:!role_home"])
        self.assertIn("home_own", next(p for p in site.values() if p.get("roles") == ["monitoring"])["hosts"])
        partial = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        users = [play for play in partial if "home_server" in json.dumps(play.get("tasks", []))
                 and "users" in play.get("tags", [])]  # fmt: skip
        self.assertEqual([play["hosts"] for play in users], ["role_home:home_own"])


if __name__ == "__main__":
    unittest.main()
