"""Verify the front node's automatic deploy runner (nanohpc-auto-deploy) and the `nanohpc --version` it relies on.

The runner is run as a program, with real Git: a bare repository stands for the configuration repository on GitHub,
and the runner's checkout is a clone of it. The `nanohpc` and installer programs it calls are FAKES written by these
tests: they record their calls (arguments, working folder, environment, whether stdin is /dev/null), print a
version, and exit with codes the tests choose. So these tests check the runner's decisions and what it runs, not a
real deploy.
"""

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "src/nanohpc/files/nanohpc-auto-deploy"
BRANCH = "main"

# FAKE nanohpc: `--version` prints the version in installed-version; `deploy` records its call and exits with the
# code in deploy-exit (0 when that file is missing).
FAKE_NANOHPC = """#!{python}
import json, os, sys
from pathlib import Path
root = Path({root!r})
if sys.argv[1:] == ["--version"]:
    print("nanohpc " + (root / "installed-version").read_text().strip())
    sys.exit(0)
null, stdin = os.stat("/dev/null"), os.fstat(0)
record = {{
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
    "automatic": os.environ.get("NANOHPC_AUTOMATIC"),
    "stdin_is_dev_null": (stdin.st_dev, stdin.st_ino) == (null.st_dev, null.st_ino),
    "cluster_yml": Path("cluster.yml").read_text(),
    "maintenance_fd_valid": (
        (fd := os.environ.get("NANOHPC_MAINTENANCE_FD")) is not None
        and os.fstat(int(fd)).st_ino == (root / "maintenance.lock").stat().st_ino
    ),
}}
with open(root / "deploy-calls", "a") as calls:
    calls.write(json.dumps(record) + "\\n")
print("fake deploy output")
code = root / "deploy-exit"
sys.exit(int(code.read_text()) if code.exists() else 0)
"""

# FAKE installer: records the version it was asked for, then "installs" it (writes installed-version) and exits 0,
# unless installer-exit holds another code, or installer-broken exists (exits 0 without installing).
FAKE_INSTALLER = """#!{python}
import sys
from pathlib import Path
root = Path({root!r})
with open(root / "installer-calls", "a") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\\n")
code = root / "installer-exit"
if code.exists():
    sys.exit(int(code.read_text()))
if not (root / "installer-broken").exists():
    (root / "installed-version").write_text(sys.argv[1] + "\\n")
"""


