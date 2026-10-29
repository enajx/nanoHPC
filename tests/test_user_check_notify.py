"""Exercise the user check notifier through its CLI and local HTTP endpoints."""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "src/nanohpc/files/nanohpc-user-check-notify"


def finding(identity: str, user: str, kind: str, action: str, detail: str) -> dict:
    """A Prometheus vector series with empty labels omitted, as Prometheus does."""
    labels = {
        "id": identity,
        "user": user,
        "kind": kind,
        "action": action,
        "detail": detail,
        "host": "compute-1",
        "program": "cloudflared" if kind == "tunnel" else "",
        "slurm_job": "42" if kind == "tunnel" else "",
    }
    return {"metric": {key: value for key, value in labels.items() if value}, "value": [1791208800, "1791208800"]}


class FakeHTTP:
    """Answer Prometheus queries and record Slack webhook posts."""

    def __init__(self, series: list[dict]) -> None:
        self.series = series
        self.messages: list[str] = []
        self.private_messages: list[dict[str, str]] = []
        self.private_authorizations: list[str] = []
        self.private_ok = True
        self.prometheus_status = "success"
        self.webhook_status = 200
        self.webhook_body = "ok"
        self.fail_post_number: int | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = urlparse(self.path)
                if query.path != "/api/v1/query" or parse_qs(query.query).get("query") != [
                    "cluster_user_check_finding"
                ]:
                    self.send_error(404)
                    return
                body = json.dumps(
                    {"status": outer.prometheus_status, "data": {"resultType": "vector", "result": outer.series}}
                ).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                if self.path not in ("/webhook", "/chat.postMessage"):
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/chat.postMessage":
                    outer.private_messages.append(json.loads(body))
                    outer.private_authorizations.append(self.headers["Authorization"])
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": outer.private_ok, "error": "not_in_channel"}).encode())
                    return
                outer.messages.append(json.loads(body)["text"])
                if len(outer.messages) == outer.fail_post_number:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"failed")
                    return
                self.send_response(outer.webhook_status)
                self.end_headers()
                self.wfile.write(outer.webhook_body.encode())

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


