"""Test the nanohpc-hello login banner command with a fake snapshot, fake squeue, and fake login history."""

import json
import os
import pty
import re
import runpy
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/nanohpc/files/nanohpc-hello"
LOGIN = ROOT / "src/nanohpc/files/nanohpc-login.sh"
ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
UTF8 = {"LANG": "en_US.UTF-8", "TERM": "xterm-256color"}


def read_terminal(fd: int, output: bytes) -> bytes:
    """Read a pseudo-terminal until the program on it has exited."""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return output
        if not chunk:
            return output
        output += chunk


class NanohpcHelloTests(unittest.TestCase):
    """Animate only in a capable terminal; always end with the art and the user's summary."""

    def setUp(self) -> None:
        """Copy the command with its fixed paths pointed at fake data, as the front node would have it."""
        self.directory = Path(tempfile.mkdtemp(prefix="nanohpc-hello-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.directory)], check=True)
        self.user = subprocess.run(["id", "-un"], text=True, capture_output=True, check=True).stdout.strip()
        self.snapshot = self.directory / "status.json"
        self.snapshot.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "running_jobs": 5,
                    "average_wait_seconds_7d": 2640.0,
                    "maintenance": [],
                    "users": [
                        {"user": "someone", "gpu_hours_30d": 500.0},
                        {"user": self.user, "gpu_hours_30d": 438.6},
                        {"user": "idle", "gpu_hours_30d": 0.0},
                    ],
                }
            )
        )
        settings = self.directory / "login-banner.json"
        settings.write_text(json.dumps({"cluster_name": "demo", "website": "cluster.example.org/cluster/"}))
        last = self.directory / "last"
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        previous = (datetime.now(UTC) - timedelta(days=3)).astimezone().isoformat(timespec="seconds")
        last.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{self.user} pts/3 1.2.3.4 {now}   still logged in'\n"
            f"printf '%s\\n' '{self.user} pts/3 1.2.3.4 {previous} - {previous}  (00:04)'\n"
        )
        last.chmod(0o755)
        self.squeue = self.directory / "squeue"
        self.squeue.write_text("#!/bin/sh\nprintf 'RUNNING\\nRUNNING\\nPENDING\\n'\n")
        self.squeue.chmod(0o755)
        self.usage = self.directory / "usage"
        self.usage.mkdir()
        (self.usage / self.user).write_text("+----------+\n| BOX-LINE |\n+----------+\n")
        self.announcements = self.directory / "announcements"
        self.folders = self.directory / "folders"
        self.folders.mkdir()
        source = COMMAND.read_text()
        for fixed, fake in (
            ("/home/.nanohpc-home-folders", self.folders),
            ("/var/lib/nanohpc/monitor/status.json", self.snapshot),
            ("/etc/nanohpc/login-banner.json", settings),
            ("/var/lib/nanohpc-home-notices", self.usage),
            ("/var/lib/nanohpc/announcements", self.announcements),
            ('"last"', f'"{last}"'),
            ('"squeue"', f'"{self.squeue}"'),
        ):
            self.assertIn(str(fixed), source)
            source = source.replace(str(fixed), str(fake))
        self.script = self.directory / "nanohpc-hello"
        self.script.write_text(source)
        # Run through its own first line, as on the cluster.
        self.script.chmod(0o755)

    def plain(self) -> str:
        """Run with output to a pipe, not a terminal."""
        result = subprocess.run(
            [str(self.script)], text=True, capture_output=True, check=False, env=dict(os.environ, **UTF8)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def in_terminal(self, env: dict[str, str], interrupt: bool) -> tuple[str, float]:
        """Run the command on a pseudo-terminal; optionally press Ctrl+C once the animation has started."""
        start = time.monotonic()
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(str(self.script), [str(self.script)], env)
        output = b""
        if interrupt:
            while b"\x1b[?25l" not in output:
                output += os.read(fd, 65536)
            os.kill(pid, signal.SIGINT)
        output = read_terminal(fd, output)
        os.waitpid(pid, 0)
        return output.decode(), time.monotonic() - start

    def test_piped_output_is_plain_art_and_summary(self) -> None:
        """No terminal: no control codes, the art, then the user's jobs, hours, rank, home box, and links."""
        output = self.plain()
        self.assertNotIn("\x1b", output)
        self.assertIn("██╔══██╗██╔══██║██╔══██╗██╔══██╗ ██╔══██║██╔═══╝ ██║", output)
        expected = [
            f"Welcome back, {self.user}!",
            "You are running 2 jobs  |  Cluster is running 5 jobs  |  7d Waiting time: 44m",
            "GPU-hours (last 30 days): 439  |  Ranking: 🥈 2nd in demo",
            "BOX-LINE",
            "Cluster status: cluster.example.org/cluster/#overview",
            "Cluster guide: cluster.example.org/cluster/#docs | cluster.example.org/cluster/docs.md",
        ]
        lines = [line.strip() for line in output.splitlines()]
        positions = [
            next(index for index, line in enumerate(lines) if text == line or (text == "BOX-LINE" and text in line))
            for text in expected
        ]
        self.assertEqual(positions, sorted(positions), "summary order")
        self.assertTrue(output.endswith("cluster.example.org/cluster/docs.md\n\n"), "a blank line below the links")
        for removed in ("Impact", "Cancers", "backup", "Disk usage"):
            self.assertNotIn(removed, output)
        raw = output.splitlines()
        art = [index for index, line in enumerate(raw) if "█" in line]
        self.assertLess(art[0], next(index for index, line in enumerate(raw) if "Welcome back" in line), "art first")
        # The art has the same 2-space left margin as the text below it.
        self.assertTrue(all(raw[index].startswith("  ") for index in art), art)
        self.assertTrue(next(line for line in raw if "Welcome back" in line).startswith("  Welcome"))

    def test_announcements_box_below_the_links_only_when_there_is_one(self) -> None:
        """Maintenance reservations, messages for everyone, and the user's own messages, in a box after the links."""
        self.assertNotIn("Announcements", self.plain(), "no box without announcements")
        today = datetime.now().date()  # noqa: DTZ005 (the banner reads the local day)

        def day(offset: int) -> str:
            return (today + timedelta(days=offset)).isoformat()

        (self.announcements / "users").mkdir(parents=True)
        (self.announcements / "everyone.json").write_text(
            json.dumps(
                [
                    {"message": "New CUDA 13 toolkit installed", "show_from": day(-1), "until": day(0)},
                    {"message": "OLD MESSAGE", "show_from": day(-5), "until": day(-1)},
                    {"message": "FUTURE MESSAGE", "show_from": day(1), "until": day(3)},
                ]
            )
        )
        (self.announcements / "users" / f"{self.user}.json").write_text(
            json.dumps([{"message": "Please move your datasets to /scratch", "show_from": day(0), "until": day(2)}])
        )
        (self.announcements / "users" / "someone-else.json").write_text(
            json.dumps([{"message": "NOT YOURS", "show_from": day(0), "until": day(2)}])
        )
        start = datetime(2026, 10, 18, 8, 0).timestamp()  # noqa: DTZ001 (shown in local time)
        data = json.loads(self.snapshot.read_text())
        data["maintenance"] = [
            {"start": start, "end": start + 2 * 86400, "nodes": ["gpu1", "gpu2"], "comment": "Moving racks"}
        ]
        self.snapshot.write_text(json.dumps(data))
        output = self.plain()
        lines = [line.strip() for line in output.splitlines()]
        links = next(index for index, line in enumerate(lines) if line.startswith("Cluster guide"))
        box = next(index for index, line in enumerate(lines) if "Announcements" in line)
        self.assertGreater(box, links, "below the links")
        body = "\n".join(lines[box:])
        for text in (
            "From Sun 18 Oct 08:00: gpu1, gpu2",
            "Moving racks",
            "New CUDA 13 toolkit installed",
            "Please move your datasets to /scratch",
        ):
            self.assertIn(text, body)
        for text in ("OLD MESSAGE", "FUTURE MESSAGE", "NOT YOURS"):
            self.assertNotIn(text, output)
        # Only the start: the end is not when the machine is back.
        self.assertNotIn("Tue 20 Oct", output)
        self.assertTrue(lines[box].startswith("┌") and any(line.startswith("└") for line in lines[box:]), "a box")
        colored, _ = self.in_terminal(dict(os.environ, COLUMNS="100", **UTF8), False)
        self.assertIn("\x1b[38;5;221m┌─ Announcements", colored, "warm yellow in a terminal")

    def test_animates_in_a_utf8_terminal_and_restores_the_cursor(self) -> None:
        """A capable terminal: hidden cursor during the animation, shown at the end, within four seconds."""
        output, seconds = self.in_terminal(dict(os.environ, **UTF8), False)
        self.assertIn("\x1b[?25l", output)
        self.assertLess(output.index("\x1b[?25l"), output.rindex("\x1b[?25h"))
        self.assertIn("╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝ ╚════╝", ESCAPE.sub("", output))
        # Every animation frame draws the art 2 spaces in, like the text below.
        self.assertNotRegex(output, r"\r\x1b\[1;38;5;", "art drawn at column 0")
        self.assertIn("\r  \x1b[1;38;5;", output)
        # Every value, with its unit, is in the palette's soft, non-bold tone.
        soft = r"\x1b\[38;5;(?:210|183|114)m"
        for value in ("2 jobs", "5 jobs", "44m", "439", "2nd"):
            self.assertRegex(output, soft + re.escape(value) + r"\x1b\[0m", value)
        self.assertIn("🥈", output)
        self.assertRegex(output, r"Welcome back, \x1b\[1;38;5;\d+m", "name in the art's colors")
        box = next(line for line in output.splitlines() if "BOX-LINE" in line)
        self.assertRegex(box, r"^ *\x1b\[38;5;(216|189|28|174)m", "calm, non-bold box tone")
        self.assertIn("\x1b[4mCluster status\x1b[24m:", output)
        self.assertIn("\x1b[4mCluster guide\x1b[24m:", output)
        # Reveal 4 columns per frame, then a 16-frame color wave, then the final frame.
        width = max(len(line) for line in runpy.run_path(str(COMMAND))["ART"])
        self.assertEqual(output.count("\x1b[6A"), len(range(0, width + 4, 4)) + 16 + 1)
        self.assertGreater(seconds, 1.8, "slowed down")
        self.assertLess(seconds, 4.0)

    def test_slow_cluster_reads_run_during_the_animation(self) -> None:
        """squeue and the snapshot are read while the art animates, so the lines below follow without a pause."""
        self.squeue.write_text("#!/bin/sh\nsleep 1.5\nprintf 'RUNNING\\nRUNNING\\nPENDING\\n'\n")
        output, seconds = self.in_terminal(dict(os.environ, **UTF8), False)
        self.assertRegex(output, r"\x1b\[38;5;(?:210|183|114)m2 jobs\x1b\[0m")
        self.assertLess(seconds, 2.8, "the 1.5 s read waited for the animation to end")

    def test_ascii_art_without_utf8(self) -> None:
        """A terminal without UTF-8 gets the plain ASCII art, no animation, and no medal."""
        env = {key: value for key, value in os.environ.items() if not key.startswith("LC_")}
        output, _ = self.in_terminal(dict(env, LANG="C", TERM="xterm-256color"), False)
        self.assertNotIn("█", output)
        self.assertNotIn("\x1b[?25l", output)
        self.assertIn("| '_ \\ / _` | '_ \\ / _ \\  | |_| |", ESCAPE.sub("", output))
        self.assertNotIn("🥈", output)

    def test_ctrl_c_restores_the_cursor(self) -> None:
        """Interrupting the animation shows the cursor again and resets colors."""
        output, _ = self.in_terminal(dict(os.environ, **UTF8), True)
        self.assertIn("\x1b[0m\x1b[?25h", output[output.index("\x1b[?25l") :])

    def test_stopped_by_the_time_cap_restores_the_cursor(self) -> None:
        """The login's 10-second cap stops it with SIGTERM; the cursor and colors come back."""
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(str(self.script), [str(self.script)], dict(os.environ, **UTF8))
        output = b""
        while b"\x1b[?25l" not in output:
            output += os.read(fd, 65536)
        os.kill(pid, signal.SIGTERM)
        text = read_terminal(fd, output).decode()
        os.waitpid(pid, 0)
        self.assertIn("\x1b[0m\x1b[?25h", text[text.index("\x1b[?25l") :])

    def test_without_squeue_jobs_are_unknown(self) -> None:
        """No squeue: the summary says jobs are unknown instead of failing."""
        self.squeue.unlink()
        self.assertIn("You are running unknown jobs", self.plain())

    def test_stale_snapshot_shows_unknown_values(self) -> None:
        """A snapshot more than an hour old is not used."""
        data = json.loads(self.snapshot.read_text())
        data["generated_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        self.snapshot.write_text(json.dumps(data))
        output = self.plain()
        self.assertIn("Cluster is running unknown jobs  |  7d Waiting time: unknown", output)
        self.assertIn("GPU-hours (last 30 days): unknown", output)

    def test_disk_usage_when_not_measured_or_not_on_the_front_node(self) -> None:
        """No box yet says when it is measured; no box folder points to the front node."""
        (self.usage / self.user).unlink()
        self.assertIn("Disk usage: not measured yet; it is measured every 5 minutes.", self.plain())
        self.usage.rmdir()
        self.assertIn("Disk usage: run nanohpc-hello on the front node to see it.", self.plain())

    def test_largest_folders_inside_the_usage_box(self) -> None:
        """The home machine's list of largest folders is shown inside the usage box, with the box redrawn
        around it; before the first measurement the box says when it is measured."""
        (self.folders / self.user).write_text(
            "Largest folders (2026-10-29 14:05):\n  42.0 GB  ~/project  (git repo)\n"
            "  12.5 GB  ~/project/.worktrees (2 worktrees)\n"
        )
        lines = [line.strip() for line in self.plain().splitlines()]
        start = lines.index(next(line for line in lines if "BOX-LINE" in line)) - 1
        box = lines[start : start + 7]
        self.assertEqual(
            [line.strip("|").strip() if line.startswith("|") else line for line in box],
            [
                "+" + "-" * 47 + "+",
                "BOX-LINE",
                "",
                "Largest folders (2026-10-29 14:05):",
                "42.0 GB  ~/project  (git repo)",
                "12.5 GB  ~/project/.worktrees (2 worktrees)",
                "+" + "-" * 47 + "+",
            ],
        )
        self.assertEqual(len({len(line) for line in box}), 1, box)
        (self.folders / self.user).unlink()
        self.assertIn("Largest folders: not measured yet; they are measured every hour.", self.plain())

    def test_largest_folders_file_cannot_send_control_codes(self) -> None:
        """The user owns the list file: only its printable text reaches the terminal."""
        (self.folders / self.user).write_text("Largest folders:\n\x1b]0;title\x07  1.0 GB  ~/a\x1b[2J\x08b\r\n")
        output = self.plain()
        for code in ("\x1b", "\x07", "\x08", "\r"):
            self.assertNotIn(code, output)
        self.assertIn("]0;title  1.0 GB  ~/ab", output)

    def test_mac_locale_and_unknown_width_still_animate(self) -> None:
        """A Mac sends LC_CTYPE=UTF-8 without LANG; that is a UTF-8 terminal and must animate."""
        env = {key: value for key, value in os.environ.items() if not key.startswith(("LC_", "LANG"))}
        output, _ = self.in_terminal(dict(env, LC_CTYPE="UTF-8", TERM="xterm-256color"), False)
        self.assertIn("\x1b[?25l", output)
        self.assertEqual(runpy.run_path(str(COMMAND))["columns"](0), 80)


class NanohpcHelloPartsTests(unittest.TestCase):
    """The small rules the banner is built from."""

    def setUp(self) -> None:
        self.command: dict[str, Any] = runpy.run_path(str(COMMAND))

    def test_greeting_by_time_since_the_previous_login(self) -> None:
        """Each bracket of time since the previous login has its own greeting; the first one names the cluster."""
        hour, day = 3600, 86400
        cases = [
            (None, "Welcome to demo, {}!"),
            (10 * 60, "Hello again, {}!"),
            (5 * hour, "Hello {}!"),
            (3 * day, "Welcome back, {}!"),
            (10 * day, "Long time no see, {}!"),
            (40 * day, "Look who's back! Hello {}!"),
        ]
        for seconds, expected in cases:
            self.assertEqual(self.command["greeting"](seconds, "demo"), expected)

    def test_colors_follow_the_time_of_day(self) -> None:
        """Warm 07:00-18:00, soft 18:00-24:00, matrix greens 00:00-07:00."""
        palette = self.command["palette"]
        warm, soft, matrix = palette(12), palette(20), palette(3)
        self.assertEqual(warm, [209, 215, 216, 221, 220])
        self.assertEqual(matrix, [22, 28, 34, 40, 46])
        self.assertEqual(len({tuple(warm), tuple(soft), tuple(matrix)}), 3)
        self.assertEqual(
            (palette(6), palette(7), palette(17), palette(18), palette(23)), (matrix, warm, warm, soft, soft)
        )

    def test_box_tone_value_tone_and_medals(self) -> None:
        """Light salmon box by day, soft red when jobs wait; soft value tones; medals for the top three."""
        command = self.command
        self.assertEqual(command["box_tone"](command["WARM"], False), 216)
        self.assertEqual(command["box_tone"](command["WARM"], True), 174)
        self.assertEqual([command["value_tone"](command[name]) for name in ("WARM", "SOFT", "MATRIX")], [210, 183, 114])
        self.assertEqual([command["medal"](rank) for rank in (1, 2, 3, 4)], ["🥇", "🥈", "🥉", ""])

    def test_waiting_time_format_and_job_counts(self) -> None:
        """Waiting time is short: seconds, minutes, or hours with one decimal; one job is singular."""
        command = self.command
        self.assertEqual(
            [command["waiting"](value) for value in (None, 30, 2640, 3900, 4620, 93600)],
            ["unknown", "30s", "44m", "1.1h", "1.3h", "26.0h"],
        )
        self.assertEqual([command["jobs"](value) for value in (1, 2, "unknown")], ["1 job", "2 jobs", "unknown jobs"])
        # A tiny amount above 0 must not read as 0.0 next to a ranking.
        self.assertEqual([command["hours"](value) for value in (0.0, 0.0044, 0.5, 12.4)], ["0.0", "<0.1", "0.5", "12"])

    def test_art_rows_line_up(self) -> None:
        """Both arts are rectangles of 6 and 5 rows, with nothing past the right edge of the widest row."""
        self.assertEqual(len(self.command["ART"]), 6)
        self.assertEqual(len(self.command["ASCII_ART"]), 5)
        self.assertLessEqual(max(len(line) for line in self.command["ART"]) + 2, 80)


class LoginBannerRoleTests(unittest.TestCase):
    """Deploy installs the banner on the front node in Slurm mode only, with private personal messages."""

    def test_front_node_role(self) -> None:
        import yaml

        ansible = ROOT / "src/nanohpc/ansible"
        plays = yaml.safe_load((ansible / "site.yml").read_text())
        self.assertIn(("role_front", ["login_banner"]), [(play.get("hosts"), play.get("roles")) for play in plays])
        self.assertNotIn("login_banner", (ansible / "monitor.yml").read_text())
        tasks = (ansible / "roles/login_banner/tasks/main.yml").read_text()
        for text in (
            "/usr/local/bin/nanohpc-hello",
            "/etc/nanohpc/login-banner.json",
            "/etc/profile.d/nanohpc-login.sh",
            "/etc/zsh/zprofile",
            "/var/lib/nanohpc/announcements/everyone.json",
            "/var/lib/nanohpc/announcements/users/",
            'mode: "0400"',
            "/etc/update-motd.d/",
            "/etc/update-motd.d/50-landscape-sysinfo",
            "/etc/bash.bashrc",
            "# sudo hint",
            "/etc/legal",
        ):
            self.assertIn(text, tasks)
        # The plain usage box at login is replaced by the banner, which shows it.
        self.assertNotIn("nanohpc-home-notice.sh", (ansible / "roles/monitoring/tasks/main.yml").read_text())
        self.assertFalse((ROOT / "src/nanohpc/files/nanohpc-home-notice.sh").exists())


class LoginScriptTests(unittest.TestCase):
    """The login script runs the banner only in a terminal and never stops a login."""

    def test_runs_only_in_a_terminal_and_always_continues(self) -> None:
        """sh, bash, zsh: in a terminal it runs nanohpc-hello with the 10-second cap; piped it does nothing."""
        self.assertIn("timeout --foreground 10 nanohpc-hello", LOGIN.read_text())
        with tempfile.TemporaryDirectory(prefix="nanohpc-login-") as directory:
            fake = Path(directory) / "nanohpc-hello"
            fake.write_text("#!/bin/sh\necho FAKE-BANNER\nexit 3\n")
            fake.chmod(0o755)
            # A stand-in for GNU timeout, which macOS lacks: drop its options and run the command.
            timeout = Path(directory) / "timeout"
            timeout.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
            timeout.chmod(0o755)
            env = dict(os.environ, PATH=f"{directory}{os.pathsep}{os.environ['PATH']}")
            source = f'. "{LOGIN}"; echo AFTER-LOGIN-SCRIPT'
            for shell in [name for name in ("sh", "bash", "zsh") if shutil.which(name)]:
                with self.subTest(shell=shell):
                    pid, fd = pty.fork()
                    if pid == 0:
                        os.execvpe(shell, [shell, "-c", source], env)
                    shown = read_terminal(fd, b"").decode()
                    os.waitpid(pid, 0)
                    self.assertIn("FAKE-BANNER", shown)
                    self.assertIn("AFTER-LOGIN-SCRIPT", shown)
                    piped = subprocess.run([shell, "-c", source], text=True, capture_output=True, env=env, check=False)
                    self.assertEqual((piped.returncode, piped.stdout), (0, "AFTER-LOGIN-SCRIPT\n"))


if __name__ == "__main__":
    unittest.main()
