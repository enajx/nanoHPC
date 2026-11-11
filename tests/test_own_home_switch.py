"""Switching a front or login node from the shared /home to its own (home_mount: own): while the shared /home (NFS,
or its automount) is still mounted there, the preflight stops with what to do, before anything changes. Runs the
preflight's task file in real ansible-playbook on this machine, with the /home mount it would read given as a
variable."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/preflight/tasks/own_home_check.yml"
PLAYBOOK = Path(sys.executable).parent / "ansible-playbook"


def run(mount: str, own: list[str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        play = Path(directory) / "play.yml"
        play.write_text(
            f"- hosts: localhost\n  gather_facts: false\n  tasks:\n    - ansible.builtin.import_tasks: {TASKS}\n"
        )
        variables = {"home_mount": {"stdout": mount}, "nanohpc": {"home": {"own": own}}}
        return subprocess.run(
            [str(PLAYBOOK), "-i", "localhost,", "-c", "local", str(play), "-e", json.dumps(variables)],
            capture_output=True, text=True, check=False, timeout=120,
        )  # fmt: skip


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


if __name__ == "__main__":
    unittest.main()
