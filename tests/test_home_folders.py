"""Test cluster-home-folders: each user's largest folders in their home, written to a private file per user."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/nanohpc/files/cluster-home-folders"
MB = 1024**2


def write(path: Path, megabytes: int) -> None:
    """Write a file of real (allocated) data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(megabytes * MB))


class HomeFoldersTest(unittest.TestCase):
    """Largest folders first, git repositories and their worktrees marked, hard-linked files counted once."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="nanohpc-home-folders-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.directory)], check=True)
        self.user = subprocess.run(["id", "-un"], text=True, capture_output=True, check=True).stdout.strip()
        self.home = self.directory / "home"
        self.output = self.directory / "folders"
        self.output.mkdir()

    def run_command(self, users: list[str], min_mb: int) -> subprocess.CompletedProcess[str]:
        """Run the command as it runs on the home machine, with a small threshold."""
        arguments = [sys.executable, str(COMMAND), "--home-root", str(self.home), "--output-dir", str(self.output)]
        arguments += ["--min-folder-bytes", str(min_mb * MB)]
        for user in users:
            arguments += ["--user", f"{user}:{os.getuid()}"]
        return subprocess.run(arguments, text=True, capture_output=True, check=False)

    def lines(self) -> list[str]:
        """The user's file, without its first (title) line."""
        return (self.output / self.user).read_text().splitlines()[1:]

    def test_largest_folders_with_git_marks_and_hard_links_once(self) -> None:
        home = self.home / self.user
        write(home / "project/data.bin", 6)
        (home / "project/.git").mkdir()
        write(home / "project/.worktrees/run-1/out.bin", 2)
        write(home / "project/.worktrees/run-2/out.bin", 2)
        write(home / "datasets/big.bin", 3)
        # The same file linked from a second folder (like a uv .venv linked from the cache) counts once:
        # in the folder seen first by name.
        os.link(home / "datasets/big.bin", home / "venv-copy.bin")
        write(home / "small/notes.bin", 1)
        result = self.run_command([self.user], 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        box = self.output / self.user
        self.assertEqual(stat.S_IMODE(box.stat().st_mode), 0o400)
        self.assertEqual(box.stat().st_uid, os.getuid())
        title, *rows = box.read_text().splitlines()
        self.assertRegex(title, r"^Largest folders \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}\):$")
        names = [row.split("~/", 1)[1] for row in rows]
        self.assertEqual(
            names,
            ["project  (git repo)", "project/.worktrees (2 worktrees)", "datasets"],
        )
        sizes = [float(row.split()[0]) for row in rows]
        self.assertAlmostEqual(sizes[0], 10 / 1024, delta=0.01)
        # Not listed: the hard link (already counted in datasets) and folders under the threshold.
        self.assertNotIn("venv-copy.bin", box.read_text())
        self.assertNotIn("small", box.read_text())

    def test_nothing_large_and_no_home(self) -> None:
        write(self.home / self.user / "small/notes.bin", 1)
        result = self.run_command([self.user, "nohome"], 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.lines()), 1)
        self.assertRegex(self.lines()[0], r"^No single folder uses [0-9.]+ GB or more\.$")
        self.assertEqual((self.output / "nohome").read_text().splitlines()[1:], ["No home folder."])

    def test_unreadable_folders_are_skipped_and_one_failed_home_does_not_stop_the_others(self) -> None:
        """A folder root cannot read is left out; a home that cannot be read fails only that user, named in the
        error, and the run ends non-zero after writing every other user's file."""
        write(self.home / "first/data/a.bin", 3)
        locked = self.home / "first/data/locked"
        locked.mkdir()
        write(self.home / "second/project/a.bin", 3)
        (self.home / "broken").mkdir()
        for folder in (locked, self.home / "broken"):
            folder.chmod(0)
            self.addCleanup(folder.chmod, 0o755)
        result = self.run_command(["broken", "first", "second"], 2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("broken", result.stderr)
        self.assertNotIn("first", result.stderr)
        for user, folder in (("first", "~/data"), ("second", "~/project")):
            self.assertIn(folder, (self.output / user).read_text())
        self.assertFalse((self.output / "broken").exists())

    def test_links_are_not_followed(self) -> None:
        """Links to folders elsewhere are not counted, not even as a repository's .worktrees."""
        write(self.directory / "elsewhere/big.bin", 5)
        home = self.home / self.user
        (home / "project/.git").mkdir(parents=True)
        write(home / "project/a.bin", 3)
        (home / "project/.worktrees").symlink_to(self.directory / "elsewhere")
        (home / "link").symlink_to(self.directory / "elsewhere")
        (home / "plain").mkdir()
        (home / "plain/inner").symlink_to(self.directory / "elsewhere")
        result = self.run_command([self.user], 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.lines()
        self.assertEqual([row.split("~/", 1)[1] for row in rows], ["project  (git repo)"])
        self.assertLess(float(rows[0].split()[0]), 5 / 1024)

    def test_temporary_files_of_a_killed_run_are_removed(self) -> None:
        (self.home / self.user).mkdir(parents=True)
        (self.output / f".{self.user}.abc123").write_text("half")
        result = self.run_command([self.user], 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [self.user])

    def test_removed_users_lose_their_file(self) -> None:
        (self.home / self.user).mkdir(parents=True)
        (self.output / "olduser").write_text("old")
        result = self.run_command([self.user], 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [self.user])


class HomeFoldersPreflightTest(unittest.TestCase):
    """The folder of lists, written by nanoHPC, does not stop a deploy that mounts a disk or NFS over /home."""

    def test_list_folder_may_stay_hidden(self) -> None:
        tasks = yaml.safe_load((ROOT / "src/nanohpc/ansible/roles/preflight/tasks/main.yml").read_text())
        task = next(task for task in tasks if task["name"].startswith("Name the home folders that may stay hidden"))
        self.assertIn("'.nanohpc-home-folders'", task["ansible.builtin.set_fact"]["hideable_homes"])


if __name__ == "__main__":
    unittest.main()
