"""A deploy refuses Slurm node changes that would interrupt running jobs."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from nanohpc.deploy import DRY_RUN_FAILED, check_then_apply

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "src/nanohpc/ansible/files/slurm-change-check"
OLD = "NodeName=gpu4 CPUs=8 RealMemory=4096 Gres=gpu:a6000:4 State=UNKNOWN\n"
NEW = OLD.replace(":4 ", ":2 ")


class SlurmChangeCheckTests(unittest.TestCase):
    """Check the deployed files and every affected node before Ansible changes anything."""

    def test_changed_node_with_running_job_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "slurm.conf"
            current.write_text(OLD)
            squeue = root / "squeue"
            squeue.write_text("#!/bin/sh\nprintf '42 alice gpu4\\n'\n")
            squeue.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(CHECK),
                    "--squeue",
                    str(squeue),
                    "--kind",
                    "slurm.conf",
                    "--current",
                    str(current),
                    "--node",
                    "front",
                    "--new",
                    NEW,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("42 alice gpu4", result.stderr)
            self.assertIn("gpu4", result.stderr)

    def test_unchanged_node_and_idle_changed_node_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "slurm.conf"
            current.write_text(OLD)
            squeue = root / "squeue"
            squeue.write_text("#!/bin/sh\nexit 0\n")
            squeue.chmod(0o755)
            for new in (OLD, NEW):
                with self.subTest(new=new):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(CHECK),
                            "--squeue",
                            str(squeue),
                            "--kind",
                            "slurm.conf",
                            "--current",
                            str(current),
                            "--node",
                            "front",
                            "--new",
                            new,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_gres_change_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "gres.conf"
            current.write_text("Name=gpu File=/dev/nvidia[0-3]\n")
            squeue = root / "squeue"
            squeue.write_text("#!/bin/sh\nprintf '42 alice gpu4\\n'\n")
            squeue.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(CHECK),
                    "--squeue",
                    str(squeue),
                    "--kind",
                    "gres.conf",
                    "--current",
                    str(current),
                    "--node",
                    "gpu4",
                    "--new",
                    "Name=gpu File=/dev/nvidia[0-1]\n",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("42 alice gpu4", result.stderr)

    def test_full_and_partial_deploy_check_before_other_plays(self) -> None:
        for playbook in ("site.yml", "partial.yml"):
            with self.subTest(playbook=playbook):
                plays = yaml.safe_load((ROOT / "src/nanohpc/ansible" / playbook).read_text())
                first = plays[0]
                self.assertTrue(first["any_errors_fatal"])
                self.assertIn("slurm-change-check", str(first["tasks"]))
                self.assertIn("check_mode", str(first["tasks"]))

    def test_one_refusal_stops_the_whole_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            calls: list[list[str]] = []

            def run(arguments: list[str], record: Path) -> int:
                calls.append(arguments)
                record.write_text(
                    json.dumps(
                        {
                            "changed": {},
                            "facts": {},
                            "failed": {"gpu4": "slurm-change-check: refused: 42 alice gpu4"},
                        }
                    )
                )
                return 2

            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = check_then_apply(run, ["front", "gpu4", "cpu1"], {"front": "front node"}, Path(folder), False)
            self.assertEqual(code, DRY_RUN_FAILED)
            self.assertEqual(len(calls), 1)
            self.assertIn("nothing was deployed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
