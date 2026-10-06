"""Check the downloadable interactive notebook helper with a stand-in Jupyter command."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/website-source/public/job-examples/interactive-notebook.sh"


class NotebookHelperTests(unittest.TestCase):
    """Exercise the script users download from the How to page."""

    def test_jupyter_prints_cluster_tunnel_and_runs_in_job(self) -> None:
        """The helper gives a usable front-node tunnel and starts Jupyter through uv."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake = base / "uv"
            fake.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/uv-args"\n'
                'printf "%s" "$JUPYTER_TOKEN_FILE" > "$HOME/jupyter-token-file"\n'
                'cat "$JUPYTER_TOKEN_FILE" > "$HOME/jupyter-token"\n'
            )
            fake.chmod(0o755)
            env = {**os.environ, "HOME": temporary, "PATH": f"{temporary}:{os.environ['PATH']}", "SLURM_JOB_ID": "123"}
            result = subprocess.run(
                ["bash", str(SCRIPT), "jupyter", "login.example.org"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ssh -N -L", result.stdout)
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
            binary = base / ".local/bin/code"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                '#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/code-args"\nfor arg in "$@"; do file=$arg; done\ncat "$file" > "$HOME/token"\nprintf "%s" "$file" > "$HOME/token-file"\n'
            )
            binary.chmod(0o755)
            env = {**os.environ, "HOME": temporary, "SLURM_JOB_ID": "123"}
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


if __name__ == "__main__":
    unittest.main()
