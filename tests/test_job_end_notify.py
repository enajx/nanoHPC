"""Private Slack messages when a batch job that asked with --mail-user=slack ends.

Runs the real mail program (Slurm's MailProg) and the real sender through their command lines. Fakes: sacct is a
small script that prints records from a JSON file, and Slack's chat.postMessage is a local HTTP server.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIL = ROOT / "src/nanohpc/files/nanohpc-slack-mail"
NOTIFY = ROOT / "src/nanohpc/files/nanohpc-job-end-notify"


class FakeSlack:
    """Record private messages sent through chat.postMessage; answer ok or a refusal."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.ok = True
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/chat.postMessage" or self.headers["Authorization"] != "Bearer xoxb-test":
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.messages.append(body)
                ok = outer.ok and body["channel"] != "U_GONE"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "error": "channel_not_found"}).encode())

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class JobEndNotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="nanohpc-job-end-"))
        self.addCleanup(shutil.rmtree, self.folder)
        self.spool = self.folder / "spool"
        self.spool.mkdir()
        self.state = self.folder / "state.json"
        self.token = self.folder / "token"
        self.token.write_text("xoxb-test\n")
        self.slack = FakeSlack()
        self.addCleanup(self.slack.close)
        # Fake sacct: prints the record of the job given with -j, from records.json (none: nothing printed).
        self.records: dict[str, str] = {}
        self.sacct = self.folder / "sacct"
        self.sacct.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"records = json.load(open({str(self.folder / 'records.json')!r}))\n"
            "print(records.get(sys.argv[sys.argv.index('-j') + 1], ''))\n"
        )
        self.sacct.chmod(0o755)

    def mail(self, job: str, mail_type: str, recipient: str) -> subprocess.CompletedProcess[str]:
        """Call the mail program as Slurm does: -s SUBJECT RECIPIENT, with the job in the environment."""
        environment = {**os.environ, "SLURM_JOB_ID": job, "SLURM_JOB_MAIL_TYPE": mail_type}  # as Slurm sets them
        return subprocess.run(
            [sys.executable, str(MAIL), "--spool", str(self.spool), "-s", f"Slurm Job_id={job}", recipient],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def notify(self) -> subprocess.CompletedProcess[str]:
        """One run of the sender, as its timer starts it."""
        (self.folder / "records.json").write_text(json.dumps(self.records))
        return subprocess.run(
            [
                sys.executable,
                str(NOTIFY),
                "--spool",
                str(self.spool),
                "--state",
                str(self.state),
                "--sacct",
                str(self.sacct),
                "--bot-token-file",
                str(self.token),
                "--slack-api",
                self.slack.url,
                "--user",
                "alice:U111",
                "--user",
                "bob:",
                "--user",
                "dave:U_GONE",
                "--interactive-partition",
                "shell",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_mail_program_queues_only_slack_job_endings(self) -> None:
        """Endings for the "slack" recipient are queued; starts, time warnings, and other recipients are not."""
        for job, mail_type, recipient in (
            ("12", "Ended", "slack"),
            ("13", "Failed", "slack"),
            ("14", "Began", "slack"),
            ("15", "Reached 90% of time limit", "slack"),
            ("16", "Ended", "alice@example.org"),
        ):
            result = self.mail(job, mail_type, recipient)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.spool.iterdir()), ["12-Ended", "13-Failed"])

    def test_mail_program_queues_nothing_while_job_messages_are_off(self) -> None:
        """Without the queue folder (no Slack bot token on the cluster), nothing is queued and no email is sent."""
        self.spool.rmdir()
        result = self.mail("12", "Ended", "slack")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.spool.exists())

    def test_one_private_message_per_ended_job(self) -> None:
        """The message names the job, how it ended with its exit code, its run time, and its machine; once."""
        self.records = {"12": '12|train "big"|alice|COMPLETED|0:0|02:13:05|gpu4|main|2026-10-29T10:00:00'}
        self.assertEqual(self.mail("12", "Ended", "slack").returncode, 0)
        result = self.notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(self.slack.messages[0]["channel"], "U111")
        text = self.slack.messages[0]["text"]
        self.assertIn('Your job 12 "train "big"" finished: Completed (exit code 0)', text)
        self.assertIn("ran 2 hr 13 min on gpu4", text)
        self.assertEqual(list(self.spool.iterdir()), [])
        # Queued again (a second mail type for the same job): not sent twice.
        self.assertEqual(self.mail("12", "Failed", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(len(self.slack.messages), 1)

    def test_waits_until_slurm_records_the_end(self) -> None:
        """A job whose end is not in accounting yet stays queued; a failure then says so, with its exit code."""
        self.records = {"15": "15|prep|alice|COMPLETING|0:0|00:00:40|cpu1|main|2026-10-29T10:00:00"}
        self.assertEqual(self.mail("15", "Ended", "slack").returncode, 0)
        self.assertEqual(self.mail("15", "Failed", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(self.slack.messages, [])
        self.assertEqual(len(list(self.spool.iterdir())), 2)
        self.records = {"15": "15|prep|alice|FAILED|1:0|00:00:40|cpu1|main|2026-10-29T10:00:00"}
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertIn('Your job 15 "prep" finished: Failed (exit code 1)', self.slack.messages[0]["text"])
        self.assertIn("ran 40 sec on cpu1", self.slack.messages[0]["text"])
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_no_message_without_slack_id_or_for_interactive_partitions(self) -> None:
        """A user without a Slack ID, or a job in a partition that only takes the interactive shell, gets none."""
        self.records = {
            "20": "20|train|bob|COMPLETED|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00",
            "21": "21|bash|alice|COMPLETED|0:0|00:05:00|gpu4|shell|2026-10-29T10:00:00",
            "22": "22|train|carol|TIMEOUT|0:0|1-00:00:10|gpu2|main|2026-10-29T10:00:00",
        }
        for job in ("20", "21", "22"):
            self.assertEqual(self.mail(job, "Ended", "slack").returncode, 0)
        result = self.notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.slack.messages, [])
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_time_limit_names_the_signal(self) -> None:
        self.records = {"30": "30|train|alice|TIMEOUT|0:15|1-02:00:00|gpu2|main|2026-10-29T10:00:00"}
        self.assertEqual(self.mail("30", "Ended", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertIn("Time limit reached (exit code 0, signal 15)", self.slack.messages[0]["text"])
        self.assertIn("ran 26 hr 0 min on gpu2", self.slack.messages[0]["text"])

    def test_refused_message_fails_the_run_once_and_is_not_repeated(self) -> None:
        """A message Slack refuses is logged and the run fails, so the service shows it; it is not tried again."""
        self.records = {"31": "31|train|alice|COMPLETED|0:0|00:05:00|gpu2|main|2026-10-29T10:00:00"}
        self.assertEqual(self.mail("31", "Ended", "slack").returncode, 0)
        self.slack.ok = False
        refused = self.notify()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("job 31 (alice)", refused.stderr)
        self.slack.ok = True
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(len(self.slack.messages), 1)

    def test_a_refused_message_does_not_hold_up_other_jobs(self) -> None:
        """A user whose Slack ID Slack refuses gets a logged error and the run fails; other jobs are still sent."""
        self.records = {
            "40": "40|train|dave|COMPLETED|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00",
            "41": "41|train|alice|COMPLETED|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00",
        }
        for job in ("40", "41"):
            self.assertEqual(self.mail(job, "Ended", "slack").returncode, 0)
        result = self.notify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("job 40 (dave)", result.stderr)
        self.assertIn("channel_not_found", result.stderr)
        self.assertEqual([body["channel"] for body in self.slack.messages], ["U_GONE", "U111"])
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_array_job_gets_one_message_once_every_task_ended(self) -> None:
        """One message for the whole array: its tasks, how many completed and failed, and its machines."""
        self.records = {
            "50": "50_0|sweep|alice|COMPLETED|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00\n"
            "50_1|sweep|alice|FAILED|1:0|00:01:00|cpu1|main|2026-10-29T10:00:00\n"
            "50_[2-4]|sweep|alice|PENDING|0:0|00:00:00|None assigned|main|2026-10-29T10:00:00"
        }
        self.assertEqual(self.mail("50", "Ended", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(self.slack.messages, [])
        self.records["50"] = self.records["50"].replace("PENDING", "CANCELLED by 2000")
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(len(self.slack.messages), 1)
        text = self.slack.messages[0]["text"]
        self.assertIn('Your job 50 "sweep" finished: 5 tasks, 1 completed, 4 did not complete', text)
        self.assertIn("ran on cpu1, gpu4", text)

    def test_heterogeneous_job_parts_are_read_separately(self) -> None:
        self.records = {
            "55": "55+0|het|alice|COMPLETED|0:0|00:02:00|gpu4|main|2026-10-29T10:00:00\n"
            "55+1|het|alice|COMPLETED|0:0|00:02:00|cpu1|main|2026-10-29T10:00:00"
        }
        self.assertEqual(self.mail("55", "Ended", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertIn('Your job 55 "het" finished: 2 tasks, 2 completed', self.slack.messages[0]["text"])

    def test_an_older_job_with_the_same_id_is_not_used(self) -> None:
        """After Slurm's job IDs start again, sacct also lists an old job with the ID: only the newest counts."""
        old = "60|old-run|bob|COMPLETED|0:0|00:05:00|gpu4|main|2025-01-02T10:00:00"
        self.records = {"60": old + "\n60|new-run|alice|RUNNING|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00"}
        self.assertEqual(self.mail("60", "Ended", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(self.slack.messages, [])
        self.records = {"60": old + "\n60|new-run|alice|COMPLETED|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00"}
        self.assertEqual(self.notify().returncode, 0)
        self.assertIn('Your job 60 "new-run" finished: Completed', self.slack.messages[0]["text"])

    def test_any_final_state_is_an_end(self) -> None:
        """A state the message has no words for (REVOKED, SPECIAL_EXIT) still ends the job."""
        self.records = {"65": "65|train|alice|REVOKED|0:0|00:00:00|None assigned|main|2026-10-29T10:00:00"}
        self.assertEqual(self.mail("65", "Ended", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertIn('Your job 65 "train" finished: Revoked', self.slack.messages[0]["text"])

    def test_job_still_not_ended_a_day_later_is_dropped_with_a_log_line(self) -> None:
        self.records = {"70": "70|train|alice|RUNNING|0:0|00:05:00|gpu4|main|2026-10-29T10:00:00"}
        self.assertEqual(self.mail("70", "Ended", "slack").returncode, 0)
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(len(list(self.spool.iterdir())), 1)
        day_ago = time.time() - 25 * 3600
        for path in self.spool.iterdir():
            os.utime(path, (day_ago, day_ago))
        result = self.notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dropped job 70", result.stdout)
        self.assertEqual(list(self.spool.iterdir()), [])
        self.assertEqual(self.slack.messages, [])


if __name__ == "__main__":
    unittest.main()
