"""Switching a front or login node between the shared /home and its own (home_mount): while the old /home is still
there, the preflight stops with what to do, before anything changes. Runs the
preflight's task file in real ansible-playbook on this machine, with the /home mount it would read given as a
variable."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/preflight/tasks"
TASKS = FOLDER / "own_home_check.yml"
SHARED_TASKS = FOLDER / "shared_home_check.yml"
PLAYBOOK = Path(sys.executable).parent / "ansible-playbook"


def run_tasks(tasks: Path, variables: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Run one preflight task file on this machine with the given variables."""
    with tempfile.TemporaryDirectory() as directory:
        play = Path(directory) / "play.yml"
        play.write_text(
            f"- hosts: localhost\n  gather_facts: false\n  tasks:\n    - ansible.builtin.import_tasks: {tasks}\n"
        )
        return subprocess.run(
            [str(PLAYBOOK), "-i", "localhost,", "-c", "local", str(play), "-e", json.dumps(variables)],
            capture_output=True, text=True, check=False, timeout=120,
        )  # fmt: skip


def run(mount: str, own: list[str]) -> subprocess.CompletedProcess[str]:
    return run_tasks(TASKS, {"home_mount": {"stdout": mount}, "nanohpc": {"home": {"own": own}}})


class OwnHomeSwitchTest(unittest.TestCase):
    def test_the_shared_home_still_mounted_stops_with_what_to_do(self) -> None:
        for mount in ("10.0.0.10:/home nfs4", "systemd-1 autofs"):
            with self.subTest(mount):
                result = run(mount, ["localhost"])
                self.assertNotEqual(result.returncode, 0)
                output = " ".join(result.stdout.split())
                self.assertIn("localhost: /home is still the shared /home", output)
                self.assertIn("cluster.yml now says home_mount: own. Nothing was changed on this machine.", output)
                self.assertIn("umount /home", output)
                self.assertIn("remove the /home line from /etc/fstab", output)

    def test_an_own_home_or_another_machine_passes(self) -> None:
        for mount, own in (("/dev/vdb ext4", ["localhost"]), ("", ["localhost"]), ("10.0.0.10:/home nfs4", [])):
            with self.subTest(mount=mount, own=own):
                result = run(mount, own)
                self.assertEqual(result.returncode, 0, result.stdout)


class SharedHomeSwitchTest(unittest.TestCase):
    """From own back to shared: the machine still has its own /home (the marker /etc/nanohpc/home-local), and the
    shared /home is not mounted yet (home_mount, read with -t nfs4, is empty)."""

    def run_check(self, mount: str, marker: bool, clients: list[str]) -> subprocess.CompletedProcess[str]:
        variables = {
            "home_mount": {"stdout": mount},
            "own_home_marker": {"stat": {"exists": marker}},
            "nanohpc": {"home": {"clients": clients, "server": "store"}},
        }
        return run_tasks(SHARED_TASKS, variables)

    def test_the_own_home_still_there_stops_with_what_to_do(self) -> None:
        result = self.run_check("", True, ["localhost"])
        self.assertNotEqual(result.returncode, 0)
        output = " ".join(result.stdout.split())
        self.assertIn(
            "localhost: this machine still has its own /home, and cluster.yml now says home_mount: shared.", output
        )
        self.assertIn("Nothing was changed on this machine.", output)
        self.assertIn("copy what users want to keep to the shared /home (on store)", output)
        self.assertIn("umount /home", output)
        self.assertIn("remove its /home line from /etc/fstab", output)
        self.assertIn("rm /etc/nanohpc/home-local", output)

    def test_already_shared_or_never_own_passes(self) -> None:
        for mount, marker, clients in (
            ("10.0.0.60:/home nfs4", True, ["localhost"]),
            ("", False, ["localhost"]),
            ("", True, []),
        ):
            with self.subTest(mount=mount, marker=marker, clients=clients):
                result = self.run_check(mount, marker, clients)
                self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
