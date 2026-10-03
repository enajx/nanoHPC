"""Tests for the cluster.yml model the setup wizard edits: comments and order survive every edit."""

import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from nanohpc import clusterfile

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cluster.yml"
MINIMAL = ROOT / "examples" / "minimal.yml"
MINIMAL_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReplaceWithYourOwnPublicKey alice@laptop"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReplaceWithYourOwnPublicKey carol@laptop"
GPU2 = {
    "address": "192.168.104.12",
    "roles": ["compute"],
    "cpu": {"sockets": 1, "cores_per_socket": 4, "threads_per_core": 2},
    "memory_mb": 4096,
    "gpu": {"type": "rtx6000ada", "count": 2},
    "partitions": ["main"],
    "scratch": {"image_gb": 2},
}


class RoundTripTest(unittest.TestCase):
    """Loading and saving without edits changes nothing."""

    def test_examples_are_byte_identical(self) -> None:
        for source in (EXAMPLE, MINIMAL):
            with self.subTest(path=source.name), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "cluster.yml"
                clusterfile.load(source).save(target)
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                self.assertEqual(clusterfile.load(source).as_text(), source.read_text())


class EditTest(unittest.TestCase):
    """Each edit changes only the lines it is about."""

    def setUp(self) -> None:
        self.original = EXAMPLE.read_text()
        self.file = clusterfile.load(EXAMPLE)

    def test_set_machine_keeps_comments_of_other_machines(self) -> None:
        self.file.set_machine("gpu2", {**GPU2, "memory_mb": 8192})
        expected = self.original.replace(
            "    memory_mb: 4096\n    gpu: {type: rtx6000ada", "    memory_mb: 8192\n    gpu: {type: rtx6000ada"
        )
        self.assertEqual(self.file.as_text(), expected)
        self.assertIn("    scratch: {device: /dev/vdb}  # a spare disk for scratch\n", self.file.as_text())

    def test_new_machine_goes_at_the_end_of_machines(self) -> None:
        self.file.set_machine("gpu5", {**GPU2, "address": "192.168.104.15"})
        text = self.file.as_text()
        self.assertIn(
            "      path: /srv/nanohpc-backup\n\n  gpu5:\n    address: 192.168.104.15\n    roles: [compute]\n", text
        )
        self.assertIn("    scratch: {image_gb: 2}\n\nusers:\n", text)
        self.assertEqual(self.file.machines()["gpu5"]["cpu"]["cores_per_socket"], 4)
        self.assertEqual(self.file.validate(), [])

    def test_remove_last_machine_keeps_the_blank_line_before_users(self) -> None:
        self.file.set_value(["backup"], None)
        self.file.remove_machine("store")
        text = self.file.as_text()
        self.assertIn("    scratch: {image_gb: 2}\n\nusers:\n", text)
        self.assertNotIn("store", self.file.machines())
        self.assertEqual(self.file.validate(), [])

    def test_emptied_sections_are_written_as_empty_and_can_be_filled_again(self) -> None:
        file = clusterfile.load(MINIMAL)
        machines = file.machines()
        file.remove_user("alice")
        for name in reversed(machines):
            file.remove_machine(name)
        self.assertIn("  admins: []\n", file.as_text())
        self.assertIn("\nmachines: {}\n\nusers: []\n\npartitions:\n", file.as_text())
        for name, values in machines.items():
            file.set_machine(name, values)
        file.set_user("alice", 2000, [MINIMAL_KEY], True)
        self.assertEqual(file.as_text(), MINIMAL.read_text())

    def test_website_hostname_edit_keeps_line_comments(self) -> None:
        self.file.set_value(["cluster", "website", "hostname"], "hpc.example.org")
        self.file.set_value(["cluster", "website", "login_address"], "login.example.org")
        expected = self.original.replace(
            "    hostname: cluster.example.org\n", "    hostname: hpc.example.org\n"
        ).replace(
            "    login_address: cluster.example.org   # shown in the user guide\n",
            "    login_address: login.example.org     # shown in the user guide\n",  # the comment keeps its column
        )
        self.assertEqual(self.file.as_text(), expected)
        self.assertEqual(self.file.get(["cluster", "website", "hostname"]), "hpc.example.org")

    def test_remove_user_also_removes_admin(self) -> None:
        self.file.set_user("bob", 2001, [KEY], True)
        self.assertEqual(self.file.get(["cluster", "admins"]), ["alice", "bob"])
        self.file.remove_user("alice")
        self.assertEqual(self.file.get(["cluster", "admins"]), ["bob"])
        self.assertEqual([user["name"] for user in self.file.users()], ["bob"])
        self.assertEqual(self.file.validate(), [])

    def test_validate_reports_a_wrong_field_with_its_path(self) -> None:
        self.file.set_value(["cluster", "website", "https"], "maybe")
        self.assertIn("cluster.website.https must be letsencrypt or own", self.file.validate())

    def test_strings_that_read_as_other_types_are_quoted(self) -> None:
        self.file.set_value(["backup", "time"], "04:30")
        self.file.set_partition("interactive", {"jobs": "interactive", "max_time": "12:00:00"})
        self.assertEqual(self.file.validate(), [])
        self.file.set_value(["auto_deploy", "branch"], "1:30")
        self.file.set_value(["nanohpc_version"], "0.2")
        parsed = yaml.safe_load(self.file.as_text())
        self.assertEqual(parsed["backup"]["time"], "04:30")
        self.assertEqual(parsed["partitions"]["interactive"]["max_time"], "12:00:00")
        self.assertEqual(parsed["auto_deploy"]["branch"], "1:30")
        self.assertEqual(parsed["nanohpc_version"], "0.2")


class NewFileTest(unittest.TestCase):
    """A new file from the wizard."""

    def test_new_file_validates_once_machines_and_users_are_added(self) -> None:
        file = clusterfile.new("mylab")
        self.assertEqual(file.get(["cluster", "name"]), "mylab")
        self.assertEqual(file.partitions(), {"main": {"default": True, "jobs": "any", "max_time": "24:00:00"}})
        self.assertNotEqual(file.validate(), [])
        file.set_machine("front", {"address": "10.0.0.10", "roles": ["front", "home"]})
        file.set_machine("gpu1", {**GPU2, "address": "10.0.0.11", "memory_mb": 128000})
        file.set_user("alice", 2000, [KEY], True)
        self.assertEqual(file.validate(), [])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            file.save(path)
            self.assertEqual(clusterfile.load(path).as_text(), file.as_text())
        self.assertIn("#", file.as_text())


if __name__ == "__main__":
    unittest.main()
