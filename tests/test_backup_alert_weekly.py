"""Exercise the weekly backup reminder through its CLI and local HTTP endpoints."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/nanohpc/files/nanohpc-backup-alert-weekly"


class BackupAlertWeeklyTest(unittest.TestCase):
    """Only active backup alerts appear in the weekly Slack reminder."""

    def test_active_backup_alerts_show_start_day_and_other_alerts_are_ignored(self) -> None:
        alerts = [
            {
                "labels": {"alertname": "BackupFailed", "machine": "home"},
                "annotations": {"summary": "The last /home backup from home failed."},
                "startsAt": "2026-10-09T04:00:00Z",
                "status": {"state": "active"},
            },
            {
                "labels": {"alertname": "BackupOld", "machine": "store"},
                "annotations": {"summary": "The last successful /home backup from store is more than 26 hours old."},
                "startsAt": "2026-10-10T04:00:00Z",
                "status": {"state": "active"},
            },
            {
                "labels": {"alertname": "BackupFailed", "machine": "other"},
                "annotations": {"summary": "suppressed backup"},
                "startsAt": "2026-10-08T04:00:00Z",
                "status": {"state": "suppressed"},
            },
            {
                "labels": {"alertname": "MachineMetricsMissing", "machine": "gpu"},
                "annotations": {"summary": "unrelated"},
                "startsAt": "2026-10-08T04:00:00Z",
                "status": {"state": "active"},
            },
        ]
        posts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/api/v2/alerts":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(alerts).encode())

            def do_POST(self) -> None:
                posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())["text"])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory() as directory:
            webhook = Path(directory) / "webhook"
            webhook.write_text(base + "/webhook\n")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--alertmanager",
                    base,
                    "--webhook-file",
                    str(webhook),
                    "--cluster",
                    "lab",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(posts), 1)
        self.assertIn("Backup problems still active", posts[0])
        self.assertIn("The last /home backup from home failed. (since 9 Oct)", posts[0])
        self.assertIn("The last successful /home backup from store is more than 26 hours old. (since 10 Oct)", posts[0])
        self.assertNotIn("suppressed", posts[0])
        self.assertNotIn("unrelated", posts[0])

        alerts.clear()
        with tempfile.TemporaryDirectory() as directory:
            webhook = Path(directory) / "webhook"
            webhook.write_text(base + "/webhook\n")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--alertmanager",
                    base,
                    "--webhook-file",
                    str(webhook),
                    "--cluster",
                    "lab",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