class UserCheckNotifyTest(unittest.TestCase):
    """Each finding is posted once and failed posts stay retryable."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="nanohpc-user-check-notify-"))
        self.addCleanup(shutil.rmtree, self.folder)
        self.http = FakeHTTP([])
        self.addCleanup(self.http.close)
        self.webhook = self.folder / "webhook"
        self.webhook.write_text(self.http.url + "/webhook\n")
        self.token = self.folder / "bot-token"
        self.token.write_text("xoxb-test-token\n")
        self.state = self.folder / "state.json"

    def run_notify(self, now: str, private: bool = False) -> subprocess.CompletedProcess[str]:
        """Invoke the same script and arguments a systemd service can use."""
        command = [
            sys.executable,
            str(NOTIFY),
            "--prometheus",
            self.http.url,
            "--webhook-file",
            str(self.webhook),
            "--state",
            str(self.state),
            "--now",
            now,
            "--user",
            "alice:U123",
            "--guide-url",
            "https://cluster.example.org/hpc/#docs",
        ]
        if private:
            command.extend(("--bot-token-file", str(self.token), "--slack-api", self.http.url))
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_tunnel_posts_once_with_user_machine_and_job(self) -> None:
        self.http.series = [finding("compute-1-42", "alice", "tunnel", "stopped", "")]
        for now in ("2026-10-05T12:00:00+00:00", "2026-10-07T12:00:00+00:00"):
            self.assertEqual(self.run_notify(now).returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        message = self.http.messages[0]
        for word in ("<@U123>", "cloudflared", "compute-1", "job 42", "stopped"):
            self.assertIn(word, message)
        self.assertNotIn("(alice)", message)

    def test_port_post_names_program_once_and_links_guide(self) -> None:
        """The public post names the program and user once and points at this cluster's guide."""
        self.http.series = [finding("compute-1-port-alice", "alice", "port", "lasting", "`codex login` (port 1455)")]
        result = self.run_notify("2026-10-05T12:00:00+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.http.messages[0]
        self.assertEqual(message.count("<@U123>"), 1)
        self.assertNotIn("(alice)", message)
        self.assertIn("`codex login` (port 1455)", message)
        self.assertIn("https://cluster.example.org/hpc/#docs", message)
        self.assertIn("must run inside a Slurm job", message)

    def test_vscode_post_explains_remote_ssh_and_alternatives(self) -> None:
        """A VS Code finding gives a single notice with working options."""
        self.http.series = [finding("front-vscode-alice", "alice", "vscode-server", "lasting", "")]
        result = self.run_notify("2026-10-05T12:00:00+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.http.messages), 1)
        message = self.http.messages[0]
        self.assertEqual(message.count("<@U123>"), 1)
        self.assertIn("Remote-SSH cannot connect", message)
        self.assertIn("Existing Jupyter Server", message)
        self.assertIn("https://cluster.example.org/hpc/#docs", message)

    def test_private_port_and_vscode_notices_link_the_guide(self) -> None:
        """The corresponding private notices keep the same advice as the channel posts."""
        self.http.series = [
            finding("port", "alice", "port", "lasting", "`codex login` (port 1455)"),
            finding("vscode", "alice", "vscode-server", "lasting", ""),
        ]
        result = self.run_notify("2026-10-05T12:00:00+00:00", private=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = [body["text"] for body in self.http.private_messages]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("https://cluster.example.org/hpc/#docs" in message for message in messages))
        self.assertTrue(any("`codex login` (port 1455)" in message for message in messages))
        self.assertTrue(any("Remote-SSH cannot connect" in message for message in messages))

    def test_lasting_finding_posts_once_with_empty_labels(self) -> None:
        """A lasting finding is posted when first seen and not again while it lasts, not even a day later."""
        self.http.series = [
            finding("compute-1-outside-alice", "alice", "outside-job", "lasting", "python serve.py"),
            finding("compute-1-disk", "", "disk", "lasting", "/ is 93% full"),
        ]
        for now in (
            "2026-10-05T12:00:00+00:00",
            "2026-10-05T13:00:00+00:00",
            "2026-10-06T12:00:00+00:00",
            "2026-10-09T12:00:00+00:00",
        ):
            self.assertEqual(self.run_notify(now).returncode, 0)
        self.assertEqual(len(self.http.messages), 2)
        self.assertEqual(sum("python serve.py" in message for message in self.http.messages), 1)
        self.assertEqual(sum("93% full" in message for message in self.http.messages), 1)
        self.assertFalse(any("Reminder" in message for message in self.http.messages))

    def test_finding_seen_every_day_for_weeks_is_not_posted_again(self) -> None:
        """A finding that lasts longer than 7 days is still the same finding."""
        self.http.series = [finding("a", "alice", "outside-job", "lasting", "python")]
        for day in range(5, 25):
            self.assertEqual(self.run_notify(f"2026-10-{day:02d}T12:00:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        self.assertEqual(len(self.http.private_messages), 1)

    def test_finding_back_within_7_days_posts_nothing(self) -> None:
        """A finding that ends and comes back within 7 days of ending is not posted again; after 7 days it is."""
        lasting = finding("a", "alice", "outside-job", "lasting", "python")
        tunnel = finding("t", "alice", "tunnel", "stopped", "")
        self.http.series = [lasting, tunnel]
        self.assertEqual(self.run_notify("2026-10-05T12:00:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 2)
        self.assertEqual(len(self.http.private_messages), 1)
        # Gone for six days, then back: nothing is posted, in the channel or privately.
        self.http.series = []
        self.assertEqual(self.run_notify("2026-10-05T12:01:00+00:00", private=True).returncode, 0)
        self.http.series = [lasting, tunnel]
        self.assertEqual(self.run_notify("2026-10-11T12:00:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 2)
        self.assertEqual(len(self.http.private_messages), 1)
        # Gone for 7 days from its last sighting: it is new again.
        self.http.series = []
        self.assertEqual(self.run_notify("2026-10-11T12:01:00+00:00", private=True).returncode, 0)
        self.assertEqual(self.run_notify("2026-10-18T12:00:00+00:00", private=True).returncode, 0)
        self.http.series = [lasting, tunnel]
        self.assertEqual(self.run_notify("2026-10-18T12:01:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 4)
        self.assertEqual(len(self.http.private_messages), 2)

    def test_rejected_webhook_does_not_mark_finding_posted(self) -> None:
        self.http.series = [finding("compute-1-42", "alice", "tunnel", "stopped", "")]
        self.http.webhook_body = "invalid"
        failed = self.run_notify("2026-10-05T12:00:00+00:00")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.state.exists())
        self.http.webhook_body = "ok"
        self.assertEqual(self.run_notify("2026-10-05T12:01:00+00:00").returncode, 0)
        self.assertEqual(len(self.http.messages), 2)
        self.assertEqual(len(json.loads(self.state.read_text())["posted"]), 1)

    def test_later_failure_keeps_earlier_accepted_post(self) -> None:
        self.http.series = [
            finding("a", "alice", "tunnel", "reported", ""),
            finding("b", "alice", "tunnel", "stopped", ""),
        ]
        self.http.fail_post_number = 2
        self.assertNotEqual(self.run_notify("2026-10-05T12:00:00+00:00").returncode, 0)
        self.assertEqual(list(json.loads(self.state.read_text())["posted"]), ["a"])
        self.http.fail_post_number = None
        self.assertEqual(self.run_notify("2026-10-05T12:01:00+00:00").returncode, 0)
        self.assertEqual(len(self.http.messages), 3)
        self.assertIn("reported, not stopped", self.http.messages[0])
        self.assertIn("stopped", self.http.messages[-1])

    def test_bad_prometheus_response_fails_without_state(self) -> None:
        self.http.prometheus_status = "error"
        self.assertNotEqual(self.run_notify("2026-10-05T12:00:00+00:00").returncode, 0)
        self.assertEqual(self.http.messages, [])
        self.assertFalse(self.state.exists())

    def test_lasting_finding_private_message_once(self) -> None:
        self.http.series = [
            finding("a", "alice", "outside-job", "lasting", "python"),
            finding("disk", "", "disk", "lasting", "/ is 93% full"),
        ]
        for now in ("2026-10-05T12:00:00+00:00", "2026-10-05T13:00:00+00:00", "2026-10-06T12:00:00+00:00"):
            self.assertEqual(self.run_notify(now, private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 2)
        self.assertEqual(len(self.http.private_messages), 1)
        self.assertEqual(self.http.private_authorizations, ["Bearer xoxb-test-token"])
        self.assertEqual({body["channel"] for body in self.http.private_messages}, {"U123"})
        self.assertTrue(all("Hi alice" in body["text"] for body in self.http.private_messages))

    def test_private_failure_retries_without_repeating_channel(self) -> None:
        self.http.series = [finding("a", "alice", "outside-job", "lasting", "python")]
        self.http.private_ok = False
        self.assertNotEqual(self.run_notify("2026-10-05T12:00:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        self.assertEqual(len(self.http.private_messages), 1)
        self.assertIn("a", json.loads(self.state.read_text())["posted"])
        self.http.private_ok = True
        self.assertEqual(self.run_notify("2026-10-05T12:01:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        self.assertEqual(len(self.http.private_messages), 2)
        self.assertIn("a", json.loads(self.state.read_text())["private_posted"])

    def test_without_token_channel_still_posts(self) -> None:
        self.http.series = [finding("a", "alice", "outside-job", "lasting", "python")]
        self.assertEqual(self.run_notify("2026-10-05T12:00:00+00:00").returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        self.assertEqual(self.http.private_messages, [])
        self.assertEqual(self.run_notify("2026-10-05T12:01:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        self.assertEqual(len(self.http.private_messages), 1)

    def test_user_without_slack_id_has_channel_notice_only(self) -> None:
        self.http.series = [finding("b", "bob", "port", "lasting", "127.0.0.1:8888")]
        self.assertEqual(self.run_notify("2026-10-05T12:00:00+00:00", private=True).returncode, 0)
        self.assertEqual(len(self.http.messages), 1)
        self.assertEqual(self.http.private_messages, [])


if __name__ == "__main__":
    unittest.main()
