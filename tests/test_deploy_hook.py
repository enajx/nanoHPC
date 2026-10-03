"""Verify the front node's GitHub webhook listener starts the automatic deploy only for signed pushes to the branch.

Each test starts the real listener in a subprocess on a free local port, with a fake systemctl that records its
arguments, and sends real HTTP requests to it. The fake systemctl means these tests check what the listener asks
systemd to do, not that systemd starts the unit.
"""

import hashlib
import hmac
import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src/nanohpc/files/nanohpc-deploy-hook"
SECRET = "test-webhook-secret-4f9a"
UNIT = "nanohpc-auto-deploy.service"
PATH = "/cluster/deploy-hook"
START = ["start", "--no-block", UNIT]


def free_port() -> int:
    """Return a local TCP port that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def sign(body: bytes, secret: str) -> str:
    """Return the X-Hub-Signature-256 value GitHub sends for this body and secret."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push_body(ref: str) -> bytes:
    """Return a small push event body for this ref."""
    return json.dumps({"ref": ref, "after": "0" * 40, "repository": {"full_name": "lab/cluster-config"}}).encode()


class KeepErrorAnswers(urllib.request.HTTPErrorProcessor):
    """Return 4xx and 5xx answers like any other, instead of raising them."""

    def http_response(
        self, request: urllib.request.Request, response: http.client.HTTPResponse
    ) -> http.client.HTTPResponse:
        return response


OPENER = urllib.request.build_opener(KeepErrorAnswers)


class Listener:
    """The real listener running in a subprocess, with a fake systemctl that records its arguments."""

    def __init__(self, root: Path, systemctl_exit: int) -> None:
        self.calls_file = root / "calls"
        systemctl = root / "systemctl"
        systemctl.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"with open({str(self.calls_file)!r}, 'a') as calls:\n"
            "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"sys.exit({systemctl_exit})\n"
        )
        systemctl.chmod(0o755)
        secret_file = root / "secret"
        secret_file.write_text(SECRET + "\n")
        self.port = free_port()
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(HOOK),
                "--listen",
                f"127.0.0.1:{self.port}",
                "--secret-file",
                str(secret_file),
                "--branch",
                "main",
                "--unit",
                UNIT,
                "--systemctl",
                str(systemctl),
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while True:
            if self.process.poll() is not None:
                raise AssertionError("the listener exited: " + self.stop())
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            if time.monotonic() > deadline:
                raise AssertionError("the listener did not start listening within 10 seconds")
            time.sleep(0.05)

    def calls(self) -> list[list[str]]:
        """Return the argument lists the fake systemctl was run with."""
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def send(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, str]:
        """Send one HTTP request with urllib and return the status code and the answer's text."""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, headers=headers, method=method
        )
        with OPENER.open(request, timeout=10) as answer:
            return answer.status, answer.read().decode()

    def event(self, event: str, body: bytes, signature: str | None) -> tuple[int, str]:
        """POST a GitHub event with JSON content to the hook path."""
        headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.send("POST", PATH, body, headers)

    def stop(self) -> str:
        """Stop the listener and return what it wrote on stderr."""
        if self.process.poll() is None:
            self.process.terminate()
        _, stderr = self.process.communicate(timeout=10)
        return stderr


