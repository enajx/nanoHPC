"""The nightly copy of each user's listed files into the own /home of front and login nodes (home_mount: own): the
home machine's nanohpc-home-copy sends them, and the node's nanohpc-home-copy-receive (root's forced command for that
key) lets rsync write only into one user's home folder, as that user. Run through the real scripts with a fake rsync
and a fake setpriv that record their arguments."""

import getpass
import importlib.machinery
import importlib.util
import json
import os
import shlex
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


USER = getpass.getuser()


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


# A FAKE rsync client for the sending side: like rsync, it starts its --rsh program (without closing other open
# files) as `RSH -l root ADDRESS rsync --server ... . PATH`, sends a few bytes, and records what comes back.
FAKE_CLIENT = """#!/usr/bin/env python3
import os, shlex, subprocess, sys
calls = os.environ["FAKE_CALLS"]
with open(calls, "a") as out:
    out.write("rsync " + " ".join(sys.argv[1:]) + "\\n")
rsh = next(arg for arg in sys.argv if arg.startswith("--rsh=")).removeprefix("--rsh=")
address, path = sys.argv[-1].removeprefix("root@").split(":", 1)
path = os.environ.get("FAKE_REMOTE_PATH", path)
child = subprocess.Popen(
    [*shlex.split(rsh), "-l", "root", address, "rsync", "--server", "-tpre.iLsfxCIvu", "--timeout=600", ".", path],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=False,
)
answer, _ = child.communicate(b"payload from " + sys.argv[-2].encode() + b"\\n")
with open(calls, "a") as out:
    out.write("got " + answer.decode().strip() + "\\n")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")) if child.returncode == 0 else 12)
"""


def fake_sender_side(folder: Path, exit_code: int) -> dict[str, str]:
    """The home machine's side, FAKE: setpriv (records, then runs the rest), the rsync client above, and ssh, which
    records its arguments and answers with what it received (as the node's rsync would talk back)."""
    environment = fake_programs(folder, 0)
    bin_folder = folder / "bin"
    (bin_folder / "rsync").write_text(FAKE_CLIENT)
    (bin_folder / "ssh").write_text(f"#!/bin/sh\necho \"ssh $*\" >> '{folder / 'calls'}'\nsed 's/^/remote /'\n")
    (bin_folder / "ssh").chmod(0o755)
    return {**environment, "FAKE_CALLS": str(folder / "calls"), "FAKE_EXIT": str(exit_code)}


def calls(folder: Path) -> list[str]:
    path = folder / "calls"
    return path.read_text().splitlines() if path.exists() else []


