"""Run cluster-user-check against a fake machine.

Agreed 2026-10-05: tunnels started by normal users are stopped (the job keeps running) and reported once on
monitoring; admins' tunnels are only
reported; root's and system accounts' programs are never touched; an allow list lets admins permit one.
The fake /proc points at real sleeping processes, so stopping is tested for real.
"""

import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "src/nanohpc/files/cluster-user-check"
PASSWD = "root:x:0:0::/root:/bin/bash\nsyslog:x:104:110::/home/syslog:/usr/sbin/nologin\nelias:x:1004:1004::/home/elias:/bin/bash\ntest-user1:x:2000:2000::/home/test-user1:/bin/bash\nnoah:x:1101:1101::/home/noah:/bin/bash\nslurm:x:64030:64030::/nonexistent:/usr/sbin/nologin\n"


class FakeMachine:
    """A temporary root with /etc/passwd and a /proc whose entries are real sleeping processes."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="user-check-"))
        (self.root / "etc").mkdir()
        (self.root / "etc/passwd").write_text(PASSWD)
        self.sleepers: dict[str, subprocess.Popen] = {}

    def process(self, label: str, uid: int, exe: str, argv: list[str], cgroup: str) -> None:
        """Start a sleeping process and describe it in the fake /proc under its real PID."""
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.sleepers[label] = sleeper
        entry = self.root / "proc" / str(sleeper.pid)
        entry.mkdir(parents=True)
        (entry / "status").write_text(f"Name:\t{Path(exe).name[:15]}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        (entry / "cgroup").write_text(f"0::{cgroup}\n")
        (entry / "stat").write_text(f"{sleeper.pid} (x) S " + " ".join(["0"] * 18) + " 4242 0 0\n")
        os.symlink(exe, entry / "exe")

    def listen(self, label: str, port: int, inode: int) -> None:
        """Give the process a listening TCP socket in the fake /proc/net/tcp."""
        entry = self.root / "proc" / str(self.sleepers[label].pid)
        (entry / "fd").mkdir(exist_ok=True)
        os.symlink(f"socket:[{inode}]", entry / "fd" / str(inode))
        table = self.root / "proc/net/tcp"
        table.parent.mkdir(exist_ok=True)
        if not table.exists():
            table.write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            )
        with table.open("a") as lines:
            lines.write(
                f"   0: 00000000:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {inode} 1\n"
            )

    def alive(self, label: str) -> bool:
        """Whether the process is still running."""
        return self.sleepers[label].poll() is None

    def forget_stopped(self) -> None:
        """Remove stopped processes from the fake /proc, as the kernel does."""
        for sleeper in self.sleepers.values():
            if sleeper.poll() is not None and (self.root / "proc" / str(sleeper.pid)).exists():
                shutil.rmtree(self.root / "proc" / str(sleeper.pid))

    def cleanup(self) -> None:
        for sleeper in self.sleepers.values():
            sleeper.kill()
            sleeper.wait()
        shutil.rmtree(self.root)


def run_check(
    machine: FakeMachine, output: Path, state: Path, now: str, role: str = "compute", disk_percent: str = "101"
) -> subprocess.CompletedProcess:
    """Run the check as the deployed service does, against the fake machine (disk check off unless asked)."""
    return subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--root",
            str(machine.root),
            "--machine",
            "vapor",
            "--role",
            role,
            "--admin",
            "elias",
            "--admin",
            "djordje",
            "--allow",
            "noah:ngrok",
            "--disk",
            str(machine.root),
            "--disk-percent",
            disk_percent,
            "--output",
            str(output),
            "--state",
            str(state),
            "--now",
            now,
            "--keep-minutes",
            "15",
        ],
        text=True,
        capture_output=True,
        check=True,
    )


def findings(output: Path) -> list[str]:
    """The published finding lines."""
    return [line for line in output.read_text().splitlines() if line.startswith("cluster_user_check_finding{")]


class UserCheckTest(unittest.TestCase):
    """What counts as a tunnel, who is stopped, and what is published for Prometheus."""

    def setUp(self) -> None:
        self.machine = FakeMachine()
        self.addCleanup(self.machine.cleanup)
        self.out = self.machine.root / "user-check.prom"
        self.state = self.machine.root / "state.json"
        job = "/system.slice/slurmstepd.scope/job_660/step_0/user/task_0"
        login = "/user.slice/user-2000.slice/session-5.scope"
        add = self.machine.process
        add("cloudflared", 2000, "/home/test-user1/bin/cloudflared", ["cloudflared", "tunnel", "run"], job)
        add(
            "code tunnel",
            2000,
            "/home/test-user1/.local/bin/code",
            ["/home/test-user1/.local/bin/code", "tunnel", "--name", "x"],
            job,
        )
        add("ssh -NfR", 2000, "/usr/bin/ssh", ["ssh", "-NfR", "8080:localhost:80", "me@example.org"], login)
        add(
            "ssh RemoteForward",
            2000,
            "/usr/bin/ssh",
            ["ssh", "-o", "RemoteForward=9000 localhost:9000", "me@example.org"],
            login,
        )
        add("ssh -w", 2000, "/usr/bin/ssh", ["ssh", "-w", "0:0", "me@example.org"], login)
        add("autossh", 2000, "/usr/bin/autossh", ["autossh", "-M", "0", "me@example.org"], login)
        add("code serve-web", 2000, "/home/test-user1/.local/bin/code", ["code", "serve-web", "--port", "8000"], job)
        add("ssh -L", 2000, "/usr/bin/ssh", ["ssh", "-L", "8888:disco:8888", "-i", "key", "real"], login)
        add("ssh command -R", 2000, "/usr/bin/ssh", ["ssh", "me@example.org", "grep", "-R", "x"], login)
        add("admin tailscaled", 1004, "/usr/sbin/tailscaled", ["tailscaled", "--tun=userspace-networking"], login)
        add("root tailscaled", 0, "/usr/sbin/tailscaled", ["/usr/sbin/tailscaled"], "/system.slice/tailscaled.service")
        add("system cloudflared", 104, "/usr/bin/cloudflared", ["cloudflared"], "/system.slice/cloudflared.service")
        add("allowed ngrok", 1101, "/home/noah/ngrok", ["./ngrok", "tcp", "5432"], job)

    def test_users_tunnels_are_stopped_and_others_left_alone(self) -> None:
        """Normal users' tunnels stop; serve-web, ssh -L, admins, root, system accounts, and allowed ones stay."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        time.sleep(0.5)
        for label in ("cloudflared", "code tunnel", "ssh -NfR", "ssh RemoteForward", "ssh -w", "autossh"):
            self.assertFalse(self.machine.alive(label), label)
        for label in (
            "code serve-web",
            "ssh -L",
            "ssh command -R",
            "admin tailscaled",
            "root tailscaled",
            "system cloudflared",
            "allowed ngrok",
        ):
            self.assertTrue(self.machine.alive(label), label)

    def test_vanished_tunnel_is_not_reported_as_stopped(self) -> None:
        """A tunnel that ended or changed PID before the kill does not create a false stopped notice."""
        main = runpy.run_path(str(CHECK))["main"]
        arguments = [
            "cluster-user-check",
            "--root",
            str(self.machine.root),
            "--machine",
            "vapor",
            "--role",
            "front",
            "--admin",
            "elias",
            "--admin",
            "djordje",
            "--disk",
            str(self.machine.root),
            "--disk-percent",
            "101",
            "--output",
            str(self.out),
            "--state",
            str(self.state),
            "--now",
            "2026-10-05T16:00:00+02:00",
            "--keep-minutes",
            "15",
        ]
        with patch.object(sys, "argv", arguments), patch.dict(main.__globals__, {"stop": lambda root, process: False}):
            main()
        self.assertNotIn('action="stopped"', self.out.read_text())

    def test_findings_are_published_with_who_what_where_and_the_action(self) -> None:
        """One line per finding, with the user, program, job, machine, and stopped or reported."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        lines = [line for line in findings(self.out) if 'kind="tunnel"' in line]
        self.assertEqual(len(lines), 7)
        stopped = next(line for line in lines if 'program="cloudflared"' in line)
        # job and machine are Prometheus's own labels (it renamed ours to exported_job, 2026-10-05).
        for label in ('kind="tunnel"', 'user="test-user1"', 'slurm_job="660"', 'host="vapor"', 'action="stopped"'):
            self.assertIn(label, stopped)
        self.assertTrue(stopped.endswith(" 1791208800"))
        admin = next(line for line in lines if 'user="elias"' in line)
        self.assertIn('action="reported"', admin)
        self.assertIn('slurm_job=""', admin)
        self.assertNotIn(" job=", admin.replace(",", " "))
        self.assertIn('program="ssh -R"', next(line for line in lines if '"ssh' in line and "-R" in line))

    def test_state_from_the_first_version_without_detail_is_read(self) -> None:
        """A stopped tunnel saved by the first version (no detail field) is still published (real, 2026-10-05)."""
        self.state.write_text(
            json.dumps(
                {
                    "real-1-2": {
                        "kind": "tunnel",
                        "user": "test-user1",
                        "program": "cloudflared",
                        "job": "",
                        "machine": "real",
                        "action": "stopped",
                        "found": 1791208800,
                    }
                }
            )
        )
        run_check(self.machine, self.out, self.state, "2026-10-05T16:05:00+02:00")
        self.assertIn('id="real-1-2"', self.out.read_text())

    def test_findings_are_kept_for_a_while_then_dropped(self) -> None:
        """A stopped tunnel stays published for --keep-minutes so Slack sees it, and is not counted twice."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        time.sleep(0.5)
        self.machine.forget_stopped()
        run_check(self.machine, self.out, self.state, "2026-10-05T16:10:00+02:00")
        lines = [line for line in findings(self.out) if 'kind="tunnel"' in line]
        self.assertEqual(len(lines), 7)
        self.assertTrue(all(line.endswith(" 1791208800") for line in lines))
        run_check(self.machine, self.out, self.state, "2026-10-05T16:20:00+02:00")
        lines = [line for line in findings(self.out) if 'kind="tunnel"' in line]
        self.assertEqual([line for line in lines if 'action="stopped"' in line], [])
        self.assertEqual(len(lines), 1)  # the admin's tunnel still runs, so it is still published

    def test_front_code_serve_web_is_reported_as_vscode_server(self) -> None:
        """The front node reports browser VS Code sessions, while the same command inside a job is allowed."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role="front")
        self.assertTrue(
            any('kind="vscode-server"' in line and 'user="test-user1"' in line for line in findings(self.out))
        )
        self.assertTrue(self.machine.alive("code serve-web"))


class LastingFindingsTest(unittest.TestCase):
    """Things users may not do that last: published while they last, admins only for VS Code on the front node."""

    def setUp(self) -> None:
        self.machine = FakeMachine()
        self.addCleanup(self.machine.cleanup)
        self.out = self.machine.root / "user-check.prom"
        self.state = self.machine.root / "state.json"
        job = "/system.slice/slurmstepd.scope/job_660/step_0/user/task_0"
        add = self.machine.process
        add("job python", 2000, "/usr/bin/python3", ["python3", "train.py"], job)
        add("left python", 2000, "/usr/bin/python3", ["python3", "serve.py"], "/system.slice/cron.service")
        add("admin shell", 1004, "/usr/bin/bash", ["bash"], "/user.slice/user-1004.slice/session-9.scope")
        add(
            "noah vscode",
            1101,
            "/home/noah/.vscode-server/bin/abc/node",
            ["/home/noah/.vscode-server/bin/abc/node", "server-main.js"],
            "/user.slice/user-1101.slice/session-3.scope",
        )
        add(
            "elias vscode",
            1004,
            "/home/elias/.vscode-server/bin/abc/node",
            ["/home/elias/.vscode-server/bin/abc/node", "server-main.js"],
            "/user.slice/user-1004.slice/session-9.scope",
        )
        self.machine.listen("job python", 8888, 111)
        self.machine.listen("left python", 6006, 222)
        self.machine.listen("admin shell", 9999, 333)
        add("slurmctld", 64030, "/usr/sbin/slurmctld", ["/usr/sbin/slurmctld"], "/system.slice/slurmctld.service")
        self.machine.listen("slurmctld", 6817, 444)
        add("slurm cloudflared", 64030, "/usr/bin/cloudflared", ["cloudflared"], "/system.slice/x.service")
        for folder in (
            "var/spool/cron/crontabs",
            "var/lib/systemd/linger",
            "home/test-user1/.config/systemd/user/default.target.wants",
            "tmp",
            "var/tmp",
        ):
            (self.machine.root / folder).mkdir(parents=True)
        (self.machine.root / "var/spool/cron/crontabs/test-user1").write_text("* * * * * true\n")
        (self.machine.root / "var/spool/cron/crontabs/elias").write_text("* * * * * true\n")
        (self.machine.root / "var/lib/systemd/linger/test-user1").write_text("")
        (self.machine.root / "home/test-user1/.config/systemd/user/default.target.wants/sync.service").write_text("")
        (self.machine.root / "tmp/big").write_bytes(b"x" * 2_000_000)

    def lines(self) -> list[str]:
        return findings(self.out)

    def test_compute_machine_reports_processes_outside_jobs_and_all_user_ports(self) -> None:
        """The leftover process is outside a job; both job and leftover listeners are reported."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        outside = [line for line in self.lines() if 'kind="outside-job"' in line]
        self.assertEqual(len(outside), 2)  # test-user1's leftover, and noah's VS Code server outside a job
        mine = next(line for line in outside if 'user="test-user1"' in line)
        self.assertIn('action="lasting"', mine)
        self.assertIn("python3", mine)
        ports = [line for line in self.lines() if 'kind="port"' in line]
        self.assertEqual(len(ports), 1)
        self.assertIn('user="test-user1"', ports[0])
        self.assertIn("6006", ports[0])
        self.assertIn("8888", ports[0])
        self.assertNotIn("9999", ports[0])
        self.assertTrue(self.machine.alive("left python"), "lasting findings are only reported")

    def test_outside_job_detail_does_not_publish_arguments_or_slack_mentions(self) -> None:
        """Arguments can include passwords, tokens, and text that Slack would interpret as a mention."""
        self.machine.process(
            "secret command",
            2000,
            "/usr/bin/python3",
            ["python3", "serve.py", "--token=TOPSECRET", "<@U999>"],
            "/system.slice/cron.service",
        )
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        finding = next(line for line in self.lines() if 'kind="outside-job"' in line and 'user="test-user1"' in line)
        self.assertIn("python3", finding)
        self.assertNotIn("TOPSECRET", finding)
        self.assertNotIn("<@U999>", finding)
        self.assertNotIn("serve.py", finding)

    def test_user_manager_started_for_a_job_is_not_outside_a_job(self) -> None:
        """Slurm's `su - <user>` for `--export=ALL,VAR=value` starts the user manager for about 10 s (eleni on dada, 2026-10-07)."""
        manager = "/user.slice/user-2000.slice/user@2000.service"
        add = self.machine.process
        add(
            "user manager",
            2000,
            "/usr/lib/systemd/systemd",
            ["/lib/systemd/systemd", "--user"],
            f"{manager}/init.scope",
        )
        add("sd-pam", 2000, "/usr/lib/systemd/systemd", ["(sd-pam)"], f"{manager}/init.scope")
        add(
            "dbus",
            2000,
            "/usr/bin/dbus-daemon",
            ["/usr/bin/dbus-daemon", "--session"],
            f"{manager}/app.slice/dbus.service",
        )
        run_check(self.machine, self.out, self.state, "2026-10-07T10:28:40+02:00")
        mine = next(line for line in self.lines() if 'kind="outside-job"' in line and 'user="test-user1"' in line)
        self.assertIn('detail="1 process: python3"', mine)

    def test_user_service_descendant_is_not_skipped_as_a_transient_manager(self) -> None:
        """A lasting service inside user@UID.service is still outside a Slurm job."""
        self.machine.process(
            "user service",
            2000,
            "/usr/bin/python3",
            ["python3", "daemon.py"],
            "/user.slice/user-2000.slice/user@2000.service/app.slice/daemon.service",
        )
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        finding = next(line for line in self.lines() if 'kind="outside-job"' in line and 'user="test-user1"' in line)
        self.assertIn("2 processes", finding)

    def test_service_accounts_without_a_login_shell_are_never_counted(self) -> None:
        """slurm (UID 64030) and munge have UIDs above 1000 but no login shell: never reported or stopped (seen 2026-10-05)."""
        for role in ("compute", "front"):
            run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role=role)
            self.assertEqual([line for line in self.lines() if 'user="slurm"' in line], [], role)
        self.assertTrue(self.machine.alive("slurm cloudflared"))

    def test_systemd_temporary_service_accounts_are_never_counted(self) -> None:
        """The nightly route test's listener runs under DynamicUser as UID 61389, not in /etc/passwd (posted 2026-10-08)."""
        add = self.machine.process
        add(
            "route listener",
            61389,
            "/usr/bin/iperf3",
            ["/usr/bin/iperf3", "-s", "--bind", "130.226.140.64"],
            "/system.slice/cluster-route-listen.service",
        )
        self.machine.listen("route listener", 5201, 555)
        for role in ("compute", "front"):
            run_check(self.machine, self.out, self.state, "2026-10-08T01:33:00+02:00", role=role)
            self.assertEqual([line for line in self.lines() if 'user="61389"' in line], [], role)

    def test_unknown_uid_is_not_stopped_or_reported(self) -> None:
        """A process without a configured login account must not be killed as a user tunnel."""
        self.machine.process(
            "unknown cloudflared",
            61000,
            "/usr/bin/cloudflared",
            ["cloudflared", "tunnel"],
            "/system.slice/unknown.service",
        )
        run_check(self.machine, self.out, self.state, "2026-10-08T01:33:00+02:00")
        self.assertTrue(self.machine.alive("unknown cloudflared"))
        self.assertFalse(any('user="61000"' in line for line in self.lines()))

    def test_schedules_of_users_are_reported_but_not_of_admins(self) -> None:
        """A crontab, lingering, or an enabled user service of a normal user; an admin's crontab is fine."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role="front")
        schedule = [line for line in self.lines() if 'kind="schedule"' in line]
        self.assertEqual(len(schedule), 1)
        for word in ('user="test-user1"', "crontab", "lingering", "sync.service"):
            self.assertIn(word, schedule[0])

    def test_plain_user_unit_file_is_reported_on_compute_machine(self) -> None:
        """A unit file need not be enabled, and home is shared with compute nodes."""
        (self.machine.root / "home/test-user1/.config/systemd/user/background.service").write_text("[Service]\n")
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role="compute")
        finding = next(line for line in self.lines() if 'kind="schedule"' in line and 'user="test-user1"' in line)
        self.assertIn("background.service", finding)

    def test_at_jobs_are_reported_for_their_owner(self) -> None:
        """An at job is identified by the spool file's owner, including when its name is opaque."""
        spool = self.machine.root / "var/spool/cron/atjobs"
        spool.mkdir(parents=True)
        (spool / "a0000101a86f3a").write_text("#!/bin/sh\ntrue\n")
        schedules = runpy.run_path(str(CHECK))["schedules"]
        with patch("os.lstat", return_value=Mock(st_uid=2000)):
            found = schedules(self.machine.root, ["elias"])
        self.assertIn("at job", found["test-user1"])

    def test_system_account_schedules_are_not_reported(self) -> None:
        """A service account with no login shell is not a normal user, even with cron or linger files."""
        (self.machine.root / "var/spool/cron/crontabs/slurm").write_text("* * * * * true\n")
        (self.machine.root / "var/lib/systemd/linger/slurm").write_text("")
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role="front")
        self.assertEqual([line for line in self.lines() if 'kind="schedule"' in line and 'user="slurm"' in line], [])

    def test_front_node_reports_vscode_servers_of_everyone_and_not_processes_outside_jobs(self) -> None:
        """On the front node users work outside jobs; a VS Code server is reported, admins included."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role="front")
        self.assertEqual([line for line in self.lines() if 'kind="outside-job"' in line], [])
        vscode = sorted(line for line in self.lines() if 'kind="vscode-server"' in line)
        self.assertEqual(len(vscode), 2)
        self.assertIn('user="elias"', vscode[0])
        self.assertIn('user="noah"', vscode[1])
        ports = [line for line in self.lines() if 'kind="port"' in line]
        self.assertEqual(len(ports), 1, "test-user1's 6006 and 8888; the admin's 9999 is not reported")
        self.assertIn("8888", ports[0])

    def test_other_machine_reports_user_ports_without_compute_job_rule(self) -> None:
        """Storage and backup nodes have no Slurm jobs, but a user listener still needs a report."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", role="other")
        self.assertEqual([line for line in self.lines() if 'kind="outside-job"' in line], [])
        self.assertEqual(len([line for line in self.lines() if 'kind="port"' in line]), 1)

    def test_full_disk_names_the_largest_users_of_tmp(self) -> None:
        """At or above the threshold the system disk is reported with the largest owners in /tmp and /var/tmp."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00", disk_percent="0")
        disk = [line for line in self.lines() if 'kind="disk"' in line]
        self.assertEqual(len(disk), 1)
        self.assertIn('user=""', disk[0])
        self.assertIn("/tmp", disk[0])
        run_check(self.machine, self.out, self.state, "2026-10-05T16:01:00+02:00")
        self.assertEqual([line for line in self.lines() if 'kind="disk"' in line], [])

    def test_lasting_findings_keep_their_first_time_and_end_when_gone(self) -> None:
        """While it lasts a finding keeps its first time; once gone it is no longer published."""
        run_check(self.machine, self.out, self.state, "2026-10-05T16:00:00+02:00")
        run_check(self.machine, self.out, self.state, "2026-10-06T16:00:00+02:00")
        outside = next(line for line in self.lines() if 'kind="outside-job"' in line and 'user="test-user1"' in line)
        self.assertTrue(outside.endswith(" 1791208800"))
        self.machine.sleepers["left python"].kill()
        self.machine.sleepers["left python"].wait()
        self.machine.forget_stopped()
        run_check(self.machine, self.out, self.state, "2026-10-06T16:01:00+02:00")
        self.assertEqual(
            [line for line in self.lines() if 'user="test-user1"' in line and 'kind="outside-job"' in line], []
        )


class StateWriteTest(unittest.TestCase):
    """A failed state write keeps the previous complete state for the next run."""

    def test_interrupted_state_write_does_not_corrupt_previous_state(self) -> None:
        machine = FakeMachine()
        self.addCleanup(machine.cleanup)
        (machine.root / "proc").mkdir()
        state = machine.root / "state.json"
        previous = json.dumps(
            {
                "old-finding": {
                    "kind": "tunnel",
                    "user": "test-user1",
                    "program": "ngrok",
                    "job": "",
                    "machine": "vapor",
                    "action": "stopped",
                    "found": 1791208800,
                }
            }
        )
        state.write_text(previous)
        original_write = Path.write_text

        def interrupted_write(path: Path, value: str, *args: object, **kwargs: object) -> int:
            if path.name.startswith("state.json"):
                original_write(path, value[:8])
                raise OSError("simulated interrupted write")
            return original_write(path, value, *args, **kwargs)

        command = [
            str(CHECK),
            "--root",
            str(machine.root),
            "--machine",
            "vapor",
            "--role",
            "other",
            "--admin",
            "elias",
            "--disk",
            str(machine.root),
            "--disk-percent",
            "101",
            "--output",
            str(machine.root / "user-check.prom"),
            "--state",
            str(state),
            "--now",
            "2026-10-05T16:05:00+02:00",
            "--keep-minutes",
            "15",
        ]
        main = runpy.run_path(str(CHECK))["main"]
        with (
            patch.object(sys, "argv", command),
            patch.object(Path, "write_text", interrupted_write),
            self.assertRaisesRegex(OSError, "interrupted write"),
        ):
            main()
        self.assertEqual(state.read_text(), previous)


if __name__ == "__main__":
    unittest.main()
