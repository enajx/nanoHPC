"""The Slack agent script (src/nanohpc/files/nanohpc-agent): the runner's rules, code changes, deploys from Slack,
Approve clicks, usage limits, and routing.

The runner is tested through its real Unix socket, running fake programs (sinfo, scancel, ssh, the deploy command)
from PATH. Slack and the Claude agent are faked (FakeSlack, fake_answer): these tests check our rules, not Slack
or the SDK. A local bare repository stands in for the cluster.yml repository's host (fake: no network, no key).
"""

import grp
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/nanohpc/files/nanohpc-agent"
ALICE = "U0ALICE"  # an approver
SAM = "U0SAM"  # an approver
BOB = "U0BOB"  # not an approver
CHANNEL = "C0HPC"
ZONE = ZoneInfo("Europe/Berlin")


def load_agent() -> Any:
    """Import the nanohpc-agent script as a module (it has no .py name)."""
    loader = importlib.machinery.SourceFileLoader("nanohpc_agent", str(COMMAND))
    spec = importlib.util.spec_from_loader("nanohpc_agent", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def limits() -> dict[str, Any]:
    """The agreed defaults: $1 a day for everyone, $1 a request, 10 requests a person, approvers unlimited."""
    return {"daily_usd": 1.0, "request_usd": 1.0, "requests_per_person": 10, "unlimited": [ALICE, SAM]}


class FakeSlack:
    """Records what the agent would post to Slack (fake: no Slack connection)."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.ephemeral: list[dict] = []
        self.updates: list[dict] = []

    def chat_postMessage(self, **kwargs: Any) -> dict:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "2.0"}

    def chat_postEphemeral(self, **kwargs: Any) -> dict:
        self.ephemeral.append(kwargs)
        return {"ok": True}

    def chat_update(self, **kwargs: Any) -> dict:
        self.updates.append(kwargs)
        return {"ok": True}

    def conversations_replies(self, **kwargs: Any) -> dict:
        return {"ok": True, "messages": []}

    def conversations_history(self, **kwargs: Any) -> dict:
        """The private chat, newest first, as Slack returns it."""
        return {
            "ok": True,
            "messages": [
                {"ts": "3.0", "user": ALICE, "text": "why?"},
                {"ts": "2.0", "bot_id": "B1", "text": "gpu1 looked idle."},
                {"ts": "1.0", "user": ALICE, "text": "is gpu1 busy"},
            ],
        }


def git(*args: str) -> str:
    """Run git for a test and return its output."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def wait_for(socket_path: str) -> None:
    """Wait until the runner has made its socket."""
    for _ in range(100):
        if Path(socket_path).exists():
            return
        time.sleep(0.05)


class ReadOnlyRule(unittest.TestCase):
    """The runner's read list: wide reads that change nothing, on any file except those that hold secrets; Slurm
    commands only in Slurm mode."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.log = str(base / "slurmctld.log")
        Path(self.log).write_text("error: node gpu1 not responding\n")
        (base / "bot-token").write_text("xoxb-1\n")
        self.escape = str(base / "escape.log")
        Path(self.escape).symlink_to(base / "bot-token")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def refused(self, argv: list[str]) -> bool:
        return self.agent.refusal(argv, True) is not None

    def test_dmesg_reads_but_never_clears_or_changes_the_console(self) -> None:
        self.assertFalse(self.refused(["dmesg", "-T", "--level", "err"]))
        for argv in (["dmesg", "-c"], ["dmesg", "-C"], ["dmesg", "--clear"], ["dmesg", "-n", "1"], ["dmesg", "-D"]):
            self.assertTrue(self.refused(argv), argv)

    def test_reads_are_allowed_on_any_ordinary_file(self) -> None:
        for argv in (["sinfo", "-N"], ["squeue"], ["sacct", "-j", "12"], ["scontrol", "show", "node", "gpu1"],
                     ["sacctmgr", "show", "qos"], ["systemctl", "status", "slurmctld"], ["systemctl", "cat", "munge"],
                     ["journalctl", "-u", "slurmctld", "-n", "50"], ["df", "-h"], ["tail", "-n", "200", "/var/log/syslog"],
                     ["grep", "-nE", "a|b", "/var/log/slurm/slurmctld.log"], ["cat", "/etc/slurm/slurm.conf"],
                     ["head", "/etc/fstab"], ["ls", "-la", "/etc/ssh"], ["find", "/etc", "-name", "*.conf"],
                     ["systemctl", "--no-pager", "status", "slurmd"], ["systemctl"], ["timedatectl"],
                     ["iptables", "-nvL", "INPUT"], ["ipmitool", "sdr", "list"], ["nft", "list", "ruleset"],
                     ["date", "+%s"], ["dpkg", "-l", "slurm"], ["scontrol", "-o", "show", "node"],
                     ["ps", "aux"], ["ps", "-ef"], ["nvidia-smi"], ["nvidia-smi", "--query-gpu=name", "--format=csv"],
                     ["ss", "-tlnp"], ["ip", "-br", "addr"], ["top", "-b", "-n", "1"], ["stat", "/etc/x"],
                     ["test", "-f", "/etc/x"], ["cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
                     ["git", "-C", "/var/lib/nanohpc/auto-deploy/config", "log", "-5", "--oneline"], ["git", "status"],
                     ["hostname"], ["ping", "-c", "2", "-W", "2", "gpu1"]):  # fmt: skip
            self.assertFalse(self.refused(argv), argv)

    def test_changes_and_endless_commands_are_refused(self) -> None:
        for argv in (["scontrol", "update", "NodeName=gpu1", "State=RESUME"], ["scancel", "12"],
                     ["systemctl", "restart", "slurmd"], ["journalctl", "--vacuum-size=1M"], ["journalctl", "--rotate"],
                     ["sacctmgr", "-i", "delete", "user", "x"], ["bash", "-c", "sinfo"], ["rm", "-rf", "/"], [],
                     ["find", "/tmp", "-delete"], ["find", "/", "-exec", "sh", "{}", ";"], ["tail", "-f", self.log],
                     ["nvidia-smi", "-pl", "200"], ["nvidia-smi", "dmon"], ["git", "commit", "-m", "x"],
                     ["git", "-c", "core.pager=sh", "log"], ["git", "branch", "-D", "main"], ["git", "grep", "-O", "x"],
                     ["ping", "gpu1"], ["hostname", "newname"], ["top"], ["ps", "auxe"], ["ps", "eww"],
                     ["ip", "link", "set", "eth0", "down"],
                     # Changes behind a harmless first word, and options that change things anywhere.
                     ["systemctl", "--no-pager", "restart", "slurmctld"], ["systemctl", "--failed", "poweroff"],
                     ["systemctl", "-H", "other", "status"], ["timedatectl", "set-time", "2020-01-01"],
                     ["date", "-s", "2020-01-01"], ["date", "010100002020"], ["ss", "-K", "dst", "10.0.0.1"],
                     ["ss", "-tK"], ["ipmitool", "fru", "write", "0", "/tmp/x"], ["ipmitool", "sensor", "thresh", "x"],
                     ["ipmitool", "-I", "lanplus", "-H", "bmc", "sdr"], ["iptables", "-L", "-Z"], ["iptables", "-F"],
                     ["lastlog", "-C", "-u", "alice"], ["journalctl", "--cursor-file=/etc/nologin"],
                     ["nft", "list ruleset; flush ruleset"], ["nft", "flush", "ruleset"], ["sensors", "-s"],
                     ["file", "-C", "-m", "x"], ["dpkg", "-l", "--purge", "x"], ["apt", "list", "-o", "x=y"],
                     ["chronyc", "tracking", "makestep"], ["resolvectl", "dns", "eth0", "1.1.1.1"],
                     ["scontrol", "update", "node=gpu1"], ["sacctmgr", "-i", "add", "user", "x"],
                     ["ip", "netns", "exec", "x", "sh"], ["munge", "-n"],
                     # Recursive reads, which could reach files with secrets.
                     ["grep", "-r", "munge", "/var/log"], ["grep", "-d", "recurse", "x", "/etc"],
                     ["grep", "--directories=recurse", "x", "/etc"], ["diff", "-r", "/etc", "/opt"],
                     ["cat", "line\nbreak"]):  # fmt: skip
            self.assertTrue(self.refused(argv), argv)

    def test_files_with_secrets_are_never_read(self) -> None:
        for argv in (["cat", "/etc/nanohpc-agent/env"], ["cat", "/etc/shadow"],
                     ["tail", "/etc/nanohpc/alertmanager/slack-webhook"],
                     ["cat", "/var/lib/nanohpc-agent-runner/repository_ed25519"], ["cat", ".ssh/id_ed25519"],
                     ["cat", "/etc/ssh/ssh_host_ed25519_key"], ["cat", "/etc/munge/munge.key"],
                     ["cat", "/proc/1/environ"], ["cat", "/etc/slurm/slurmdbd.conf"], ["cat", "/etc/nanohpc/slurmdbd.env"],
                     ["cat", "/var/lib/nanohpc/auto-deploy/config/.env"], ["cat", "/etc/nanohpc/metrics-tls/node.key"],
                     ["grep", "-f", "/root/.ssh/id_rsa", self.log], ["cat", self.escape], ["cat", "/opt/x/.env"],
                     ["grep", "-r", "xoxb", "/etc"], ["grep", "-rn", "key", "/"], ["grep", "-r", "xoxb", "etc"],
                     ["diff", "-r", "/etc", "/tmp"], ["cat", "/dev/sda"], ["head", "-c", "100", "/dev/mem"],
                     ["cat", "/proc/1/mem"], ["cat", "/proc/kcore"], ["cat", "/swap.img"],
                     ["cat", "/var/lib/grafana/grafana.db"]):  # fmt: skip
            self.assertTrue(self.refused(argv), argv)

    def test_peoples_own_files_are_read_only_when_an_approver_asks(self) -> None:
        rule = self.agent.RunnerRule("front", [ALICE], {"gpu1": "10.0.0.7"}, "key", "known_hosts", True)
        for argv in (["ls", "-la", "/home/bob"], ["cat", "/tmp/x"], ["cat", "tmp/x"], ["tail", "/var/tmp/y"]):
            self.assertIsNotNone(rule.refusal(argv, BOB, "front", "read", None), argv)
            self.assertIsNone(rule.refusal(argv, ALICE, "front", "read", None), argv)

    def test_paths_are_checked_where_the_command_runs(self) -> None:
        """On another machine, a link to a secret is found with that machine's own realpath (stand-in here)."""
        rule = self.agent.RunnerRule("front", [ALICE], {"gpu1": "10.0.0.7"}, "key", "known_hosts", True)
        rule.real_paths = lambda paths, host: ["/etc/munge/munge.key" for _ in paths]
        refused = rule.refusal(["cat", "-v", "/var/log/innocent.log"], ALICE, "gpu1", "change", ALICE)
        self.assertIn("holds secrets", refused)
        rule.real_paths = lambda paths, host: None
        self.assertIn("could not check", rule.refusal(["cat", "/var/log/syslog"], BOB, "gpu1", "read", None))

    def test_files_with_secrets_are_refused_even_as_an_approvers_change(self) -> None:
        rule = self.agent.RunnerRule("front", [ALICE], {"gpu1": "10.0.0.7"}, "key", "known_hosts", True)
        for argv in (["cat", "/etc/munge/munge.key"], ["cp", "/root/.ssh/id_ed25519", "/tmp/k"]):
            self.assertIsNotNone(rule.refusal(argv, ALICE, "gpu1", "change", ALICE), argv)

    def test_git_trusts_only_roots_repositories_with_no_pager_or_hooks(self) -> None:
        rule = self.agent.RunnerRule("front", [ALICE], {"gpu1": "10.0.0.7"}, "key", "known_hosts", True)
        command = rule.command(["git", "log", "-1"], "front")
        self.assertEqual(command, ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "--no-pager",
                                   "log", "-1"])  # fmt: skip
        self.assertNotIn("safe.directory", " ".join(rule.command(["git", "status"], "gpu1")))

    def test_monitor_mode_has_no_slurm_commands(self) -> None:
        self.assertIsNotNone(self.agent.refusal(["sinfo"], False))
        self.assertIsNone(self.agent.refusal(["nvidia-smi"], False))

    def test_secrets_are_masked(self) -> None:
        text = (
            "xoxb-1-abc xapp-1-def sk-ant-api03-x https://hooks.slack.com/services/T/B/x "
            "NANOHPC_DEPLOY_WEBHOOK_SECRET=abc root:$6$salt$hash:19000"
        )
        self.assertEqual(self.agent.mask(text), "[hidden] [hidden] [hidden] [hidden] [hidden] root:[hidden]:19000")

    def test_deploy_arguments(self) -> None:
        for args in ([], ["--dry-run"], ["--only", "users"], ["--only", "node", "gpu2"]):
            self.assertIsNone(self.agent.deploy_refusal(args, True), args)
        self.assertIsNone(self.agent.deploy_refusal(["--only", "node", "gpu2"], False))
        self.assertIsNotNone(self.agent.deploy_refusal(["--only", "users"], False))
        for args in (["--only"], ["--only", "everything"], ["--only", "node"], ["--ssh-config", "/x"],
                     ["--only", "users", "extra"], ["--only", "node", "gpu2;rm"]):  # fmt: skip
            self.assertIsNotNone(self.agent.deploy_refusal(args, True), args)


