"""Tests for `nanohpc init`, the setup wizard, driven headless with Textual's Pilot.

The machines are FAKES: probe_machine, user_ids, plan_fix, and apply_fix are replaced by the fakes below
(the wizard takes them as a Dependencies object), so no SSH runs. The real functions are tested in
test_probe.py and test_fixuid.py.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Checkbox, DataTable, Input, ListView, Select, Static, TextArea

from nanohpc import clusterfile
from nanohpc.config import load_config
from nanohpc.fixuid import FixPlan
from nanohpc.probe import Disk, MachineFacts
from nanohpc.wizard import Dependencies, WizardApp

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cluster.yml"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReplaceWithYourOwnPublicKey alice@laptop"
SIZE = (130, 45)


def facts(target: str, addresses: list[str], gpus: list[str], error: str | None) -> MachineFacts:
    """FAKE probe result: a 32-CPU, 128 GiB Ubuntu 24.04 machine with a root disk and an empty spare disk."""
    disks = [
        Disk("/dev/nvme0n1", 512.1, None, None, "Samsung SSD", "disk"),
        Disk("/dev/nvme0n1p1", 512.0, "ext4", "/", None, "part"),
        Disk("/dev/sdb", 2000.4, None, None, "WD Red", "disk"),
    ]
    if error is not None:
        return MachineFacts(target, "", [], 0, None, None, None, 0, [], [], "", "", False, [], error)
    return MachineFacts(target, target, addresses, 32, 1, 16, 2, 131072, gpus, disks, "ubuntu", "24.04", True, [], None)


class Fakes:
    """FAKE machines: canned probe facts by ssh target, canned user IDs, and a canned fix-uid plan.
    Every call is recorded."""

    def __init__(self, machines: dict[str, MachineFacts], ids: dict[str, dict[str, tuple[int, int] | None]]) -> None:
        self.machines = machines
        self.ids = ids
        self.probed: list[str] = []
        self.checked: list[str] = []
        self.planned: list[tuple[str, str, int]] = []
        self.applied: list[tuple[str, FixPlan]] = []

    def probe_machine(self, target: str, ssh_config: Path | None) -> MachineFacts:
        """FAKE probe_machine."""
        self.probed.append(target)
        return self.machines[target]

    def user_ids(self, target: str, ssh_config: Path | None, names: list[str]) -> dict[str, tuple[int, int] | None]:
        """FAKE user_ids."""
        self.checked.append(target)
        found = self.ids.get(target, {})
        return {name: found.get(name) for name in names}

    def plan_fix(self, target: str, ssh_config: Path | None, user: str, uid: int) -> FixPlan:
        """FAKE plan_fix: a plan that can proceed."""
        self.planned.append((target, user, uid))
        old = self.ids[target][user]
        assert old is not None
        commands = [["groupmod", "-g", str(uid), user], ["usermod", "-u", str(uid), user]]
        return FixPlan(
            target, user, uid, old[0], old[1], user, True, [], [], ["/"], 3, ["/home/alice"], False, commands
        )

    def apply_fix(self, target: str, ssh_config: Path | None, plan: FixPlan) -> int:
        """FAKE apply_fix: the user now has the new UID on that machine."""
        self.applied.append((target, plan))
        self.ids[target][plan.user] = (plan.new_id, plan.new_id)
        print(f"{plan.user} on {target} now has UID {plan.new_id} and GID {plan.new_id}")
        return 0

    def dependencies(self) -> Dependencies:
        """Return the fakes as the wizard's dependencies."""
        return Dependencies(self.probe_machine, self.user_ids, self.plan_fix, self.apply_fix)