class Cluster:
    """A configuration repository, the front node's checkout of it, fake programs, and the runner's folders."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote.git"
        self.work = root / "work"
        self.checkout = root / "checkout"
        self.state = root / "state"
        self.metrics = root / "metrics" / "auto-deploy.prom"
        self.nanohpc = root / "fake-nanohpc"
        self.installer = root / "fake-installer"
        self.maintenance_lock = root / "maintenance.lock"
        self.runner = root / "nanohpc-auto-deploy"
        self.runner.write_text(RUNNER.read_text().replace("/run/nanohpc/maintenance.lock", str(self.maintenance_lock)))
        (root / "gitconfig").write_text("")
        self.environment = dict(os.environ)
        self.environment.update(
            GIT_CONFIG_GLOBAL=str(root / "gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.org",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.org",
        )
        self.environment.pop("NANOHPC_AUTOMATIC", None)
        self.state.mkdir()
        self.metrics.parent.mkdir()
        for path, template in [(self.nanohpc, FAKE_NANOHPC), (self.installer, FAKE_INSTALLER)]:
            path.write_text(template.format(python=sys.executable, root=str(root)))
            path.chmod(0o755)
        (root / "installed-version").write_text("1.0.0\n")
        self.git(root, "init", "--quiet", "--bare", "--initial-branch", BRANCH, str(self.remote))
        self.git(root, "clone", "--quiet", str(self.remote), str(self.work))
        self.git(self.work, "checkout", "--quiet", "-b", BRANCH)
        self.commit("nanohpc_version: 1.0.0\nname: first\n")
        self.git(root, "clone", "--quiet", "--branch", BRANCH, str(self.remote), str(self.checkout))

    def git(self, folder: Path, *arguments: str) -> str:
        """Run git in a folder and return its output; fail the test if git fails."""
        result = subprocess.run(
            ["git", "-C", str(folder), *arguments], capture_output=True, text=True, env=self.environment, check=False
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def commit(self, cluster_yml: str) -> str:
        """Commit a new cluster.yml in the work clone, push it to the remote, and return the commit."""
        (self.work / "cluster.yml").write_text(cluster_yml)
        self.git(self.work, "add", "cluster.yml")
        self.git(self.work, "commit", "--quiet", "--allow-empty", "-m", "change")
        self.git(self.work, "push", "--quiet", "--force", "origin", BRANCH)
        return self.git(self.work, "rev-parse", "HEAD")

    def run(self, remote: str) -> subprocess.CompletedProcess[str]:
        """Run the runner as the systemd timer does, with something on stdin that the deploy must not get."""
        return subprocess.run(
            [
                sys.executable,
                str(self.runner),
                "--checkout",
                str(self.checkout),
                "--branch",
                BRANCH,
                "--remote",
                remote,
                "--state",
                str(self.state),
                "--metrics",
                str(self.metrics),
                "--installer",
                str(self.installer),
                "--nanohpc",
                str(self.nanohpc),
                "--ssh-config",
                "/etc/nanohpc/ssh_config",
            ],
            input="secret on stdin\n",
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def deploys(self) -> list[dict[str, object]]:
        """Return the fake deploy's recorded calls."""
        calls = self.root / "deploy-calls"
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    def installs(self) -> list[str]:
        """Return the versions the fake installer was asked for."""
        calls = self.root / "installer-calls"
        return calls.read_text().splitlines() if calls.exists() else []

    def state_file(self, name: str) -> str | None:
        """Return a state file's content without the final newline, or None when it does not exist."""
        path = self.state / name
        return path.read_text().strip() if path.exists() else None

    def metric(self, name: str) -> str | None:
        """Return one metric's value (the whole sample line after the name for labelled ones), or None."""
        found = re.search(rf"^{re.escape(name)}(\{{.*\}})? (\S+)$", self.metrics.read_text(), re.MULTILINE)
        if found is None:
            return None
        return (found.group(1) or "") + " " + found.group(2) if found.group(1) else found.group(2)