class Runner(unittest.TestCase):
    """The runner through its real socket, with fake sinfo, scancel, ssh, and deploy programs on PATH (fake
    programs: no Slurm, no machine is reached, nothing is deployed)."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        programs = {
            "sinfo": 'echo "gpu1 idle $*"\necho leaked xoxb-9-secret',
            "scancel": 'echo "cancelled $*"',
            "ssh": 'case "$*" in *"realpath -m -- "*) eval "set -- ${@: -1}"; shift 3; printf "%s\\n" "$@";;'
            ' *) for arg in "$@"; do echo "arg:$arg"; done;; esac',
            "fake-deploy": 'echo "deploy $*"',
        }
        for name, body in programs.items():
            program = bin_dir / name
            program.write_text(f"#!/bin/bash\n{body}\n")
            program.chmod(0o755)
        self.socket = str(base / "runner.sock")
        self.log = base / "runner.log"
        settings = {
            "socket": self.socket,
            "socket_group": grp.getgrgid(os.getgid()).gr_name,
            "log": str(self.log),
            "front": "front",
            "approvers": [ALICE, SAM],
            "hosts": {"gpu1": "10.0.0.7"},
            "key": str(base / "key"),
            "known_hosts": str(base / "known_hosts"),
            "slurm": True,
            "work": str(base / "work"),
            "agent_user": os.environ.get("USER", ""),
            "repository": None,
            "deploy": [str(bin_dir / "fake-deploy"), "--again"],
        }
        (base / "runner.json").write_text(json.dumps(settings))
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.process = subprocess.Popen(
            [sys.executable, str(COMMAND), "runner", "--config", str(base / "runner.json")], env=env
        )
        wait_for(self.socket)

    def tearDown(self) -> None:
        self.process.terminate()
        self.process.wait()
        self.folder.cleanup()

    def ask(self, argv: list[str], requester: str, host: str, mode: str, approver: str | None) -> dict:
        return self.agent.ask_runner(self.socket, argv, requester, host, mode, approver)

    def test_the_socket_is_for_the_bots_group_only(self) -> None:
        self.assertEqual(os.stat(self.socket).st_mode & 0o777, 0o660)

    def test_read_runs_masked_and_logged(self) -> None:
        answer = self.ask(["sinfo", "-N"], BOB, "front", "read", None)
        self.assertTrue(answer["ran"])
        self.assertIn("gpu1 idle -N", answer["output"])
        self.assertNotIn("xoxb-9", answer["output"])
        record = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual((record["requester"], record["argv"], record["ran"]), (BOB, ["sinfo", "-N"], True))

    def test_a_change_asked_as_a_read_is_refused_even_for_an_approver(self) -> None:
        answer = self.ask(["scancel", "12"], ALICE, "front", "read", None)
        self.assertFalse(answer["ran"])
        self.assertIn("read-only list", answer["output"])

    def test_a_change_from_an_approver_runs(self) -> None:
        for person in (ALICE, SAM):
            answer = self.ask(["scancel", "12"], person, "front", "change", None)
            self.assertTrue(answer["ran"])
            self.assertIn("cancelled 12", answer["output"])

    def test_a_change_from_anyone_else_runs_only_with_an_approvers_click(self) -> None:
        self.assertFalse(self.ask(["scancel", "12"], BOB, "front", "change", None)["ran"])
        self.assertFalse(self.ask(["scancel", "12"], BOB, "front", "change", BOB)["ran"])
        self.assertTrue(self.ask(["scancel", "12"], BOB, "front", "change", SAM)["ran"])
        record = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual((record["requester"], record["approver"], record["mode"]), (BOB, SAM, "change"))

    def test_another_machine_is_reached_as_root_with_the_runner_key_and_a_quoted_command(self) -> None:
        answer = self.ask(["systemctl", "status", "slurmd; rm -rf /"], BOB, "gpu1", "read", None)
        self.assertTrue(answer["ran"])
        self.assertIn("arg:root@10.0.0.7", answer["output"])
        self.assertIn("arg:StrictHostKeyChecking=yes", answer["output"])
        self.assertIn("arg:cd / && systemctl status 'slurmd; rm -rf /'", answer["output"])

    def test_an_unknown_machine_is_refused(self) -> None:
        answer = self.ask(["sinfo"], ALICE, "elsewhere", "change", None)
        self.assertFalse(answer["ran"])
        self.assertIn("unknown machine", answer["output"])

    def test_a_deploy_runs_only_as_an_approved_change_with_allowed_arguments(self) -> None:
        self.assertFalse(self.ask(["nanohpc", "deploy"], ALICE, "front", "read", None)["ran"])
        self.assertFalse(self.ask(["nanohpc", "deploy"], BOB, "front", "change", None)["ran"])
        refused = self.ask(["nanohpc", "deploy", "--ssh-config", "/tmp/x"], ALICE, "front", "change", None)
        self.assertFalse(refused["ran"])
        self.assertIn("--only users", refused["output"])
        answer = self.ask(["nanohpc", "deploy", "--only", "users"], BOB, "front", "change", ALICE)
        self.assertTrue(answer["ran"], answer["output"])
        self.assertIn("deploy --again -- --only users", answer["output"])
        dry = self.ask(["nanohpc", "deploy", "--dry-run"], ALICE, "front", "change", None)
        self.assertIn("deploy --again -- --dry-run", dry["output"])

    def test_code_requests_say_there_is_no_repository(self) -> None:
        answer = self.agent.ask_code(self.socket, {"code": "start", "requester": ALICE})
        self.assertFalse(answer["ok"])
        self.assertIn("no repository", answer["output"])

    def test_a_missing_program_is_reported_and_the_runner_keeps_working(self) -> None:
        missing = self.ask(["sdiag"], BOB, "front", "read", None)
        self.assertFalse(missing["ran"])
        self.assertIn("not installed", missing["output"])
        self.assertTrue(self.ask(["sinfo"], BOB, "front", "read", None)["ran"])


class FrontChanges(unittest.TestCase):
    """On the front node, changes may write anywhere except the agent's own files."""

    def setUp(self) -> None:
        agent = load_agent()
        self.rule = agent.RunnerRule("front", [ALICE], {"gpu1": "10.0.0.7"}, "key", "known_hosts", True)

    def refused(self, argv: list[str]) -> bool:
        return self.rule.refusal(argv, ALICE, "front", "change", None) is not None

    def test_an_ordinary_change_is_allowed(self) -> None:
        self.assertFalse(self.refused(["systemctl", "restart", "nginx"]))

    def test_the_agents_own_files_and_services_are_refused(self) -> None:
        for argv in (["rm", "/etc/nanohpc-agent/bot.json"], ["cp", "x", "/usr/local/bin/nanohpc-agent"],
                     ["systemctl", "stop", "nanohpc-agent-runner"]):  # fmt: skip
            self.assertTrue(self.refused(argv), argv)

    def test_programs_that_leave_the_runners_limits_are_refused(self) -> None:
        for argv in (["systemd-run", "rm", "-rf", "/x"], ["nsenter", "-t", "1", "-m"], ["mount", "-o", "rw", "/"]):
            self.assertTrue(self.refused(argv), argv)


