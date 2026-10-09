"""An upgraded Grafana removes only the empty folder from the old provider."""

import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "src/nanohpc/files/grafana/cleanup-legacy-folder"


class GrafanaUpgradeTest(unittest.TestCase):
    """Exercise the cleanup command through a local Grafana API stand-in."""

    def run_cleanup(self, old_dashboard: bool) -> tuple[subprocess.CompletedProcess[str], list[dict[str, str]]]:
        """Run the public cleanup command with an empty or occupied old folder."""
        folders = [
            {"uid": "old-uid", "title": "Cluster", "managedBy": "classic-file-provisioning"},
            {"uid": "nanohpc-cluster", "title": "lab", "managedBy": "classic-file-provisioning"},
        ]
        dashboards = [
            {"uid": "nanohpc-machines", "folderUid": "nanohpc-cluster"},
            {"uid": "nanohpc-history", "folderUid": "nanohpc-cluster"},
        ]
        if old_dashboard:
            dashboards.extend({"uid": f"unrelated-{index}", "folderUid": "another-folder"} for index in range(1001))
            dashboards.append({"uid": "other-dashboard", "folderUid": "old-uid"})
        expected_auth = "Basic " + base64.b64encode(b"nanohpc-admin:test-password").decode()

        class Handler(BaseHTTPRequestHandler):
            """Serve only the Grafana endpoints used by the cleanup command."""

            def do_GET(self) -> None:
                self.assert_auth()
                if self.path == "/api/folders":
                    body = folders
                elif self.path.startswith("/api/search?"):
                    query = parse_qs(urlsplit(self.path).query)
                    if query.get("type") != ["dash-db"]:
                        self.send_error(400)
                        return
                    folder_uids = query.get("folderUIDs")
                    selected = (
                        [item for item in dashboards if item["folderUid"] in folder_uids] if folder_uids else dashboards
                    )
                    body = selected[: int(query.get("limit", ["1000"])[0])]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_DELETE(self) -> None:
                self.assert_auth()
                if self.path != "/api/folders/old-uid":
                    self.send_error(404)
                    return
                folders[:] = [folder for folder in folders if folder["uid"] != "old-uid"]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"message":"Folder deleted"}')

            def assert_auth(self) -> None:
                """Require the admin password supplied through the secrets file."""
                if self.headers.get("Authorization") != expected_auth:
                    self.send_error(401)
                    raise AssertionError("missing Grafana admin authorization")

            def log_message(self, format: str, *args: object) -> None:
                """Keep the focused test output quiet."""

        with tempfile.TemporaryDirectory() as temporary:
            secrets = Path(temporary) / "secrets.env"
            secrets.write_text("GF_SECURITY_ADMIN_PASSWORD=test-password\n")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                result = subprocess.run(
                    [sys.executable, str(CLEANUP), f"http://127.0.0.1:{server.server_port}", "lab", str(secrets)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
        return result, folders

    def test_empty_legacy_folder_is_removed(self) -> None:
        result, folders = self.run_cleanup(False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([folder["uid"] for folder in folders], ["nanohpc-cluster"])
        self.assertIn("removed", result.stdout)

    def test_occupied_legacy_folder_is_preserved(self) -> None:
        result, folders = self.run_cleanup(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([folder["uid"] for folder in folders], ["old-uid", "nanohpc-cluster"])
        self.assertIn("still contains", result.stderr)