class AutoDeployTests(unittest.TestCase):
    """Decisions of one run of the runner, checked through its exit code, state files, metrics, and fake calls."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cluster = Cluster(Path(self.folder.name))
        self.url = str(self.cluster.remote)

    def assert_run(self, expected_code: int) -> subprocess.CompletedProcess[str]:
        """Run once and check the exit code, showing the runner's output when it differs."""
        result = self.cluster.run(self.url)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        return result

    def test_first_deploy_of_a_new_commit(self) -> None:
        """A commit not yet deployed is deployed once, from the checkout, with the given SSH config."""
        commit = self.cluster.commit("nanohpc_version: 1.0.0\nname: second\n")
        result = self.assert_run(0)
        self.assertIn("fake deploy output", result.stdout)
        [call] = self.cluster.deploys()
        self.assertEqual(call["args"], ["deploy", "cluster.yml", "--ssh-config", "/etc/nanohpc/ssh_config"])
        self.assertEqual(Path(str(call["cwd"])).resolve(), self.cluster.checkout.resolve())
        self.assertEqual(call["cluster_yml"], "nanohpc_version: 1.0.0\nname: second\n")
        self.assertTrue(call["maintenance_fd_valid"])
        self.assertEqual(self.cluster.state_file("deployed"), commit)
        self.assertIsNone(self.cluster.state_file("failed"))
        self.assertEqual(self.cluster.installs(), [])

    def test_cluster_maintenance_retries_after_another_operation_releases_its_lock(self) -> None:
        """A busy shared lock leaves the commit unmarked, so the next timer run can deploy it."""
        commit = self.cluster.commit("nanohpc_version: 1.0.0\nname: second\n")
        with self.cluster.maintenance_lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = self.cluster.run(self.url)
            self.assertEqual(busy.returncode, 0, busy.stdout + busy.stderr)
            self.assertIn("maintenance", busy.stdout.lower())
            self.assertEqual(self.cluster.deploys(), [])
            self.assertIsNone(self.cluster.state_file("deployed"))
            self.assertIsNone(self.cluster.state_file("failed"))
        self.assert_run(0)
        self.assertEqual(self.cluster.state_file("deployed"), commit)

    def test_first_fetch_into_an_empty_checkout(self) -> None:
        """A checkout made without network access (git init, origin added, no commit yet) gets the branch checked out."""
        shutil.rmtree(self.cluster.checkout)
        self.cluster.checkout.mkdir()
        self.cluster.git(self.cluster.checkout, "init", "--quiet")
        self.cluster.git(self.cluster.checkout, "remote", "add", "origin", self.url)
        with (self.cluster.checkout / ".git/info/exclude").open("a") as exclude:
            exclude.write(".env\n")
        (self.cluster.checkout / ".env").write_text("NANOHPC_SLACK_WEBHOOK=https://hooks.example.org/x\n")
        commit = self.cluster.git(self.cluster.work, "rev-parse", "HEAD")
        self.assert_run(0)
        [call] = self.cluster.deploys()
        self.assertEqual(call["cluster_yml"], "nanohpc_version: 1.0.0\nname: first\n")
        self.assertEqual(self.cluster.state_file("deployed"), commit)
        self.assertEqual(self.cluster.git(self.cluster.checkout, "rev-parse", "HEAD"), commit)
        self.assertEqual(self.cluster.git(self.cluster.checkout, "branch", "--show-current"), BRANCH)
        newer = self.cluster.commit("nanohpc_version: 1.0.0\nname: second\n")
        self.assert_run(0)
        self.assertEqual(self.cluster.state_file("deployed"), newer)

    def test_deploy_gets_no_stdin_and_the_automatic_variable(self) -> None:
        """The deploy runs with stdin on /dev/null and NANOHPC_AUTOMATIC=1."""
        self.assert_run(0)
        [call] = self.cluster.deploys()
        self.assertTrue(call["stdin_is_dev_null"])
        self.assertEqual(call["automatic"], "1")

    def test_nothing_to_do_when_the_commit_is_deployed(self) -> None:
        """A second run with no new commit deploys nothing and only updates the check time."""
        self.assert_run(0)
        before = self.cluster.metrics.read_text()
        self.cluster.metrics.write_text(
            re.sub(r"^(cluster_auto_deploy_last_check_timestamp_seconds) \S+$", r"\1 1", before, flags=re.MULTILINE)
        )
        self.assert_run(0)
        self.assertEqual(len(self.cluster.deploys()), 1)
        self.assertNotEqual(self.cluster.metric("cluster_auto_deploy_last_check_timestamp_seconds"), "1")
        for name in [
            "cluster_auto_deploy_last_exit_code",
            "cluster_auto_deploy_last_run_timestamp_seconds",
            "cluster_auto_deploy_last_success_timestamp_seconds",
            "cluster_auto_deploy_info",
        ]:
            with self.subTest(metric=name):
                self.assertIsNotNone(self.metric_in(before, name))
                self.assertEqual(self.cluster.metric(name), self.metric_in(before, name))

    def metric_in(self, text: str, name: str) -> str | None:
        """Return a metric from a saved copy of the metrics file, in the same form as Cluster.metric."""
        saved = self.cluster.metrics.read_text()
        self.cluster.metrics.write_text(text)
        value = self.cluster.metric(name)
        self.cluster.metrics.write_text(saved)
        return value

    def test_failed_commit_is_not_retried_until_a_newer_one(self) -> None:
        """A commit whose deploy failed is recorded and skipped; the next commit is deployed."""
        (self.cluster.root / "deploy-exit").write_text("2")
        bad = self.cluster.commit("nanohpc_version: 1.0.0\nname: bad\n")
        self.assert_run(2)
        self.assertEqual(self.cluster.state_file("failed"), bad)
        self.assertIsNone(self.cluster.state_file("deployed"))
        (self.cluster.root / "deploy-exit").write_text("0")
        result = self.assert_run(0)
        self.assertIn(f"commit {bad} failed before; waiting for a newer commit", result.stdout)
        self.assertEqual(len(self.cluster.deploys()), 1)
        self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), "2")
        good = self.cluster.commit("nanohpc_version: 1.0.0\nname: good\n")
        self.assert_run(0)
        self.assertEqual(len(self.cluster.deploys()), 2)
        self.assertEqual(self.cluster.state_file("deployed"), good)
        self.assertIsNone(self.cluster.state_file("failed"))

    def test_journal_says_which_run_failed(self) -> None:
        """nanohpc deploy exits 3 when its dry run failed on some machines (left out, unchanged), 4 when its real
        run failed; the journal says which, and the metric keeps the code for the alert."""
        for code, meaning in (
            ("3", "its dry run failed on some machines, which were left out, unchanged"),
            ("4", "its real run failed, so machines may be partly changed"),
        ):
            with self.subTest(code=code):
                (self.cluster.root / "deploy-exit").write_text(code)
                commit = self.cluster.commit(f"nanohpc_version: 1.0.0\nname: exit-{code}\n")
                result = self.assert_run(int(code))
                self.assertIn(f"deploy of commit {commit} failed with exit code {code} ({meaning})", result.stderr)
                self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), code)

    def test_wrong_origin_is_refused(self) -> None:
        """A checkout whose origin is not the expected repository is not fetched or deployed."""
        result = self.cluster.run("git@github.com:lab/other.git")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("origin", result.stderr)
        self.assertIn("git@github.com:lab/other.git", result.stderr)
        self.assertEqual(self.cluster.deploys(), [])
        self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), "1")

    def test_local_changes_are_refused(self) -> None:
        """A changed tracked file or an untracked file stops the run; a file excluded in .git/info/exclude does not."""
        for name, content in [("cluster.yml", "nanohpc_version: 1.0.0\nname: edited\n"), ("notes.txt", "notes\n")]:
            with self.subTest(file=name):
                original = (self.cluster.checkout / name).read_text() if name == "cluster.yml" else None
                (self.cluster.checkout / name).write_text(content)
                result = self.assert_run(1)
                self.assertIn("local changes", result.stderr)
                self.assertEqual(self.cluster.deploys(), [])
                if original is None:
                    (self.cluster.checkout / name).unlink()
                else:
                    (self.cluster.checkout / name).write_text(original)
        with (self.cluster.checkout / ".git/info/exclude").open("a") as exclude:
            exclude.write(".env\n")
        (self.cluster.checkout / ".env").write_text("NANOHPC_SLACK_WEBHOOK=https://hooks.example.org/x\n")
        self.assert_run(0)
        self.assertEqual(len(self.cluster.deploys()), 1)

    def rewrite_branch(self, cluster_yml: str) -> str:
        """Replace the branch on the remote with a new history of one commit, and return that commit."""
        self.cluster.git(self.cluster.work, "checkout", "--quiet", "--orphan", "rewritten")
        self.cluster.git(self.cluster.work, "branch", "--quiet", "-D", BRANCH)
        self.cluster.git(self.cluster.work, "checkout", "--quiet", "-b", BRANCH)
        return self.cluster.commit(cluster_yml)

    def test_history_rewritten_past_the_deployed_commit_is_refused(self) -> None:
        """A branch whose history no longer contains the deployed commit is not checked out or deployed."""
        self.assert_run(0)
        deployed = self.cluster.state_file("deployed")
        rewritten = self.rewrite_branch("nanohpc_version: 1.0.0\nname: rewritten\n")
        result = self.assert_run(1)
        self.assertIn(
            f"{rewritten} does not contain the deployed commit {deployed}: the branch history was rewritten;"
            f" push a commit on top of {deployed}, or run nanohpc deploy by hand from the administrator's machine",
            result.stderr,
        )
        self.assertEqual(self.cluster.state_file("failed"), rewritten)
        self.assertEqual(self.cluster.git(self.cluster.checkout, "rev-parse", "HEAD"), deployed)
        self.assertEqual(len(self.cluster.deploys()), 1)

    def test_branch_moved_back_is_refused(self) -> None:
        """A branch reset to a commit older than the deployed one is refused, not deployed."""
        first = self.cluster.git(self.cluster.work, "rev-parse", "HEAD")
        second = self.cluster.commit("nanohpc_version: 1.0.0\nname: second\n")
        self.assert_run(0)
        self.cluster.git(self.cluster.work, "reset", "--quiet", "--hard", first)
        self.cluster.git(self.cluster.work, "push", "--quiet", "--force", "origin", BRANCH)
        result = self.assert_run(1)
        self.assertIn(f"{first} does not contain the deployed commit {second}", result.stderr)
        self.assertEqual(self.cluster.state_file("failed"), first)
        self.assertEqual(len(self.cluster.deploys()), 1)

    def test_failed_commit_dropped_by_a_force_push(self) -> None:
        """A deployed, B fails, then C (on A, without B) is force-pushed: C is deployed."""
        first = self.cluster.git(self.cluster.work, "rev-parse", "HEAD")
        self.assert_run(0)
        (self.cluster.root / "deploy-exit").write_text("2")
        bad = self.cluster.commit("nanohpc_version: 1.0.0\nname: bad\n")
        self.assert_run(2)
        self.assertEqual(self.cluster.git(self.cluster.checkout, "rev-parse", "HEAD"), bad)
        (self.cluster.root / "deploy-exit").write_text("0")
        self.cluster.git(self.cluster.work, "reset", "--quiet", "--hard", first)
        fixed = self.cluster.commit("nanohpc_version: 1.0.0\nname: fixed\n")
        self.assert_run(0)
        self.assertEqual(self.cluster.deploys()[-1]["cluster_yml"], "nanohpc_version: 1.0.0\nname: fixed\n")
        self.assertEqual(self.cluster.state_file("deployed"), fixed)
        self.assertIsNone(self.cluster.state_file("failed"))
        self.assertEqual(self.cluster.git(self.cluster.checkout, "rev-parse", "HEAD"), fixed)
        self.assertEqual(self.cluster.git(self.cluster.checkout, "branch", "--show-current"), BRANCH)

    def test_any_commit_is_accepted_before_the_first_success(self) -> None:
        """With no deployed commit yet, a rewritten branch is deployed even though the checkout is elsewhere."""
        (self.cluster.root / "deploy-exit").write_text("2")
        self.cluster.commit("nanohpc_version: 1.0.0\nname: bad\n")
        self.assert_run(2)
        (self.cluster.root / "deploy-exit").write_text("0")
        rewritten = self.rewrite_branch("nanohpc_version: 1.0.0\nname: rewritten\n")
        self.assert_run(0)
        self.assertEqual(self.cluster.state_file("deployed"), rewritten)

    def test_version_change_runs_the_installer(self) -> None:
        """A new pinned version is installed before the deploy."""
        self.cluster.commit("nanohpc_version: '1.2.0'  # pinned\nname: second\n")
        self.assert_run(0)
        self.assertEqual(self.cluster.installs(), ["1.2.0"])
        self.assertEqual(len(self.cluster.deploys()), 1)
        self.assertEqual((self.cluster.root / "installed-version").read_text().strip(), "1.2.0")

    def test_installer_failure_marks_the_commit_failed(self) -> None:
        """When the installer fails, or the version is still wrong after it, nothing is deployed."""
        for marker, content, code in [("installer-exit", "3", 3), ("installer-broken", "", 1)]:
            with self.subTest(case=marker):
                (self.cluster.root / marker).write_text(content)
                commit = self.cluster.commit(f'nanohpc_version: "1.3.0"\nname: {marker}\n')
                self.assert_run(code)
                self.assertEqual(self.cluster.state_file("failed"), commit)
                self.assertEqual(self.cluster.deploys(), [])
                self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), str(code))
                (self.cluster.root / marker).unlink()

    def test_missing_version_is_refused_and_marked_failed(self) -> None:
        """A cluster.yml without a top-level nanohpc_version line (or with an unreadable one) is not deployed."""
        for text in [
            "name: unpinned\n",
            "nanohpc_version: [1, 0]\n",
            "nanohpc_version: 1.0.0\nnanohpc_version: 2.0.0\n",
        ]:
            with self.subTest(cluster_yml=text):
                commit = self.cluster.commit(text)
                result = self.assert_run(1)
                self.assertIn("nanohpc_version", result.stderr)
                self.assertEqual(self.cluster.state_file("failed"), commit)
                self.assertEqual(self.cluster.deploys(), [])

    def test_lock_held_by_another_run(self) -> None:
        """While another process holds the lock, a run does nothing and exits 0."""
        with (self.cluster.state / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.assert_run(0)
        self.assertIn("another deploy is running", result.stdout)
        self.assertEqual(self.cluster.deploys(), [])
        self.assertFalse(self.cluster.metrics.exists())

    def test_fetch_failure_is_not_marked_failed(self) -> None:
        """When the repository cannot be reached, the run fails with git's code and the commit is tried again."""
        commit = self.cluster.commit("nanohpc_version: 1.0.0\nname: second\n")
        moved = self.cluster.root / "moved.git"
        self.cluster.remote.rename(moved)
        result = self.cluster.run(self.url)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.url, result.stderr)
        self.assertIn("is the front node's deploy key added to the repository?", result.stderr)
        self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), str(result.returncode))
        self.assertIsNone(self.cluster.state_file("failed"))
        self.assertEqual(self.cluster.deploys(), [])
        moved.rename(self.cluster.remote)
        self.assert_run(0)
        self.assertEqual(self.cluster.state_file("deployed"), commit)

    def test_metrics_contents_and_mode(self) -> None:
        """After a success and then a failure: exit code, times, the deployed commit, and mode 0644."""
        self.assert_run(0)
        deployed = self.cluster.state_file("deployed")
        assert deployed is not None
        self.assertEqual(stat.S_IMODE(self.cluster.metrics.stat().st_mode), 0o644)
        self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), "0")
        success = self.cluster.metric("cluster_auto_deploy_last_success_timestamp_seconds")
        run = self.cluster.metric("cluster_auto_deploy_last_run_timestamp_seconds")
        check = self.cluster.metric("cluster_auto_deploy_last_check_timestamp_seconds")
        for value in [success, run, check]:
            self.assertIsNotNone(value)
            self.assertGreater(float(str(value)), 1.7e9)
        self.assertEqual(
            self.cluster.metric("cluster_auto_deploy_info"), f'{{commit="{deployed[:12]}",branch="{BRANCH}"}} 1'
        )
        text = self.cluster.metrics.read_text()
        for name in ["last_exit_code", "last_run_timestamp_seconds", "last_success_timestamp_seconds", "info"]:
            self.assertIn(f"# TYPE cluster_auto_deploy_{name} gauge", text)
        (self.cluster.root / "deploy-exit").write_text("4")
        self.cluster.commit("nanohpc_version: 1.0.0\nname: broken\n")
        self.assert_run(4)
        self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_exit_code"), "4")
        self.assertEqual(self.cluster.metric("cluster_auto_deploy_last_success_timestamp_seconds"), success)
        self.assertEqual(
            self.cluster.metric("cluster_auto_deploy_info"), f'{{commit="{deployed[:12]}",branch="{BRANCH}"}} 1'
        )
        self.assertEqual(stat.S_IMODE(self.cluster.metrics.stat().st_mode), 0o644)
        self.assertEqual(sorted(path.name for path in self.cluster.metrics.parent.iterdir()), ["auto-deploy.prom"])


class VersionCommandTest(unittest.TestCase):
    """`nanohpc --version`, which the runner compares with the pinned version."""

    def test_version_is_printed(self) -> None:
        command = shutil.which("nanohpc")
        assert command is not None, "the nanohpc command is not installed in this environment"
        result = subprocess.run([command, "--version"], capture_output=True, text=True, check=False)
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        self.assertEqual((result.returncode, result.stdout), (0, f"nanohpc {version}\n"), result.stderr)


if __name__ == "__main__":
    unittest.main()