class CodeChanges(unittest.TestCase):
    """Edits to the cluster.yml repository through the runner's code: the agent edits its own folder; only an
    approver's click commits and pushes, to the configured branch."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.origin = base / "origin.git"
        seed = base / "seed"
        git("init", "-q", "--bare", "-b", "stable", str(self.origin))
        git("init", "-q", "-b", "stable", str(seed))
        (seed / "cluster.yml").write_text("users: [alice]\n")
        git("-C", str(seed), "add", "-A")
        git("-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
        git("-C", str(seed), "push", "-q", str(self.origin), "stable")
        self.work = base / "work"
        self.work.mkdir()
        self.code = self.agent.CodeChanges(
            str(self.origin), "stable", str(base / "clone"), str(self.work), os.environ.get("USER", ""),
            str(base / "key"), str(base / "known"), [ALICE, SAM], "nanoHPC agent", "agent@front",
            "The automatic deploy picks it up.",
        )  # fmt: skip

    def tearDown(self) -> None:
        self.folder.cleanup()

    def pushed(self) -> str:
        return git("-C", str(self.origin), "show", "stable:cluster.yml")

    def edit_and_stage(self) -> dict:
        started = self.code.start()
        self.assertTrue(started["ok"], started["output"])
        (self.work / "cluster.yml").write_text("users: [alice, bob]\n")
        return self.code.stage()

    def test_the_agents_folder_is_a_copy_of_the_branch(self) -> None:
        self.code.start()
        self.assertEqual((self.work / "cluster.yml").read_text(), "users: [alice]\n")

    def test_stage_shows_the_diff_and_pushes_nothing(self) -> None:
        staged = self.edit_and_stage()
        self.assertIn("+users: [alice, bob]", staged["diff"])
        self.assertEqual(self.pushed(), "users: [alice]\n")

    def test_push_needs_an_approvers_click_even_when_an_approver_asked(self) -> None:
        staged = self.edit_and_stage()
        self.assertFalse(self.code.push(ALICE, None, staged["tree"], "Add bob")["ok"])
        self.assertFalse(self.code.push(ALICE, BOB, staged["tree"], "Add bob")["ok"])
        self.assertEqual(self.pushed(), "users: [alice]\n")

    def test_an_approved_push_lands_on_the_branch_with_who_asked_and_approved(self) -> None:
        staged = self.edit_and_stage()
        done = self.code.push(BOB, SAM, staged["tree"], "Add bob\n\n- users: bob")
        self.assertTrue(done["ok"], done["output"])
        self.assertIn("The automatic deploy picks it up.", done["output"])
        self.assertEqual(self.pushed(), "users: [alice, bob]\n")
        message = git("-C", str(self.origin), "log", "-1", "--format=%B%an", "stable")
        self.assertIn(f"Requested-by: {BOB}", message)
        self.assertIn(f"Approved-by: {SAM}", message)
        self.assertIn("nanoHPC agent", message)

    def test_a_push_of_something_other_than_what_was_shown_is_refused(self) -> None:
        staged = self.edit_and_stage()
        (self.work / "cluster.yml").write_text("users: [mallory]\n")
        self.code.stage()
        self.assertFalse(self.code.push(ALICE, ALICE, staged["tree"], "Add bob")["ok"])
        self.assertEqual(self.pushed(), "users: [alice]\n")


class Approval(unittest.TestCase):
    """Changes: approvers' run at once; anyone else's wait for a click by an approver (fake Slack and runner)."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.slack = FakeSlack()
        self.ran: list[dict] = []
        self.code_requests: list[dict] = []
        self.approvals = self.agent.Approvals({ALICE: "alice", SAM: "sam"}, self.fake_runner, self.fake_code)

    def fake_runner(self, argv: list[str], requester: str, host: str, mode: str, approver: str | None) -> dict:
        self.ran.append({"argv": argv, "requester": requester, "host": host, "mode": mode, "approver": approver})
        return {"ran": True, "output": "done\n[exit 0]"}

    def fake_code(self, request: dict) -> dict:
        self.code_requests.append(request)
        return {"ok": True, "output": "Pushed abc123 to main"}

    def ask(self, requester: str, risky: bool) -> str:
        return self.ask_argv(["systemctl", "restart", "slurmd"], requester, risky)

    def ask_argv(self, argv: list[str], requester: str, risky: bool) -> str:
        """Ask for a change, then post what was held back, as handle_event does after its reply."""
        held: list = []
        told = self.approvals.ask(argv, "gpu1", "slurmd is stuck", risky, requester, self.slack, CHANNEL, "1.0", held)
        for later in held:
            later()
        return told

    def test_a_change_posts_one_plain_line_not_its_output(self) -> None:
        self.ask(ALICE, False)
        self.assertEqual(len(self.slack.posts), 1)
        self.assertEqual(self.slack.posts[0]["text"], "Done: `systemctl restart slurmd` on gpu1.")

    def test_a_failed_or_refused_change_says_so(self) -> None:
        self.approvals.run = lambda argv, requester, host, mode, approver: {"ran": True, "output": "oops\n[exit 2]"}
        self.ask(ALICE, False)
        self.assertIn("Failed (exit 2)", self.slack.posts[0]["text"])
        self.approvals.run = lambda argv, requester, host, mode, approver: {"ran": False, "output": "refused: no."}
        self.ask(ALICE, False)
        self.assertEqual(self.slack.posts[1]["text"], "Not done: `systemctl restart slurmd` on gpu1. refused: no.")

    def test_a_dry_run_is_not_posted(self) -> None:
        told = self.ask_argv(["nanohpc", "deploy", "--dry-run"], ALICE, False)
        self.assertEqual(len(self.ran), 1)
        self.assertIn("done", told)
        self.assertEqual(self.slack.posts, [])

    def click(self, user: str, action: str) -> None:
        with_buttons = [post for post in self.slack.posts if "blocks" in post][-1]
        button = with_buttons["blocks"][-1]["elements"][0 if action == "approve" else 1]
        body = {
            "user": {"id": user},
            "channel": {"id": CHANNEL},
            "message": {"ts": "2.0"},
            "actions": [{"action_id": button["action_id"], "value": button["value"]}],
        }
        self.approvals.click(body, self.slack)

    def test_a_change_from_an_approver_runs_at_once(self) -> None:
        for person in (ALICE, SAM):
            self.ask(person, False)
        self.assertEqual(
            [(run["requester"], run["mode"], run["approver"]) for run in self.ran],
            [(ALICE, "change", None), (SAM, "change", None)],
        )
        self.assertIn("Done", self.slack.posts[0]["text"])

    def test_a_change_from_anyone_else_waits_and_mentions_the_approvers(self) -> None:
        told = self.ask(BOB, False)
        self.assertEqual(self.ran, [])
        self.assertIn("waits for their Approve", told)
        message = self.slack.posts[0]
        self.assertEqual(message["thread_ts"], "1.0")
        self.assertIn(f"<@{ALICE}> <@{SAM}>: <@{BOB}> asks", message["text"])
        self.assertIn("systemctl restart slurmd", message["text"])

    def test_a_click_by_anyone_else_does_nothing(self) -> None:
        self.ask(BOB, False)
        self.click(BOB, "approve")
        self.assertEqual(self.ran, [])
        self.assertEqual(self.slack.ephemeral[0]["user"], BOB)
        self.click(ALICE, "approve")
        self.assertEqual(len(self.ran), 1)

    def test_an_approvers_click_runs_exactly_that_command_once(self) -> None:
        self.ask(BOB, False)
        self.click(SAM, "approve")
        self.click(ALICE, "approve")
        self.assertEqual(
            self.ran,
            [{"argv": ["systemctl", "restart", "slurmd"], "requester": BOB, "host": "gpu1", "mode": "change",
              "approver": SAM}],
        )  # fmt: skip
        self.assertIn("sam", self.slack.updates[0]["text"])
        self.assertIn("Done", self.slack.posts[-1]["text"])
        self.assertNotIn("```", self.slack.posts[-1]["text"])

    def test_deny_drops_the_change(self) -> None:
        self.ask(BOB, False)
        self.click(ALICE, "deny")
        self.click(SAM, "approve")
        self.assertEqual(self.ran, [])
        self.assertIn("Denied", self.slack.updates[0]["text"])

    def propose(self, requester: str) -> str:
        staged = {"ok": True, "tree": "t1", "stat": " cluster.yml | 2 +-", "diff": "-users: [alice]\n+users: [bob]"}
        held: list = []
        told = self.approvals.ask_push(staged, "Add bob", requester, self.slack, CHANNEL, "1.0", held)
        self.assertEqual(self.slack.posts, [])
        for later in held:
            later()
        return told

    def test_a_push_shows_the_diff_and_waits_even_when_an_approver_asked(self) -> None:
        told = self.propose(ALICE)
        self.assertIn("waits for their Approve", told)
        self.assertEqual(self.code_requests, [])
        shown = " ".join(post["text"] for post in self.slack.posts)
        self.assertIn("+users: [bob]", shown)
        self.assertIn("Add bob", shown)

    def test_a_push_runs_only_after_an_approvers_click_with_the_shown_tree(self) -> None:
        self.propose(BOB)
        self.click(BOB, "approve")
        self.assertEqual(self.code_requests, [])
        self.click(SAM, "approve")
        self.assertEqual(
            self.code_requests,
            [{"code": "push", "requester": BOB, "approver": SAM, "tree": "t1", "message": "Add bob"}],
        )
        self.assertIn("Pushed", self.slack.posts[-1]["text"])

    def test_in_a_private_chat_nobody_is_mentioned(self) -> None:
        held: list = []
        self.approvals.ask(["rm", "/tmp/x"], "gpu1", "test", True, ALICE, self.slack, "D2", "1.0", held)
        held[0]()
        self.assertNotIn("<@", self.slack.posts[0]["text"])
        self.assertIn("Approve", json.dumps(self.slack.posts[0]["blocks"]))

    def test_a_change_that_could_cut_remote_access_waits_even_for_an_approver(self) -> None:
        self.ask(ALICE, True)
        self.assertEqual(self.ran, [])
        self.click(ALICE, "approve")
        self.assertEqual(self.ran[0]["approver"], ALICE)


