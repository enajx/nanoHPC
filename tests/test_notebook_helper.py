"""Check the downloadable interactive notebook helper with a stand-in Jupyter command."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/website-source/public/job-examples/interactive-notebook.sh"


def fake_scontrol(folder: Path, nodes: str) -> None:
    """Make Slurm report the configured node names for one test allocation."""
    command = folder / "scontrol"
    command.write_text(f"#!/bin/sh\nprintf '%s\\n' '{nodes}'\n")
    command.chmod(0o755)


class NotebookHelperTests(unittest.TestCase):
    """Exercise the script users download from the How to page."""

    def test_jupyter_prints_cluster_tunnel_and_runs_in_job(self) -> None:
        """The helper gives a usable front-node tunnel and starts Jupyter through uv."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake_scontrol(base, "cpu1")
            fake = base / "uv"
            fake.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/uv-args"\n'
                'printf "%s" "$JUPYTER_TOKEN_FILE" > "$HOME/jupyter-token-file"\n'
                'cat "$JUPYTER_TOKEN_FILE" > "$HOME/jupyter-token"\n'
            )
            fake.chmod(0o755)
            env = {
                **os.environ,
                "HOME": temporary,
                "PATH": f"{temporary}:{os.environ['PATH']}",
                "SLURM_JOB_ID": "123",
                "SLURM_JOB_NODELIST": "cpu1",
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), "jupyter", "login.example.org"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"ssh -N -L \d+:cpu1:\d+ login.example.org")
            self.assertIn("login.example.org", result.stdout)
            args = (base / "uv-args").read_text()
            self.assertIn("jupyterlab", args)
            self.assertIn("--ip=0.0.0.0", args)
            self.assertEqual(len((base / "jupyter-token").read_text().strip()), 48)
            self.assertFalse(Path((base / "jupyter-token-file").read_text()).exists())

    def test_vscode_uses_private_token_file(self) -> None:
        """Browser VS Code receives a token file that is removed when the session exits."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake_scontrol(base, "cpu1")
            binary = base / ".local/bin/code"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                '#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/code-args"\nfor arg in "$@"; do file=$arg; done\ncat "$file" > "$HOME/token"\nprintf "%s" "$file" > "$HOME/token-file"\n'
            )
            binary.chmod(0o755)
            env = {
                **os.environ,
                "HOME": temporary,
                "PATH": f"{temporary}:{os.environ['PATH']}",
                "SLURM_JOB_ID": "123",
                "SLURM_JOB_NODELIST": "cpu1",
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), "vscode", "login.example.org"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            token = (base / "token").read_text().strip()
            self.assertEqual(len(token), 48)
            self.assertIn(f"tkn={token}", result.stdout)
            self.assertIn("0.0.0.0", (base / "code-args").read_text())
            self.assertFalse(Path((base / "token-file").read_text()).exists())

    def test_multiple_nodes_stop_before_printing_a_wrong_tunnel(self) -> None:
        """The helper refuses an allocation that may run on a different node."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake_scontrol(base, "cpu1\ncpu2")
            env = {
                **os.environ,
                "HOME": temporary,
                "PATH": f"{temporary}:{os.environ['PATH']}",
                "SLURM_JOB_ID": "123",
                "SLURM_JOB_NODELIST": "cpu[1-2]",
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), "jupyter", "login.example.org"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("one-node interactive Slurm job", result.stderr)
            self.assertNotIn("ssh -N", result.stdout)


if __name__ == "__main__":
    unittest.main()
