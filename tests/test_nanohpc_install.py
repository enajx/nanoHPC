"""The front node's nanoHPC installer, run with a fake uv (labelled fake) that records what it was asked."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/nanohpc-install"
FAKE_UV = """#!/bin/sh
printf '%s\\n' "$@" > "$RECORD"
printf '%s\\n' "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "$UV_PYTHON_INSTALL_DIR" >> "$RECORD"
"""


class InstallTest(unittest.TestCase):
    """nanoHPC <version> from PyPI, the Git repository at tag v<version>, or a copied wheel."""

    def run_install(self, folder: Path, conf: str, version: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        (folder / "bin").mkdir(exist_ok=True)
        uv = folder / "bin" / "uv"
        uv.write_text(FAKE_UV)
        uv.chmod(0o755)
        (folder / "install.conf").write_text(conf)
        environment = {
            **os.environ,
            "PATH": f"{folder / 'bin'}:{os.environ['PATH']}",
            "RECORD": str(folder / "record"),
            "NANOHPC_INSTALL_CONF": str(folder / "install.conf"),
            "NANOHPC_INSTALL_WHEELS": str(folder / "wheels"),
        }
        result = subprocess.run([str(SCRIPT), version], capture_output=True, text=True, check=False, env=environment)
        record = (folder / "record").read_text().splitlines() if (folder / "record").exists() else []
        return result, record

    def test_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "wheels").mkdir()
            (folder / "wheels" / "nanohpc-0.1.0-py3-none-any.whl").write_text("x")
            cases = [
                ("KIND=pypi\nURL=\n", "nanohpc==0.1.0"),
                ("KIND=git\nURL=https://github.com/enajx/nanoHPC\n", "git+https://github.com/enajx/nanoHPC@v0.1.0"),
                ("KIND=wheel\nURL=\n", str(folder / "wheels" / "nanohpc-0.1.0-py3-none-any.whl")),
            ]
            for conf, spec in cases:
                with self.subTest(spec):
                    result, record = self.run_install(folder, conf, "0.1.0")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(record[:5], ["tool", "install", "--force", "--python", "3.12"])
                    self.assertEqual(record[5], spec)
                    self.assertEqual(
                        record[6:], ["/opt/nanohpc-tool/tools", "/opt/nanohpc-tool/bin", "/opt/nanohpc-tool/python"]
                    )

    def test_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "wheels").mkdir()
            for conf, version, message in [
                ("KIND=pypi\nURL=\n", "latest", "not a version"),
                ("KIND=wheel\nURL=\n", "0.2.0", "no wheel for nanoHPC 0.2.0"),
                ("KIND=other\nURL=\n", "0.1.0", "unknown install kind"),
            ]:
                with self.subTest(message):
                    result, record = self.run_install(folder, conf, version)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(record, [])


if __name__ == "__main__":
    unittest.main()