class Limits(unittest.TestCase):
    """Usage limits: a daily amount for everyone, an amount a request, requests a person a day; approvers have
    no per-person limit."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = self.agent.Ledger(Path(self.folder.name) / "usage.json", limits(), ZONE)
        self.morning = datetime(2026, 11, 6, 9, 0, tzinfo=ZONE)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_request_budget_is_what_is_left_of_the_day(self) -> None:
        self.assertEqual(self.ledger.budget(self.morning), 1.0)
        self.ledger.record(BOB, 0.7, self.morning)
        self.assertAlmostEqual(self.ledger.budget(self.morning), 0.3)

    def test_daily_cap_refuses_everyone_until_midnight(self) -> None:
        self.ledger.record(ALICE, 1.02, self.morning)
        self.assertIsNotNone(self.ledger.refusal(ALICE, self.morning))
        self.assertIsNotNone(self.ledger.refusal(BOB, self.morning))
        next_day = datetime(2026, 11, 7, 0, 1, tzinfo=ZONE)
        self.assertIsNone(self.ledger.refusal(BOB, next_day))
        self.assertEqual(self.ledger.budget(next_day), 1.0)

    def test_ten_requests_per_person_approvers_unlimited(self) -> None:
        for _ in range(10):
            self.assertIsNone(self.ledger.refusal(BOB, self.morning))
            self.ledger.record(BOB, 0.0, self.morning)
            self.ledger.record(ALICE, 0.0, self.morning)
        self.assertIsNotNone(self.ledger.refusal(BOB, self.morning))
        self.assertIsNone(self.ledger.refusal(ALICE, self.morning))

    def test_survives_a_restart(self) -> None:
        self.ledger.record(BOB, 0.6, self.morning)
        again = self.agent.Ledger(Path(self.folder.name) / "usage.json", limits(), ZONE)
        self.assertAlmostEqual(again.budget(self.morning), 0.4)

    def test_the_machines_own_zone_when_none_is_set(self) -> None:
        ledger = self.agent.Ledger(Path(self.folder.name) / "local.json", limits(), None)
        ledger.record(BOB, 0.5, self.morning)
        self.assertAlmostEqual(ledger.budget(self.morning), 0.5)

    def test_cap_notice_is_due_once_a_day(self) -> None:
        self.ledger.record(BOB, 1.0, self.morning)
        self.assertTrue(self.ledger.cap_notice_due(self.morning))
        self.assertFalse(self.ledger.cap_notice_due(self.morning))


def bot_settings(mode: str, code: bool, people: str | None) -> dict[str, Any]:
    """The bot's settings as the deploy writes them (only what the prompts and routing read)."""
    return {
        "mode": mode,
        "cluster": "lab",
        "front": "front",
        "machines": ["gpu1", "gpu2"],
        "docs": ["/srv/nanohpc-web/config"] if mode == "slurm" else [],
        "people": people,
        "code": code,
        "after_push": "The automatic deploy picks it up within 10 minutes.",
        "channel": CHANNEL,
        "approvers": {ALICE: "alice", SAM: "sam"},
        "users": {ALICE: "alice", BOB: "bob"},
    }