class SendTest(unittest.TestCase):
    """FAKE rsync, ssh, and setpriv (see fake_sender_side); the real nanohpc-home-copy, and its relay between the
    user's rsync and root's ssh."""

    def send(self, folder: Path, environment: dict[str, str], extra: list[str]) -> subprocess.CompletedProcess[str]:
        (folder / "home" / USER).mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, str(SEND), "--source", str(folder / "home"), "--to", "login1=10.0.0.51", "--to",
             "front=10.0.0.10", "--users", USER, "--key", str(folder / "root-key"), "--known-hosts", "h", *extra],
            env=environment, capture_output=True, text=True, check=False, timeout=60,
        )  # fmt: skip

    def test_the_users_rsync_reads_as_the_user_and_root_keeps_the_key(self) -> None:
        """The sending rsync runs as the user (setpriv, no new privileges), so a link in their home to a file only
        root may read gives them nothing; only root's ssh has the copy key, and it runs the node's command for that
        user's home only. The copy takes the backup's file list (one source: cluster-backup), with the cluster's
        excludes first as in the backup, plain files only, no setuid or setgid bits, no file over 10 MB, and --delete
        without --delete-excluded, so the node's other files stay."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.send(folder, fake_sender_side(folder, 0), ["--exclude", ".cache/"])
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = calls(folder)
            self.assertEqual(len(lines), 8, lines)
            owner = (folder / "home" / USER).stat()
            self.assertEqual(
                lines[0].split(" -- ")[0],
                f"setpriv --reuid={owner.st_uid} --regid={owner.st_gid} --init-groups --no-new-privs",
            )
            client = lines[1]
            self.assertTrue(client.endswith(f"{folder}/home/{USER}/ root@10.0.0.51:/home/{USER}/"), client)
            self.assertNotIn("root-key", client)
            ssh = lines[2]
            self.assertIn(f"-i {folder}/root-key", ssh)
            self.assertIn("StrictHostKeyChecking=yes", ssh)
            self.assertTrue(
                ssh.endswith(f"root@10.0.0.51 rsync --server -tpre.iLsfxCIvu --timeout=600 . /home/{USER}/"), ssh
            )
            self.assertEqual(lines[3], f"got remote payload from {folder}/home/{USER}/")
            self.assertIn("root@10.0.0.10", lines[6])
            words = client.split(" ")
            for option in ("--recursive", "--times", "--perms", "--chmod=ug-s", "--delete", "--max-size=10M"):
                self.assertIn(option, words)
            for option in ("--archive", "--links", "--devices", "--owner", "--group", "--delete-excluded"):
                self.assertNotIn(option, words)
            loader = importlib.machinery.SourceFileLoader("cluster_backup", str(BACKUP))
            spec = importlib.util.spec_from_loader("cluster_backup", loader)
            assert spec is not None
            backup = importlib.util.module_from_spec(spec)
            loader.exec_module(backup)
            rules = [word for word in words if word.startswith("--include=")]
            self.assertEqual(rules, backup.home_rules("/"))
            self.assertLess(words.index("--exclude=.cache/"), words.index(rules[0]))
            self.assertEqual(words[-3], "--exclude=*")

    def test_a_command_for_another_home_is_never_sent(self) -> None:
        """The user's rsync (which the user could change) asks root's ssh to run a command: anything but an rsync
        server into that user's own home is refused before ssh runs."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            environment = {**fake_sender_side(folder, 0), "FAKE_REMOTE_PATH": "/home/someone-else/"}
            result = self.send(folder, environment, [])
            self.assertEqual(result.returncode, 1)
            self.assertFalse([line for line in calls(folder) if line.startswith("ssh ")])
            self.assertIn("refused", result.stderr)

    def test_a_failed_copy_fails_the_run_after_trying_every_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.send(folder, fake_sender_side(folder, 12), [])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(len([line for line in calls(folder) if line.startswith("ssh ")]), 2)
            self.assertIn(f"login1:{USER}, front:{USER}", result.stderr)

    def test_files_that_vanished_while_copying_are_fine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.send(folder, fake_sender_side(folder, 24), [])
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_or_linked_home_folder_is_not_copied(self) -> None:
        """A user without a home folder here, or whose home folder is a link, is not copied (nothing deleted there)
        and fails the run."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "elsewhere").mkdir()
            (folder / "home").mkdir()
            (folder / "home" / USER).symlink_to(folder / "elsewhere")
            result = subprocess.run(
                [sys.executable, str(SEND), "--source", str(folder / "home"), "--to", "login1=10.0.0.51",
                 "--users", USER, "carol", "--key", "k", "--known-hosts", "h"],
                env=fake_sender_side(folder, 0), capture_output=True, text=True, check=False, timeout=60,
            )  # fmt: skip
            self.assertEqual(result.returncode, 1)
            self.assertEqual(calls(folder), [])
            self.assertIn(f"login1:{USER}, login1:carol", result.stderr)


class ReceiveTest(unittest.TestCase):
    """The real nanohpc-home-copy-receive with a FAKE rsync and setpriv. Home folders are named after the user
    running the tests, who owns them."""

    def receive(self, folder: Path, command: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        home = folder / "home"
        (home / USER).mkdir(parents=True, exist_ok=True)
        environment = {
            **fake_programs(folder, 0),
            "SSH_ORIGINAL_COMMAND": command.replace("/home/", f"{home}/"),
            "NANOHPC_HOME_COPY_ROOT": str(home),
        }
        return subprocess.run([sys.executable, str(RECEIVE), *arguments], env=environment, capture_output=True,
                              text=True, check=False)  # fmt: skip

    def test_rsync_runs_as_the_owner_of_that_home_folder(self) -> None:
        """root drops to the folder's owner before rsync, so a copy can only write what that user could."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.receive(folder, LINE.format(user=USER), [])
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = calls(folder)
            self.assertEqual(len(lines), 2)
            owner = (folder / "home" / USER).stat()
            self.assertEqual(
                lines[0].split(" -- ")[0],
                f"setpriv --reuid={owner.st_uid} --regid={owner.st_gid} --init-groups --no-new-privs",
            )
            self.assertEqual(lines[1], LINE.format(user=USER).replace("/home/", f"{folder}/home/"))

    def test_a_folder_owned_by_someone_else_is_refused(self) -> None:
        """/home/NAME/ must belong to the user NAME, so a copy for one user never writes as another."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "home/someone-else").mkdir(parents=True)
            result = self.receive(folder, LINE.format(user="someone-else"), [])
            self.assertEqual(result.returncode, 1)
            self.assertIn("refused: /home/someone-else", result.stderr.replace(str(folder), ""))
            self.assertEqual(calls(folder), [])

    def test_with_a_home_disk_home_must_be_that_disk(self) -> None:
        """With home.device, /home must be a mount point of that disk: a copy never goes into the root disk's
        folder that the disk would cover (for example when the disk did not mount)."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.receive(folder, LINE.format(user=USER), ["/dev/vdb"])
            self.assertEqual(result.returncode, 1)
            self.assertIn("is not a mount point", result.stderr)
            self.assertEqual(calls(folder), [])

    def test_anything_else_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "home").mkdir()
            (folder / "home/escape").symlink_to("/etc")
            line = LINE.format(user=USER)
            for command in (
                "",
                "id",
                line + "; id",
                LINE.format(user="..").replace("/home/../", "/home/../etc/"),
                line.replace(f"/home/{USER}/", "/home/"),
                line.replace(f"/home/{USER}/", "/root/"),
                line.replace(f"/home/{USER}/", f"/home/{USER}/.ssh/"),
                LINE.format(user="escape"),
                LINE.format(user="nobody-here"),
                line.replace("--server", "--server --sender"),
                line.replace("--timeout=600", "--temp-dir=/tmp"),
                line.replace("--timeout=600", "--delete-excluded"),
                # Links, device files, owners, and groups are never received.
                line.replace("-tpre", "-ltpre"),
                line.replace("-tpre", "-Dtpre"),
                line.replace("-tpre", "-ogtpre"),
                f"rsync --server . /home/{USER}/",
            ):
                with self.subTest(command):
                    result = self.receive(folder, command, [])
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("refused", result.stderr)
            self.assertEqual(calls(folder), [])