async def settle(pilot: Pilot) -> None:
    """Wait for the wizard's workers (probes, checks) and the screen updates they cause."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def text(app: WizardApp, selector: str) -> str:
    """Return the text shown by a Static widget."""
    return str(app.screen.query_one(selector, Static).render())


async def fill(pilot: Pilot, values: dict[str, str]) -> None:
    """Type values into the inputs of the dialog on top."""
    for selector, value in values.items():
        pilot.app.screen.query_one(selector, Input).value = value
    await pilot.pause()


async def add_machine(pilot: Pilot, name: str, target: str) -> None:
    """Add a machine from the Machines step and wait for its probe."""
    await pilot.press("a")
    await fill(pilot, {"#machine-name": name, "#machine-target": target})
    await pilot.click("#ok")
    await settle(pilot)


class NewClusterTest(unittest.IsolatedAsyncioTestCase):
    """A whole new cluster through every step gives a valid cluster.yml."""

    async def test_new_cluster_through_all_steps(self) -> None:
        fakes = Fakes(
            {
                "10.0.0.10": facts("10.0.0.10", ["10.0.0.10"], [], None),
                "gpu1": facts("gpu1", ["10.0.0.11", "172.16.0.11"], ["NVIDIA RTX A6000"] * 4, None),
            },
            {},
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await fill(pilot, {"#cluster-name": "lab"})
                await pilot.click("#create")
                await settle(pilot)
                # 1 Machines: the first machine is the front node (and /home), the next ones compute.
                await add_machine(pilot, "front", "10.0.0.10")
                await add_machine(pilot, "gpu1", "gpu1")
                self.assertEqual(fakes.probed, ["10.0.0.10", "gpu1"])
                table = app.screen.query_one("#machine-table", DataTable)
                self.assertIn("✓ 32c", str(table.get_row("gpu1")[-1]))
                # Edit gpu1: the GPU type is suggested from the probe, the address chosen among the probed ones.
                table.move_cursor(row=table.get_row_index("gpu1"))
                table.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#gpu-type", Input).value, "a6000")
                self.assertEqual(app.screen.query_one("#gpu-count", Input).value, "4")
                app.screen.query_one("#probed-address", Select).value = "172.16.0.11"
                await pilot.pause()
                await pilot.click("#ok")
                await pilot.pause()
                self.assertEqual(app.state.file.machines()["gpu1"]["address"], "172.16.0.11")
                self.assertEqual(app.state.file.machines()["front"]["roles"], ["front", "home"])
                # 2 Storage: /home on the front node's root disk; scratch on gpu1 in an image file.
                await pilot.press("n")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#home-machine", Select).value, "front")
                app.screen.query_one("#home-device", Select).value = "/dev/sdb"
                await pilot.pause()
                self.assertIn("sudo mkfs.ext4 -O quota /dev/sdb", text(app, "#home-advice"))
                self.assertIn("never formats a disk", text(app, "#home-advice"))
                app.screen.query_one("#home-device", Select).value = "root"
                app.screen.query_one("#scratch-gpu1", Select).value = "image"
                await pilot.pause()
                await fill(pilot, {"#scratch-gb-gpu1": "200"})
                # 3 Users: one administrator, UID suggested from 2000.
                await pilot.press("escape", "n")
                await pilot.pause()
                await pilot.press("a")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#user-uid", Input).value, "2000")
                await fill(pilot, {"#user-name": "alice"})
                app.screen.query_one("#user-keys", TextArea).text = KEY
                app.screen.query_one("#user-admin", Checkbox).value = True
                await pilot.click("#ok")
                await pilot.pause()
                # 4 Partitions: the defaults; 5 Website; 6 Extras: no changes.
                await pilot.press("n")
                await pilot.pause()
                table = app.screen.query_one("#partition-table", DataTable)
                self.assertEqual(table.row_count, 1)
                await pilot.press("n")
                await pilot.pause()
                await fill(pilot, {"#website-hostname": "cluster.lab.example.org"})
                await pilot.press("escape", "n", "n")
                await pilot.pause()
                # 7 Review: no errors, then save.
                self.assertEqual(len(app.screen.query_one("#errors", ListView).children), 0)
                self.assertIn("ssh-add -c", text(app, "#next-steps"))
                self.assertIn(f"nanohpc deploy {path}", text(app, "#next-steps"))
                await pilot.click("#save")
                await pilot.pause()
            saved = clusterfile.load(path)
            self.assertEqual(saved.validate(), [])
            config, errors = load_config(path, True, False)
            self.assertEqual(errors, [])
            self.assertEqual(config["cluster"]["admins"], ["alice"])
            gpu1 = config["machines"]["gpu1"]
            self.assertEqual(gpu1["gpu"], {"type": "a6000", "count": 4})
            self.assertEqual(gpu1["cpu"], {"sockets": 1, "cores_per_socket": 16, "threads_per_core": 2})
            self.assertEqual(gpu1["scratch"], {"device": None, "image_gb": 200})
            self.assertEqual(config["machines"]["front"]["home"], {"device": None})
            self.assertEqual(config["cluster"]["website"]["hostname"], "cluster.lab.example.org")


class ExistingFileTest(unittest.IsolatedAsyncioTestCase):
    """Opening an existing cluster.yml shows it, and saving without changes keeps it byte for byte."""

    async def test_open_example_and_save_unchanged(self) -> None:
        fakes = Fakes({}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            shutil.copy(EXAMPLE, path)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                table = app.screen.query_one("#machine-table", DataTable)
                self.assertEqual(
                    [str(key.value) for key in table.rows], ["front", "gpu4", "gpu2", "cpu1", "gpu4i", "store"]
                )
                self.assertEqual(str(table.get_row("gpu4")[-1]), "not probed")
                for _ in range(6):
                    await pilot.press("n")
                    await pilot.pause()
                users = app.screen.query_one("#user-table", DataTable)
                self.assertEqual([str(key.value) for key in users.rows], ["alice", "bob"])
                self.assertEqual(len(app.screen.query_one("#errors", ListView).children), 0)
                await pilot.press("q")
                await pilot.pause()
            self.assertEqual(fakes.probed, [])
            self.assertEqual(path.read_bytes(), EXAMPLE.read_bytes())

    async def test_quit_saves(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            shutil.copy(EXAMPLE, path)
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                for _ in range(4):
                    await pilot.press("n")
                await pilot.pause()
                await fill(pilot, {"#website-hostname": "hpc.example.org"})
                await pilot.press("escape", "q")
                await pilot.pause()
            self.assertEqual(clusterfile.load(path).get(["cluster", "website", "hostname"]), "hpc.example.org")


class ProbeErrorTest(unittest.IsolatedAsyncioTestCase):
    """A machine that cannot be probed shows why in the machines table."""

    async def test_probe_error_in_table(self) -> None:
        error = "cannot connect with `ssh gpu9`: Connection timed out"
        fakes = Fakes({"gpu9": facts("gpu9", [], [], error)}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            shutil.copy(EXAMPLE, path)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                await add_machine(pilot, "gpu9", "gpu9")
                row = app.screen.query_one("#machine-table", DataTable).get_row("gpu9")
                self.assertIn("Connection timed out", str(row[-1]))


class UidConflictTest(unittest.IsolatedAsyncioTestCase):
    """A UID conflict: Fix shows the plan; cancel runs nothing; confirm runs apply_fix once, then re-checks."""

    async def test_fix_uid_needs_confirmation(self) -> None:
        fakes = Fakes(
            {"gpu4": facts("gpu4", ["192.168.104.11"], ["NVIDIA RTX A6000"] * 4, None)},
            {"gpu4": {"alice": (1001, 1001), "bob": None}},
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            shutil.copy(EXAMPLE, path)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                table = app.screen.query_one("#machine-table", DataTable)
                table.move_cursor(row=table.get_row_index("gpu4"))
                await pilot.press("p")
                await settle(pilot)
                self.assertEqual(fakes.probed, ["gpu4"])
                await pilot.press("n", "n")
                await pilot.pause()
                await pilot.press("c")
                await settle(pilot)
                self.assertEqual(fakes.checked, ["gpu4"])
                conflicts = app.screen.query_one("#uid-table", DataTable)
                self.assertEqual(conflicts.row_count, 1)
                self.assertIn("1001", " ".join(str(cell) for cell in conflicts.get_row("gpu4:alice")))
                conflicts.focus()
                await pilot.press("f")
                await settle(pilot)
                self.assertIn("groupmod -g 2000 alice", text(app, "#plan-text"))
                await pilot.click("#cancel")
                await settle(pilot)
                self.assertEqual(fakes.applied, [])
                await pilot.press("f")
                await settle(pilot)
                await pilot.click("#apply")
                await settle(pilot)
                self.assertEqual(len(fakes.applied), 1)
                self.assertEqual(fakes.planned, [("gpu4", "alice", 2000), ("gpu4", "alice", 2000)])
                self.assertEqual(fakes.checked, ["gpu4", "gpu4"])
                self.assertEqual(app.screen.query_one("#uid-table", DataTable).row_count, 0)


if __name__ == "__main__":
    unittest.main()