class Prompts(unittest.TestCase):
    """The rules follow the cluster: its machines, its mode, the people file, and whether it has a repository."""

    def setUp(self) -> None:
        self.agent = load_agent()

    def test_system_prompt_has_rules_personality_and_notes(self) -> None:
        prompt = self.agent.system_prompt(bot_settings("slurm", True, None), "CORE: plain", "NOTES: gpu2 is loud")
        self.assertIn("for the lab cluster", prompt)
        self.assertIn("gpu1, gpu2", prompt)
        self.assertIn("the queue, someone's jobs", prompt)
        self.assertIn("CORE: plain", prompt)
        self.assertIn("NOTES: gpu2 is loud", prompt)
        self.assertNotIn("let it shape", prompt)

    def test_people_file_and_monitor_mode(self) -> None:
        prompt = self.agent.system_prompt(bot_settings("monitor", False, "/etc/nanohpc-agent/people.md"), "c", "")
        self.assertIn("read /etc/nanohpc-agent/people.md first", prompt)
        self.assertIn("never state anything from it as a fact, even inside a joke", prompt)
        self.assertIn("front is the monitoring machine", prompt)
        self.assertNotIn("Slurm", prompt)

    def test_monitor_mode_deploys_only_whole_or_one_machine(self) -> None:
        rules = self.agent.deep_rules(bot_settings("monitor", True, None))
        self.assertIn('["nanohpc", "deploy", "--only", "node", NAME]', rules)
        self.assertNotIn("--only users", rules)

    def test_deep_rules_with_and_without_a_repository(self) -> None:
        with_code = self.agent.deep_rules(bot_settings("slurm", True, None))
        self.assertIn('["nanohpc", "deploy", "--dry-run"]', with_code)
        self.assertIn("The automatic deploy picks it up within 10 minutes.", with_code)
        without = self.agent.deep_rules(bot_settings("slurm", False, None))
        self.assertIn("has no repository", without)
        self.assertNotIn("start_code_change", without)


