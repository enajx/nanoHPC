"""Fill docs.md and policy.md templates with the cluster's own values from site.json."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/nanohpc/files/cluster-website-docs"
SITE = {
    "cluster_name": "labcluster",
    "logo": None,
    "login_address": "login.example.org",
    "home_quota_soft_gb": 300,
    "home_quota_hard_gb": 400,
    "scratch_cleanup_days": 14,
}


class WebsiteDocsTest(unittest.TestCase):
    """Run the command as the deploy does; it writes only when the result changes, and says so."""

    def run_script(self, folder: Path) -> subprocess.CompletedProcess[str]:
        arguments = [str(folder / "site.json"), str(folder / "docs.md.template"), str(folder / "docs.md")]
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--site", *arguments[:1], "--template", arguments[1], "--output", arguments[2]],
            capture_output=True, text=True, check=False,
        )  # fmt: skip

    def test_fill_and_report_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "site.json").write_text(json.dumps(SITE))
            (folder / "docs.md.template").write_text(
                "# {{cluster_name}}\nssh you@{{login_address}}\n{{home_quota_soft_gb}}/{{home_quota_hard_gb}} GB, "
                "{{scratch_cleanup_days}} days\n"
            )
            first = self.run_script(folder)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.strip(), "changed")
            self.assertEqual(
                (folder / "docs.md").read_text(), "# labcluster\nssh you@login.example.org\n300/400 GB, 14 days\n"
            )
            self.assertEqual(oct((folder / "docs.md").stat().st_mode & 0o777), oct(0o644))
            second = self.run_script(folder)
            self.assertEqual(second.stdout.strip(), "unchanged")

    def test_unknown_placeholder_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "site.json").write_text(json.dumps(SITE))
            (folder / "docs.md.template").write_text("{{partition_names}}\n")
            result = self.run_script(folder)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("partition_names", result.stderr)
            self.assertFalse((folder / "docs.md").exists())


if __name__ == "__main__":
    unittest.main()
