"""Test the public submission command with an explicitly fake Slurm boundary."""

import base64
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COMMAND = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-submit"


class ScratchExecutionTests(unittest.TestCase):
    """Exercise real Git and file copies locally, without claiming a Slurm test."""

    def test_successful_non_git_job_cleans_only_its_own_copy(self) -> None:
        """Explicit selection works without Git, and successful output return cleans scratch."""
        implementation = runpy.run_path(str(COMMAND))
        with tempfile.TemporaryDirectory(prefix="cluster-modes-") as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            (project / "input").write_text("hello")
            (project / "extra inputs/nested").mkdir(parents=True)
            (project / "extra inputs/nested/value").write_text("nested")
            (project / "unselected").touch()
            spec = {
                "project": str(project),
                "source": "#!/bin/bash\nset -eu\ntest ! -e unselected\n"
                "test -f 'extra inputs/nested/value'\ncp input output\n",
                "arguments": [],
                "includes": ["input", "extra inputs"],
                "outputs": ["output"],
                "job": "local-test",
            }
            scratch = root / "scratch"
            scratch.mkdir()
            unrelated = scratch / "keep"
            unrelated.mkdir()
            self.assertEqual(implementation["execute"](spec, scratch), 0)
            self.assertEqual((project / "output").read_text(), "hello")
            self.assertTrue(unrelated.exists())
            self.assertEqual(list(scratch.glob("job-*")), [])

    def test_default_submission_captures_script_not_project_contents(self) -> None:
        """The public CLI sends a wrapper with original resource directives to sbatch."""
        with tempfile.TemporaryDirectory(prefix="cluster-modes-") as directory:
            project = Path(directory).resolve()
            script = project / "job.sh"
            source = (
                "#!/bin/bash\n#SBATCH --gpus=2\n#SBATCH --array=0-1\n"
                "#CLUSTER include='extra data'\n#CLUSTER copy-back=results/\n"
                "echo start\n#SBATCH --gpus=99\n"
            )
            script.write_text(source)
            fake = project / "sbatch"
            fake.write_text(
                f"#!{sys.executable}\nimport json, sys\n"
                "print(json.dumps({'args': sys.argv[1:], 'wrapper': sys.stdin.read()}))\n"
            )
            fake.chmod(0o700)
            env = dict(os.environ, PATH=str(project) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(
                [sys.executable, str(COMMAND), "job.sh", "arg with spaces"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            captured = json.loads(result.stdout)
            self.assertEqual(captured["args"], ["--chdir", str(project)])
            wrapper = captured["wrapper"]
            self.assertIn("#SBATCH --gpus=2\n#SBATCH --array=0-1\n", wrapper)
            self.assertNotIn("#SBATCH --gpus=99", wrapper)
            spec = json.loads(base64.b64decode(wrapper.split("--run ")[1].strip()))
            self.assertEqual(spec["source"], source)
            self.assertEqual(spec["arguments"], ["arg with spaces"])
            self.assertEqual(spec["includes"], ["extra data"])
            self.assertEqual(spec["outputs"], ["results/"])

    def test_selection_refuses_unsafe_paths_and_unselected_link_targets(self) -> None:
        """Paths cannot escape the project or silently read an unstaged link target."""
        implementation = runpy.run_path(str(COMMAND))
        with tempfile.TemporaryDirectory(prefix="cluster-modes-") as directory:
            project = Path(directory).resolve()
            (project / "target").write_text("data")
            (project / "link").symlink_to("target")
            for paths in (["../outside"], ["/tmp"], ["."], ["link"], []):
                with self.subTest(paths=paths), self.assertRaises(SystemExit):
                    implementation["selection"](project, paths)
            self.assertEqual(implementation["selection"](project, ["link", "target"]), [Path("link"), Path("target")])

    def test_tracked_working_files_and_failed_job_outputs(self) -> None:
        """Use uncommitted tracked edits, omit large untracked inputs, recover failures."""
        implementation = runpy.run_path(str(COMMAND))
        with tempfile.TemporaryDirectory(prefix="cluster-modes-") as directory:
            root = Path(directory)
            project = root / "Transformer 25"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "code.txt").write_text("old")
            (project / "src").mkdir()
            (project / "src/tracked.py").touch()
            subprocess.run(["git", "-C", str(project), "add", "code.txt", "src/tracked.py"], check=True)
            (project / "src/untracked-checkpoint").touch()
            (project / "code.txt").write_text("edited")
            (project / "checkpoint.bin").write_bytes(b"not selected")
            (project / "extra input.txt").write_text("selected explicitly")
            (project / ".venv").mkdir()
            (project / ".venv" / "marker").touch()
            body = (
                "#!/bin/bash\nset -eu\n"
                "test ! -e checkpoint.bin\ntest ! -e .venv\n"
                "test -f src/tracked.py\ntest ! -e src/untracked-checkpoint\n"
                "test -f 'extra input.txt'\nmkdir -p results/run-123\n"
                "cp code.txt results/run-123/value.txt\n"
                "echo scratch-edit > code.txt\nexit 17\n"
            )
            spec = {
                "project": str(project),
                "source": body,
                "arguments": [],
                "includes": ["extra input.txt"],
                "outputs": ["results/"],
                "job": "local-test",
            }
            scratch = root / "scratch"
            scratch.mkdir()
            self.assertEqual(implementation["execute"](spec, scratch), 17)
            self.assertEqual((project / "results/run-123/value.txt").read_text(), "edited")
            self.assertEqual((project / "code.txt").read_text(), "edited")

    def test_missing_input_prevents_execution(self) -> None:
        """A failed explicit-input copy must not run the application."""
        implementation = runpy.run_path(str(COMMAND))
        with tempfile.TemporaryDirectory(prefix="cluster-modes-") as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            spec = {
                "project": str(project),
                "source": "#!/bin/bash\necho BAD > ran\n",
                "arguments": [],
                "includes": ["missing"],
                "outputs": ["ran"],
                "job": "local-test",
            }
            scratch = root / "scratch"
            scratch.mkdir()
            self.assertNotEqual(implementation["execute"](spec, scratch), 0)
            self.assertFalse((project / "ran").exists())

    def test_copyback_failure_retains_scratch(self) -> None:
        """Never follow destination symlinks or discard the only output copy."""
        implementation = runpy.run_path(str(COMMAND))
        with tempfile.TemporaryDirectory(prefix="cluster-modes-") as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "input").touch()
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            (project / "results").symlink_to(elsewhere, target_is_directory=True)
            spec = {
                "project": str(project),
                "source": "#!/bin/bash\nmkdir results\necho saved > results/value\necho other > other-output\n",
                "arguments": [],
                "includes": ["input"],
                "outputs": ["results", "other-output"],
                "job": "local-test",
            }
            scratch = root / "scratch"
            scratch.mkdir()
            self.assertNotEqual(implementation["execute"](spec, scratch), 0)
            self.assertFalse((elsewhere / "value").exists())
            self.assertEqual((project / "other-output").read_text(), "other\n")
            copies = list(scratch.glob("*/project/results/value"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_text(), "saved\n")


class SharedSubmissionTests(unittest.TestCase):
    """Shared mode must preserve ordinary sbatch behavior, not rewrite jobs."""

    def test_shared_submission_preserves_user_inputs(self) -> None:
        """A fake sbatch observes the original script, arguments, cwd, and environment."""
        with tempfile.TemporaryDirectory(prefix="cluster-submit-test-") as directory:
            project = Path(directory) / "Transformer 25"
            project.mkdir()
            script = project / "job script.sh"
            source = (
                "#!/bin/bash\n#SBATCH --gpus=2\n#SBATCH --time=00:02:00\n"
                "#CLUSTER copy-back=results/\nprintf '%s\\n' \"$1\"\n"
            )
            script.write_text(source)
            fake = Path(directory) / "sbatch"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), "
                "'source': pathlib.Path(sys.argv[1]).read_text(), "
                "'environment': os.environ['VIRTUAL_ENV']}))\n"
                "sys.exit(int(os.environ['FAKE_SBATCH_EXIT']))\n"
            )
            fake.chmod(0o700)
            environment = dict(os.environ)
            environment.update(
                {
                    "PATH": directory + os.pathsep + environment["PATH"],
                    "VIRTUAL_ENV": str(project / ".venv"),
                    "FAKE_SBATCH_EXIT": "0",
                }
            )
            for exit_code in (0, 23):
                with self.subTest(exit_code=exit_code):
                    environment["FAKE_SBATCH_EXIT"] = str(exit_code)
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(COMMAND),
                            "--mode=shared",
                            script.name,
                            "argument with spaces",
                            "--application-option=value",
                        ],
                        cwd=project,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, exit_code, result.stderr)
                    observed = json.loads(result.stdout)
                    self.assertEqual(
                        observed["args"], [script.name, "argument with spaces", "--application-option=value"]
                    )
                    self.assertEqual(Path(observed["cwd"]).resolve(), project.resolve())
                    self.assertEqual(observed["source"], source)
                    self.assertEqual(observed["environment"], environment["VIRTUAL_ENV"])


if __name__ == "__main__":
    unittest.main()