class DeployHookTests(unittest.TestCase):
    """Requests to the real listener and what it asks systemctl to do."""

    def start(self, systemctl_exit: int) -> Listener:
        """Start a listener in a fresh temporary folder, stopped at the end of the test."""
        directory = tempfile.TemporaryDirectory(prefix="deploy-hook-")
        self.addCleanup(directory.cleanup)
        listener = Listener(Path(directory.name), systemctl_exit)
        self.addCleanup(listener.stop)
        return listener

    def test_signed_push_to_the_branch_starts_the_unit(self) -> None:
        """A signed push to refs/heads/main runs `systemctl start --no-block <unit>` once and answers 202."""
        listener = self.start(0)
        body = push_body("refs/heads/main")
        status, text = listener.event("push", body, sign(body, SECRET))
        self.assertEqual(status, 202, text)
        self.assertIn("started", text)
        self.assertEqual(listener.calls(), [START])

    def test_ping_answers_pong(self) -> None:
        """GitHub's signed ping answers 200 pong and starts nothing."""
        listener = self.start(0)
        body = json.dumps({"zen": "Keep it simple.", "hook_id": 1}).encode()
        self.assertEqual(listener.event("ping", body, sign(body, SECRET)), (200, "pong\n"))
        self.assertEqual(listener.calls(), [])

    def test_other_branches_and_events_are_ignored(self) -> None:
        """Pushes to other branches or tags, and other events, answer 202 and start nothing."""
        listener = self.start(0)
        for ref in ["refs/heads/dev", "refs/heads/main2", "refs/tags/main", "main"]:
            body = push_body(ref)
            status, text = listener.event("push", body, sign(body, SECRET))
            self.assertEqual((status, text), (202, "ignored: other branch\n"), ref)
        body = push_body("refs/heads/main")
        status, text = listener.event("issues", body, sign(body, SECRET))
        self.assertEqual(status, 202)
        self.assertIn("ignored", text)
        self.assertEqual(listener.calls(), [])

    def test_missing_or_wrong_signatures_are_refused(self) -> None:
        """Without a valid HMAC-SHA256 signature of the exact body, the answer is 401 and nothing starts."""
        listener = self.start(0)
        body = push_body("refs/heads/main")
        other_body = push_body("refs/heads/dev")
        sha1 = "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
        for signature in [
            None,
            "",
            sign(body, "another-secret"),
            sign(other_body, SECRET),
            sign(body, SECRET).removeprefix("sha256="),
            sign(body, SECRET).upper(),
            sha1,
            "sha256=",
            "sha256=é",
        ]:
            status, _ = listener.event("push", body, signature)
            self.assertEqual(status, 401, signature)
        status, _ = listener.event("ping", body, None)
        self.assertEqual(status, 401)
        self.assertEqual(listener.calls(), [])

    def test_other_methods_and_paths_are_refused(self) -> None:
        """Only POST to a path ending in /deploy-hook is accepted: other paths 404, other methods 405."""
        listener = self.start(0)
        body = push_body("refs/heads/main")
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": sign(body, SECRET),
        }
        for path in ["/", "/cluster/", "/cluster/deploy-hook/", "/cluster/deploy-hooks", "/cluster/deploy-hook/x"]:
            self.assertEqual(listener.send("POST", path, body, headers)[0], 404, path)
        for method in ["GET", "PUT", "DELETE", "PATCH"]:
            self.assertEqual(listener.send(method, PATH, body, headers)[0], 405, method)
        self.assertEqual(listener.send("GET", "/other", None, {})[0], 404)
        self.assertEqual(listener.calls(), [])

    def test_bodies_over_1_mib_and_bad_lengths_are_refused(self) -> None:
        """A Content-Length over 1 MiB answers 413 without reading the body; a missing or bad one 411 or 400.

        These requests go through http.client, which can send a Content-Length header that does not match the body
        (urllib always sets it from the body), and lets the test read the 413 without sending 1 MiB first (the
        listener closes the connection without reading the body, so a client still sending it gets a broken pipe).
        """
        listener = self.start(0)
        body = push_body("refs/heads/main")
        signature = sign(body, SECRET)
        for length, expected in [(str(1024 * 1024 + 1), 413), ("12abc", 400), ("-1", 400), ("+5", 400), (None, 411)]:
            connection = http.client.HTTPConnection("127.0.0.1", listener.port, timeout=10)
            connection.putrequest("POST", PATH)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-GitHub-Event", "push")
            connection.putheader("X-Hub-Signature-256", signature)
            if length is not None:
                connection.putheader("Content-Length", length)
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, expected, length)
            connection.close()
        self.assertEqual(listener.calls(), [])

    def test_quick_pushes_start_the_unit_once(self) -> None:
        """A second valid push within 10 seconds answers 202 already starting, and systemctl runs once."""
        listener = self.start(0)
        body = push_body("refs/heads/main")
        first = listener.event("push", body, sign(body, SECRET))
        second = listener.event("push", body, sign(body, SECRET))
        self.assertEqual(first[0], 202)
        self.assertIn("started", first[1])
        self.assertEqual(second, (202, "already starting\n"))
        self.assertEqual(listener.calls(), [START])

    def test_failed_start_answers_500(self) -> None:
        """If systemctl fails, the answer is 500, so GitHub shows the delivery as failed."""
        listener = self.start(1)
        body = push_body("refs/heads/main")
        status, _ = listener.event("push", body, sign(body, SECRET))
        self.assertEqual(status, 500)
        self.assertEqual(listener.calls(), [START])

    def test_log_has_one_line_per_request_without_secrets(self) -> None:
        """Each request is logged on stderr with method, path, event, and result, never the secret or signature."""
        listener = self.start(0)
        body = push_body("refs/heads/main")
        signature = sign(body, SECRET)
        listener.event("push", body, signature)
        listener.event("push", body, sign(body, "wrong"))
        stderr = listener.stop()
        lines = [line for line in stderr.splitlines() if PATH in line]
        self.assertEqual(len(lines), 2, stderr)
        for line in lines:
            self.assertIn("POST", line)
            self.assertIn("push", line)
        self.assertIn("202", lines[0])
        self.assertIn("401", lines[1])
        self.assertNotIn(SECRET, stderr)
        self.assertNotIn(signature.removeprefix("sha256="), stderr)
        self.assertNotIn(sign(body, "wrong").removeprefix("sha256="), stderr)

    def test_bad_settings_stop_the_listener(self) -> None:
        """Missing arguments or an empty secret file stop the listener at start with a message."""
        with tempfile.TemporaryDirectory(prefix="deploy-hook-") as directory:
            empty = Path(directory) / "secret"
            empty.write_text("\n")
            arguments = ["--listen", "127.0.0.1:1", "--branch", "main", "--unit", UNIT, "--systemctl", "/bin/true"]
            missing = subprocess.run(
                [sys.executable, str(HOOK), *arguments], capture_output=True, text=True, check=False, timeout=10
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("--secret-file", missing.stderr)
            empty_secret = subprocess.run(
                [sys.executable, str(HOOK), *arguments, "--secret-file", str(empty)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(empty_secret.returncode, 0)
            self.assertIn("secret", empty_secret.stderr)


if __name__ == "__main__":
    unittest.main()