class Notes(unittest.TestCase):
    """The notes it writes about itself: kept under 1 KB by the code."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.folder = tempfile.TemporaryDirectory()
        self.notes = Path(self.folder.name) / "notes.md"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_notes_up_to_4_kb_are_saved_and_may_hold_people_and_events(self) -> None:
        self.assertIsNone(self.agent.save_notes(self.notes, "x" * 4096))
        self.assertEqual(len(self.notes.read_text()), 4096)
        prompt = self.agent.system_prompt(bot_settings("slurm", True, None), "c", "")
        self.assertIn("events worth remembering", prompt)
        self.assertIn("Nobody is told when you write", prompt)

    def test_notes_over_the_cap_are_refused_and_the_old_ones_kept(self) -> None:
        self.assertIsNone(self.agent.save_notes(self.notes, "short"))
        refused = self.agent.save_notes(self.notes, "x" * 4097)
        self.assertIn("4096", refused)
        self.assertEqual(self.notes.read_text(), "short")


class Routing(unittest.TestCase):
    """Only mentions in the agent's channel, and approvers' private messages, reach Claude; anything else gets one
    redirect (fake Slack and agent)."""

    def setUp(self) -> None:
        self.agent = load_agent()
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = self.agent.Ledger(Path(self.folder.name) / "usage.json", limits(), ZONE)
        self.slack = FakeSlack()
        self.asked: list[dict] = []
        self.ran: list[list[str]] = []
        self.now = datetime(2026, 11, 6, 9, 0, tzinfo=ZONE)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def fake_answer(self, prompt: str, budget: float, requester: str, post, status, change, push) -> tuple[str, float]:
        self.asked.append({"prompt": prompt, "budget": budget, "requester": requester})
        if "check" in prompt:
            status("the queue")
            status("gpu1")
            status("the queue")
        if "restart" in prompt:
            change(["systemctl", "restart", "slurmd"], "gpu1", "slurmd is stuck", False)
        return "all machines idle, token xoxb-1-2", 0.25

    def fake_runner(self, argv: list[str], requester: str, host: str, mode: str, approver: str | None) -> dict:
        self.ran.append(argv)
        return {"ran": True, "output": "done"}

    def handle(self, event: dict) -> None:
        approvals = self.agent.Approvals(
            {ALICE: "alice"}, self.fake_runner, lambda request: {"ok": False, "output": ""}
        )
        config = bot_settings("slurm", True, None)
        self.agent.handle_event(event, config, self.ledger, self.slack, self.fake_answer, approvals, self.now)

    def test_a_change_asked_in_a_request_waits_for_approval_in_its_thread(self) -> None:
        self.handle({"type": "app_mention", "channel": CHANNEL, "user": BOB, "ts": "1.0", "text": "restart slurmd"})
        self.assertEqual(self.ran, [])
        waiting = self.slack.posts[-1]
        self.assertEqual((waiting["channel"], waiting["thread_ts"]), (CHANNEL, "1.0"))
        self.assertIn("Approve", json.dumps(waiting["blocks"]))

    def test_the_approve_message_comes_after_the_reply(self) -> None:
        self.handle({"type": "app_mention", "channel": CHANNEL, "user": BOB, "ts": "1.0", "text": "restart slurmd"})
        self.assertEqual(len(self.slack.posts), 2)
        self.assertIn("all machines idle", self.slack.posts[0]["text"])
        self.assertIn("blocks", self.slack.posts[1])

    def test_checks_show_as_one_status_line_edited_in_place(self) -> None:
        self.handle({"type": "app_mention", "channel": CHANNEL, "user": BOB, "ts": "1.0", "text": "<@UBOT> check"})
        self.assertEqual(len(self.slack.posts), 2)
        self.assertIn("Checking the queue", self.slack.posts[0]["text"])
        self.assertEqual(self.slack.posts[0]["thread_ts"], "1.0")
        self.assertIn("all machines idle", self.slack.posts[1]["text"])
        self.assertEqual([update["ts"] for update in self.slack.updates], ["2.0"] * 3)
        self.assertIn("Checking gpu1", self.slack.updates[0]["text"])
        self.assertEqual(self.slack.updates[-1]["text"], "_Checked the queue, gpu1._")

    def test_the_rules_say_to_speak_as_the_clusters_manager(self) -> None:
        prompt = self.agent.system_prompt(bot_settings("slurm", True, None), "c", "")
        self.assertIn("You speak as the cluster's manager", prompt)

    def test_mention_in_the_channel_is_answered_in_its_thread(self) -> None:
        self.handle({"type": "app_mention", "channel": CHANNEL, "user": BOB, "ts": "1.0", "text": "<@UBOT> status"})
        self.assertEqual(len(self.asked), 1)
        self.assertIn("(bob)", self.asked[0]["prompt"])
        self.assertIn("request 1 today", self.asked[0]["prompt"])
        reply = self.slack.posts[-1]
        self.assertEqual((reply["channel"], reply["thread_ts"]), (CHANNEL, "1.0"))
        self.assertNotIn("xoxb-1", reply["text"])
        self.assertAlmostEqual(self.ledger.budget(self.now), 0.75)

    def test_mention_in_another_channel_gets_only_a_private_redirect(self) -> None:
        self.handle({"type": "app_mention", "channel": "COTHER", "user": BOB, "ts": "1.0", "text": "status"})
        self.assertEqual((self.asked, self.slack.posts), ([], []))
        self.assertEqual(self.slack.ephemeral[0]["user"], BOB)
        self.assertIn(f"<#{CHANNEL}>", self.slack.ephemeral[0]["text"])

    def test_private_message_from_someone_else_gets_only_a_redirect(self) -> None:
        self.handle({"type": "message", "channel_type": "im", "channel": "D1", "user": BOB, "ts": "1.0", "text": "hi"})
        self.assertEqual(self.asked, [])
        self.assertIn(f"<#{CHANNEL}>", self.slack.posts[0]["text"])

    def test_private_message_from_an_approver_is_answered_with_what_came_before(self) -> None:
        self.handle(
            {"type": "message", "channel_type": "im", "channel": "D2", "user": ALICE, "ts": "3.0", "text": "why?"}
        )
        prompt = self.asked[0]["prompt"]
        self.assertIn("in a private message", prompt)
        self.assertLess(prompt.index("is gpu1 busy"), prompt.index("gpu1 looked idle"))
        self.assertEqual(prompt.count("why?"), 1)
        self.assertEqual((self.slack.posts[-1]["channel"], self.slack.posts[-1]["thread_ts"]), ("D2", "3.0"))

    def test_bot_messages_and_edits_are_ignored(self) -> None:
        self.handle(
            {"type": "message", "channel_type": "im", "channel": "D1", "bot_id": "B1", "ts": "1.0", "text": "x"}
        )
        self.handle({"type": "message", "channel_type": "im", "channel": "D1", "subtype": "message_changed", "ts": "1"})
        self.assertEqual((self.asked, self.slack.posts, self.slack.ephemeral), ([], [], []))

    def test_over_the_limit_is_refused_in_the_thread_without_asking_claude(self) -> None:
        self.ledger.record(ALICE, 1.0, self.now)
        self.handle({"type": "app_mention", "channel": CHANNEL, "user": BOB, "ts": "1.0", "text": "status"})
        self.assertEqual(self.asked, [])
        self.assertIn("limit", self.slack.posts[-1]["text"])


if __name__ == "__main__":
    unittest.main()