ANSIBLE = ROOT / "src/nanohpc/ansible"


def task(path: Path, name: str) -> dict[str, Any]:
    return next(item for item in yaml.safe_load(path.read_text()) if item["name"] == name)


def rendered(text: str, values: dict[str, Any]) -> str:
    """Render a task's content as Ansible does (trim_blocks)."""
    environment = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    environment.filters["quote"] = shlex.quote  # Ansible's quote filter
    return environment.from_string(text).render(**values)


class WiringTest(unittest.TestCase):
    """The playbooks run these programs on the right machines, with the right arguments (rendered from the tasks)."""

    def values(self) -> dict[str, Any]:
        machines = {"login1": {"address": "10.0.0.51"}, "login2": {"address": "10.0.0.52"}}
        nanohpc = {"home": {"own": ["login2"]}, "machines": machines, "users": [{"name": "alice"}, {"name": "bob"}],
                   "backup": {"exclude": [".cache/", "*.ckpt"]}}  # fmt: skip
        return {"nanohpc": nanohpc, "groups": {"role_login": ["login1", "login2"]}}

    def test_the_home_machine_copies_to_every_own_home_at_night(self) -> None:
        content = task(ANSIBLE / "roles/home_own/tasks/send.yml", "Install the copy service and its nightly timer")
        service = rendered(content["ansible.builtin.copy"]["content"], {**self.values(), "item": "service"})
        self.assertIn(
            "ExecStart=/usr/local/bin/nanohpc-home-copy --source /home --to login2=10.0.0.52 --users alice bob "
            "--exclude .cache/ --exclude '*.ckpt' --key /etc/nanohpc/home-copy/id_ed25519 "
            "--known-hosts /etc/nanohpc/home-copy/known_hosts",
            service,
        )
        without_backup = {**self.values(), "item": "service"}
        without_backup["nanohpc"] = {**without_backup["nanohpc"], "backup": None}
        self.assertIn("--users alice bob --key", rendered(content["ansible.builtin.copy"]["content"], without_backup))
        self.assertIn("RequiresMountsFor=/home", service)
        timer = rendered(content["ansible.builtin.copy"]["content"], {**self.values(), "item": "timer"})
        self.assertIn("OnCalendar=*-*-* 02:30:00", timer)

    def test_the_front_node_sends_the_shared_boxes_to_every_login_node(self) -> None:
        """A login node with its own /home shows the shared /home's box too (when it has a notice), so it gets the
        same copies as the others."""
        content = task(ANSIBLE / "roles/login_node/tasks/send.yml", "Install the copy service and timer")
        service = rendered(content["ansible.builtin.copy"]["content"], {**self.values(), "item": "service"})
        self.assertIn("--login login1=10.0.0.51 --login login2=10.0.0.52 --key", service)

    def test_plays_reach_the_machines_with_their_own_home(self) -> None:
        site = {play["name"]: play for play in yaml.safe_load((ANSIBLE / "site.yml").read_text())}
        own = [play for play in site.values() if play["hosts"] == "home_own"]
        self.assertEqual([play.get("roles") for play in own], [["home_own"]])
        # The cleanup never runs on the home machine, whose quota units it would remove.
        cleanup = [play for play in site.values() if "home_own" in json.dumps(play.get("tasks", [])) and
                   '"tasks_from": "off"' in json.dumps(play.get("tasks", []))]  # fmt: skip
        self.assertEqual([play["hosts"] for play in cleanup], ["role_front:role_login:!home_own:!role_home"])
        self.assertIn("home_own", next(p for p in site.values() if p.get("roles") == ["monitoring"])["hosts"])
        partial = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        users = [play for play in partial if "home_server" in json.dumps(play.get("tasks", []))
                 and "users" in play.get("tags", [])]  # fmt: skip
        self.assertEqual([play["hosts"] for play in users], ["role_home:home_own"])
        # --only users also rewrites the nightly copy's user list.
        copies = [play for play in partial if "send" in json.dumps(play.get("tasks", []))
                  and "home_own" in json.dumps(play.get("tasks", []))]  # fmt: skip
        self.assertEqual([(play["hosts"], play["tags"]) for play in copies], [("role_home", ["users"])])

    def test_the_copy_units_go_when_unused_or_when_home_moves(self) -> None:
        """Without machines that keep their own /home, and on every machine that is not the home machine (after
        /home moved), the copy's timer is stopped, its units removed, and systemd reloaded."""
        site = yaml.safe_load((ANSIBLE / "site.yml").read_text())
        removal = [play for play in site if "send_off" in json.dumps(play.get("tasks", []))]
        self.assertEqual([play["hosts"] for play in removal], ["all:!role_home"])
        tasks = yaml.safe_load((ANSIBLE / "roles/home_own/tasks/send_off.yml").read_text())
        self.assertTrue(any(task.get("ansible.builtin.systemd_service", {}).get("daemon_reload") for task in tasks))
        self.assertIn("send_off", (ANSIBLE / "roles/home_own/tasks/send.yml").read_text())


if __name__ == "__main__":
    unittest.main()
