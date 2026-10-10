"""Exercise job waiting policy through its CLI with a fake accounting command."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-home-job-policy"


class HomeJobPolicyTests(unittest.TestCase):
    """Keep administrator limits and reject incomplete snapshots."""

    def test_policy_and_interrupted_changes(self) -> None:
        """Block, recover interrupted operations, restore prior limits, and preserve manual changes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "db.json"
            db.write_text(json.dumps({"alice": "4", "bob": ""}))
            fake = root / "sacctmgr"
            fake.write_text(
                "#!"
                + sys.executable
                + "\n"
                + """import json, sys
from pathlib import Path
p = Path(__file__).with_name('db.json')
db = json.loads(p.read_text())
args = sys.argv[1:]
if 'show' in args:
    for user, limit in db.items():
        print(user + '|' + limit)
else:
    user = next(x.removeprefix('name=') for x in args if x.startswith('name='))
    limit = next(x.removeprefix('MaxJobs=') for x in args if x.startswith('MaxJobs='))
    db[user] = '' if limit == '-1' else limit
    p.write_text(json.dumps(db))
    if p.with_name('fail').exists():
        sys.exit(1)
"""
            )
            fake.chmod(0o700)
            status = root / "status.json"
            state = root / "state.json"
            args = [
                sys.executable,
                str(SCRIPT),
                "--status",
                str(status),
                "--state",
                str(state),
                "--sacctmgr",
                str(fake),
                "--cluster",
                "lab",
                "--hard-bytes",
                "100",
                "--notice-bytes",
                "50",
                "--notice-dir",
                str(root / "usage"),
                "--user",
                f"alice:{os.getuid()}",
                "--user",
                f"bob:{os.getuid()}",
            ]

            def snapshot(used: int, old: bool) -> None:
                stamp = datetime.now(UTC) - timedelta(seconds=121 if old else 0)
                status.write_text(
                    json.dumps(
                        {
                            "generated_at": stamp.isoformat(),
                            "users": [
                                {"user": user, "home": {"used_bytes": used, "soft_bytes": 50, "hard_bytes": 100}}
                                for user in ("alice", "bob")
                            ],
                        }
                    )
                )

            def run(ok: bool) -> None:
                result = subprocess.run(args, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode == 0, ok, result.stderr)

            (root / "usage").mkdir()
            snapshot(101, True)
            run(False)
            self.assertFalse(state.exists())
            self.assertFalse((root / "usage/alice").exists())
            snapshot(101, False)
            (root / "fail").touch()
            run(False)
            self.assertEqual(json.loads(db.read_text())["alice"], "0")
            self.assertFalse((root / "usage/alice").exists())
            (root / "fail").unlink()
            run(True)
            self.assertEqual(json.loads(db.read_text()), {"alice": "0", "bob": "0"})
            notice = root / "usage/alice"
            self.assertIn("GB", notice.read_text())
            self.assertIn("New jobs wait", notice.read_text())
            self.assertIn("Running jobs continue", notice.read_text())
            self.assertEqual(stat.S_IMODE(notice.stat().st_mode), 0o400)
            self.assertEqual(notice.stat().st_uid, os.getuid())
            db.write_text(json.dumps({"alice": "7", "bob": "0"}))
            run(True)
            self.assertEqual(json.loads(db.read_text())["alice"], "7")
            snapshot(100, False)
            run(True)
            self.assertEqual(json.loads(db.read_text())["bob"], "0")
            snapshot(99, False)
            (root / "fail").touch()
            run(False)
            (root / "fail").unlink()
            run(True)
            self.assertEqual(json.loads(db.read_text()), {"alice": "7", "bob": ""})
            self.assertEqual(json.loads(state.read_text())["users"], {})
            before_notice = notice.read_bytes()
            for home in (
                None,
                {"used_bytes": 101, "soft_bytes": 50, "hard_bytes": 99},
                {"used_bytes": -1, "soft_bytes": 50, "hard_bytes": 100},
                {"used_bytes": True, "soft_bytes": 50, "hard_bytes": 100},
            ):
                with self.subTest(home=home):
                    snapshot(101, False)
                    data = json.loads(status.read_text())
                    data["users"][1]["home"] = home
                    status.write_text(json.dumps(data))
                    run(False)
                    self.assertEqual(json.loads(db.read_text()), {"alice": "7", "bob": ""})
                    self.assertEqual(notice.read_bytes(), before_notice)
            db.write_text(json.dumps({"alice": "0", "bob": "0"}))
            snapshot(101, False)
            run(True)
            snapshot(0, False)
            run(True)
            self.assertEqual(json.loads(db.read_text()), {"alice": "0", "bob": "0"})
            snapshot(50, False)
            run(True)
            self.assertIn("Notice:", notice.read_text())
            self.assertNotIn("New jobs wait", notice.read_text())
            snapshot(49, False)
            run(True)
            self.assertNotIn("Notice:", notice.read_text())
            self.assertNotIn("New jobs wait", notice.read_text())
            db.write_text(json.dumps({"alice": "7", "bob": ""}))
            snapshot(101, False)
            run(True)
            without_bob = args[:-2]
            removed = subprocess.run(without_bob, capture_output=True, text=True, check=False)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(json.loads(db.read_text())["alice"], "0")
            self.assertFalse((root / "usage/bob").exists())
