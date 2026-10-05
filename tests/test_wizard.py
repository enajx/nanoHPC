"""Tests for `nanohpc init`, the setup wizard, driven headless with Textual's Pilot.

The machines are FAKES: probe_machine, user_ids, uid_problems, uid_owner, plan_fix, and apply_fix are replaced by
the fakes below (the wizard takes them as a Dependencies object), so no SSH runs. The real functions are tested
in test_probe.py and test_fixuid.py.
"""

import dataclasses
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, DataTable, Input, Label, ListView, Select, Static, TextArea

from nanohpc.config import load_config
from nanohpc.fixuid import FixPlan
from nanohpc.probe import Disk, MachineFacts
from nanohpc.wizard import Dependencies, WizardApp
from nanohpc.wizard.app import WizardScreen
from nanohpc.wizard.dialogs import AddressScreen, ChoiceScreen, MessageScreen, ProblemsScreen, WorkingScreen
from nanohpc.wizard.state import disk_problem, scratch_warning

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cluster.yml"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReplaceWithYourOwnPublicKey alice@laptop"
SIZE = (130, 45)
ROOT_DISK = Disk("/dev/nvme0n1", 512.1, None, None, "Samsung SSD", "disk", "gpt", ["/dev/nvme0n1p1"], True)
ROOT_PART = Disk("/dev/nvme0n1p1", 512.0, "ext4", "/", None, "part", None, [], True)
EMPTY_DISK = Disk("/dev/vdb", 2000.4, None, None, "WD Red", "disk", None, [], False)


def facts(target: str, addresses: list[str], gpus: list[str], error: str | None) -> MachineFacts:
    """FAKE probe result: a 32-CPU, 128 GiB Ubuntu 24.04 machine with a root disk and an empty spare disk."""
    if error is not None:
        return MachineFacts(target, "", [], 0, None, None, None, 0, [], [], "", "", False, [], error, 0.0, False)
    disks = [ROOT_DISK, ROOT_PART, EMPTY_DISK]
    return MachineFacts(
        target, target, addresses, 32, 1, 16, 2, 131072, gpus, disks, "ubuntu", "24.04", True, [], None, 400.0, True
    )


class Fakes:
    """FAKE machines: canned probe results by ssh target (one per call, the last one repeats), canned user IDs,
    UID problems, and UID owners, and a canned fix-uid plan. Every call is recorded."""

    def __init__(
        self, machines: dict[str, list[MachineFacts]], ids: dict[str, dict[str, tuple[int, int] | None]]
    ) -> None:
        self.machines = machines
        self.ids = ids
        self.problems: dict[str, list[str]] = {}
        self.owners: dict[str, set[int]] = {}
        self.probed: list[str] = []
        self.checked: list[str] = []
        self.planned: list[tuple[str, str, int]] = []
        self.applied: list[tuple[str, FixPlan]] = []
        self.plan_reasons: list[str] = []
        self.apply_error: str | None = None
        self.release = threading.Event()
        self.release.set()

    def probe_machine(self, target: str, ssh_config: Path | None) -> MachineFacts:
        """FAKE probe_machine."""
        self.probed.append(target)
        results = self.machines[target]
        return results.pop(0) if len(results) > 1 else results[0]

    def user_ids(self, target: str, ssh_config: Path | None, names: list[str]) -> dict[str, tuple[int, int] | None]:
        """FAKE user_ids."""
        self.checked.append(target)
        found = self.ids.get(target, {})
        return {name: found.get(name) for name in names}

    def uid_problems(self, target: str, ssh_config: Path | None, users: list[tuple[str, int]]) -> list[str]:
        """FAKE uid_problems."""
        return self.problems.get(target, [])

    def uid_owner(self, target: str, ssh_config: Path | None, uid: int) -> str | None:
        """FAKE uid_owner."""
        return "someone" if uid in self.owners.get(target, set()) else None

    def plan_fix(self, target: str, ssh_config: Path | None, user: str, uid: int) -> FixPlan:
        """FAKE plan_fix: waits for `release`, then returns a plan that can proceed (unless plan_reasons)."""
        self.release.wait(10)
        self.planned.append((target, user, uid))
        old = self.ids[target][user]
        assert old is not None
        commands = [["groupmod", "-g", str(uid), user], ["usermod", "-u", str(uid), user]]
        ok = not self.plan_reasons
        return FixPlan(
            target,
            user,
            uid,
            old[0],
            old[1],
            user,
            ok,
            self.plan_reasons,
            [],
            ["/"],
            3,
            ["/home/[/x]"],
            False,
            commands,
        )

    def apply_fix(self, target: str, ssh_config: Path | None, plan: FixPlan) -> list[str]:
        """FAKE apply_fix: the user now has the new UID on that machine (or it raises apply_error)."""
        self.applied.append((target, plan))
        if self.apply_error is not None:
            raise RuntimeError(self.apply_error)
        self.ids[target][plan.user] = (plan.new_id, plan.new_id)
        return [f"ran as root on {target}: groupmod", f"{plan.user} on {target} now has UID {plan.new_id}"]

    def dependencies(self) -> Dependencies:
        """Return the fakes as the wizard's dependencies."""
        return Dependencies(
            self.probe_machine, self.user_ids, self.uid_problems, self.uid_owner, self.plan_fix, self.apply_fix
        )


async def settle(pilot: Pilot) -> None:
    """Wait for the wizard's workers (probes, checks) and the screen updates they cause."""
    for _ in range(2):
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def text(app: WizardApp, selector: str) -> str:
    """Return the text shown by a Static widget."""
    return str(app.screen.query_one(selector, Static).render())


async def fill(pilot: Pilot, values: dict[str, str]) -> None:
    """Type values into inputs of the screen on top."""
    for selector, value in values.items():
        pilot.app.screen.query_one(selector, Input).value = value
    await pilot.pause()


async def go(pilot: Pilot, step: int) -> None:
    """Open a step (0 to 6) through the sidebar's keys."""
    screen = pilot.app.screen
    assert isinstance(screen, WizardScreen)
    await pilot.press("escape")
    while screen.current != step:
        await pilot.press("n" if screen.current < step else "b")
    await settle(pilot)


async def select_row(pilot: Pilot, table_id: str, key: str) -> None:
    """Put the cursor of a table on a row and focus the table."""
    table = pilot.app.screen.query_one(table_id, DataTable)
    table.move_cursor(row=table.get_row_index(key))
    table.focus()
    await pilot.pause()


async def add_machine(pilot: Pilot, name: str, target: str) -> None:
    """Add a machine from the Machines step and wait for its probe."""
    await pilot.press("a")
    await fill(pilot, {"#machine-name": name, "#machine-target": target})
    await pilot.click("#ok")
    await settle(pilot)


async def answer(pilot: Pilot, choice: str) -> None:
    """Answer the choice dialog on top."""
    assert isinstance(pilot.app.screen, ChoiceScreen), pilot.app.screen
    await pilot.click(f"#choice-{choice}")
    await settle(pilot)


def example_copy(folder: str) -> Path:
    """Copy examples/cluster.yml into a folder and return its path."""
    path = Path(folder) / "cluster.yml"
    shutil.copy(EXAMPLE, path)
    return path


class NewClusterTest(unittest.IsolatedAsyncioTestCase):
    """A whole new cluster through every step gives a valid cluster.yml."""

    async def test_new_cluster_through_all_steps(self) -> None:
        fakes = Fakes(
            {
                "10.0.0.10": [facts("10.0.0.10", ["10.0.0.10"], [], None)],
                "gpu1": [facts("gpu1", ["172.16.0.11", "10.0.0.11"], ["NVIDIA RTX A6000"] * 4, None)],
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
                # gpu1 has two addresses: the wizard asks which one is on the cluster network.
                self.assertIsInstance(app.screen, AddressScreen)
                app.screen.query_one("#address", Select).value = "10.0.0.11"
                await pilot.click("#ok")
                await settle(pilot)
                table = app.screen.query_one("#machine-table", DataTable)
                self.assertIn("✓ 32c", str(table.get_row("gpu1")[-1]))
                self.assertEqual(app.state.file.machines()["gpu1"]["address"], "10.0.0.11")
                # Edit gpu1: the GPU type is suggested from the probe.
                await select_row(pilot, "#machine-table", "gpu1")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#gpu-type", Input).value, "a6000")
                self.assertEqual(app.screen.query_one("#gpu-count", Input).value, "4")
                await pilot.click("#ok")
                await pilot.pause()
                self.assertEqual(app.state.file.machines()["front"]["roles"], ["front", "home"])
                # 2 Storage: /home on the front node's root disk; scratch on gpu1 in an image file.
                await go(pilot, 1)
                self.assertEqual(app.screen.query_one("#home-machine", Select).value, "front")
                app.screen.query_one("#home-device", Select).value = "/dev/vdb"
                await settle(pilot)
                self.assertIn("sudo mkfs.ext4 -O quota /dev/vdb", text(app, "#home-advice"))
                self.assertIn("never formats a disk", text(app, "#home-advice"))
                app.screen.query_one("#home-device", Select).value = "root"
                await settle(pilot)
                app.screen.query_one("#scratch-gpu1", Select).value = "image"
                await settle(pilot)
                self.assertIn("400 GB free", text(app, "#scratch-gb-gpu1-note"))
                await fill(pilot, {"#scratch-gb-gpu1": "200"})
                # 3 Users: one administrator, UID suggested from 2000.
                await go(pilot, 2)
                await pilot.press("a")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#user-uid", Input).value, "2000")
                await fill(pilot, {"#user-name": "alice"})
                app.screen.query_one("#user-keys", TextArea).text = KEY
                app.screen.query_one("#user-admin", Checkbox).value = True
                await pilot.click("#ok")
                await settle(pilot)
                self.assertIn("10.0.0.10", fakes.checked)  # the UID check ran by itself after the change
                # 4 Partitions: the defaults; 5 Website: the placeholder hostname is an error until changed.
                await go(pilot, 3)
                self.assertEqual(app.screen.query_one("#partition-table", DataTable).row_count, 1)
                await go(pilot, 6)
                self.assertIn("placeholder", text(app, "#errors ListItem Label"))
                await go(pilot, 4)
                await fill(pilot, {"#website-hostname": "cluster.lab.example.org"})
                # 7 Review: no errors, then save.
                await go(pilot, 6)
                self.assertEqual(len(app.screen.query_one("#errors", ListView).children), 0)
                self.assertIn("ssh-add -c", text(app, "#next-steps"))
                self.assertIn(f"nanohpc deploy {path}", text(app, "#next-steps"))
                await pilot.click("#save")
                await settle(pilot)
                self.assertTrue(app.saved)
                self.assertEqual(app.saved_errors, 0)
            config, errors = load_config(path, True, False)
            self.assertEqual(errors, [])
            self.assertEqual(config["cluster"]["admins"], ["alice"])
            gpu1 = config["machines"]["gpu1"]
            self.assertEqual(gpu1["gpu"], {"type": "a6000", "count": 4})
            self.assertEqual(gpu1["cpu"], {"sockets": 1, "cores_per_socket": 16, "threads_per_core": 2})
            self.assertEqual(gpu1["scratch"], {"device": None, "image_gb": 200})
            self.assertEqual(config["machines"]["front"]["home"], {"device": None})


class MonitorWizardTest(unittest.IsolatedAsyncioTestCase):
    """Monitor setup shows only machine, user, website, alert, and review steps."""

    async def test_new_monitor_file_through_wizard(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "monitor.yml"
            app = WizardApp(path, None, Fakes({}, {}).dependencies(), "monitor")
            async with app.run_test(size=SIZE) as pilot:
                await fill(pilot, {"#cluster-name": "lab"})
                await pilot.click("#create")
                await settle(pilot)
                sidebar = app.screen.query_one("#steps", ListView)
                self.assertEqual(len(sidebar.children), 5)
                self.assertNotIn(
                    "Partitions", " ".join(str(item.query_one(Label).render()) for item in sidebar.children)
                )
                await fill(pilot, {"#monitor-machine-name": "host", "#monitor-machine-address": "192.168.1.10"})
                await pilot.click("#monitor-machine-add")
                await settle(pilot)
                app.screen.query_one("#monitor-host", Select).value = "host"
                await go(pilot, 1)
                await fill(pilot, {"#monitor-users": "alice, bob"})
                await go(pilot, 2)
                await fill(pilot, {"#website-hostname": "cluster.lab.example.org"})
                await go(pilot, 4)
                self.assertEqual(
                    len(app.screen.query_one("#errors", ListView).children), 0, app.state.file.validate_in(path.parent)
                )
                self.assertIn("deploy-monitor", text(app, "#next-steps"))
                await pilot.click("#save")
                await settle(pilot)
            config, errors = load_config(path, True, False)
            self.assertEqual(errors, [])
            self.assertEqual(config["users"], ["alice", "bob"])
            self.assertEqual(config["cluster"]["monitor_host"], "host")

    async def test_existing_monitor_file_uses_monitor_steps(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "monitor.yml"
            shutil.copy(ROOT / "examples" / "monitor.yml", path)
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                self.assertEqual(len(app.state.steps), 5)
                self.assertEqual(app.state.file.get(["cluster", "mode"]), "monitor")
                await go(pilot, 1)
                self.assertEqual(app.screen.query_one("#monitor-users", Input).value, "alice, bob")
                await pilot.press("q")
                await settle(pilot)


class SaveAndQuitTest(unittest.IsolatedAsyncioTestCase):
    """The file is written only when it changed and the administrator saves; quitting asks about changes."""

    async def test_untouched_file_is_shown_and_not_rewritten(self) -> None:
        fakes = Fakes({}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            before = path.stat()
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                table = app.screen.query_one("#machine-table", DataTable)
                self.assertEqual(
                    [str(key.value) for key in table.rows], ["front", "gpu4", "gpu2", "cpu1", "gpu4i", "store"]
                )
                self.assertEqual(str(table.get_row("gpu4")[-1]), "not probed")
                await go(pilot, 2)
                users = app.screen.query_one("#user-table", DataTable)
                self.assertEqual([str(key.value) for key in users.rows], ["alice", "bob"])
                await go(pilot, 6)
                self.assertEqual(len(app.screen.query_one("#errors", ListView).children), 0)
                await pilot.press("q")
                await pilot.pause()
            self.assertFalse(app.saved)
            self.assertEqual(fakes.probed, [])
            self.assertEqual(path.read_bytes(), EXAMPLE.read_bytes())
            self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    async def test_quit_asks_about_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await go(pilot, 4)
                await fill(pilot, {"#website-hostname": "hpc.example.org"})
                await pilot.press("escape", "q")
                await pilot.pause()
                await answer(pilot, "cancel")
                self.assertIsInstance(app.screen, WizardScreen)
                await pilot.press("ctrl+q")
                await pilot.pause()
                await answer(pilot, "discard")
            self.assertEqual(path.read_bytes(), EXAMPLE.read_bytes())

    async def test_save_keeps_mode_and_symlink_and_asks_when_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = example_copy(folder)
            os.chmod(target, 0o600)
            link = Path(folder) / "link.yml"
            link.symlink_to(target)
            app = WizardApp(link, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await go(pilot, 4)
                await fill(pilot, {"#website-hostname": ""})
                await pilot.press("escape", "ctrl+s")
                await pilot.pause()
                self.assertIn("Save anyway", str(app.screen.query_one("#choice-yes", Button).label))
                await answer(pilot, "no")
                self.assertEqual(target.read_bytes(), EXAMPLE.read_bytes())
                await fill(pilot, {"#website-hostname": "hpc.example.org"})
                await pilot.press("escape", "q")
                await pilot.pause()
                await answer(pilot, "save")
            self.assertTrue(link.is_symlink())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            text_now = target.read_text()
            self.assertIn("hostname: hpc.example.org", text_now)
            # The hostname kept its place: first in website, as in the example.
            self.assertLess(text_now.index("hostname:"), text_now.index("https:"))
            self.assertTrue(app.saved)
            self.assertEqual(app.saved_errors, 0)


class PlainTextTest(unittest.IsolatedAsyncioTestCase):
    """Text from the file and the machines is shown as it is, never read as markup."""

    async def test_markup_like_names_are_shown_as_text(self) -> None:
        error = "[/x] cannot connect [bold]"
        fakes = Fakes(
            {"gpu4": [facts("gpu4", [], [], error)], "gpu2": [facts("gpu2", ["192.168.104.12"], [], None)]},
            {"gpu2": {"alice": (1001, 1001), "bob": None}},
        )
        fakes.problems = {"gpu2": ["the group [/x] has group ID 5"]}
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "[").mkdir()
            path = Path(folder) / "[" / "x] notes.yml"
            shutil.copy(EXAMPLE, path)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                for machine in ("gpu4", "gpu2"):
                    await select_row(pilot, "#machine-table", machine)
                    await pilot.press("p")
                    await settle(pilot)
                await select_row(pilot, "#machine-table", "gpu4")
                self.assertIn(error, text(app, "#machine-facts"))
                self.assertIn(error, str(app.screen.query_one("#machine-table", DataTable).get_row("gpu4")[-1]))
                for step in range(1, 7):
                    await go(pilot, step)
                    if step == 2:
                        self.assertIn("[/x]", text(app, "#uid-problems"))
                await go(pilot, 4)
                await fill(pilot, {"#website-hostname": "hpc.example.org"})
                await pilot.press("escape", "ctrl+s")
                await settle(pilot)
                await go(pilot, 2)
                await select_row(pilot, "#uid-table", "conflict-0")
                await pilot.press("f")
                await settle(pilot)
                self.assertIn("/home/[/x]", text(app, "#plan-text"))
                await pilot.click("#cancel")
                await pilot.press("q")
                await pilot.pause()
            self.assertIn("hpc.example.org", path.read_text())


class ReviewTest(unittest.IsolatedAsyncioTestCase):
    """The review says valid only when `nanohpc validate` would pass: website files and .env secrets too."""

    async def test_missing_logo_and_slack_secret_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await go(pilot, 4)
                await fill(pilot, {"#website-logo": "logo.png"})
                await go(pilot, 5)
                await pilot.click("#alerts-slack")
                await go(pilot, 6)
                errors = [str(item.name) for item in app.screen.query_one("#errors", ListView).query("ListItem")]
                self.assertEqual(len(errors), 2, errors)
                self.assertIn("cluster.website.logo", errors[0])
                self.assertIn("NANOHPC_SLACK_WEBHOOK", errors[1])
                (Path(folder) / "logo.png").write_bytes(b"png")
                (Path(folder) / ".env").write_text("NANOHPC_SLACK_WEBHOOK=https://hooks.slack.com/services/x\n")
                await go(pilot, 5)
                await go(pilot, 6)
                self.assertEqual(len(app.screen.query_one("#errors", ListView).children), 0)
                self.assertIn("is valid", text(app, ".ok"))


class UidTest(unittest.IsolatedAsyncioTestCase):
    """The UID check runs by itself on probed machines; Fix needs a saved, valid file and a confirmation."""

    def fakes(self) -> Fakes:
        """FAKE machines: gpu4, where alice has UID 1001 and bob's UID belongs to another account."""
        fakes = Fakes(
            {"gpu4": [facts("gpu4", ["192.168.104.11"], ["NVIDIA RTX A6000"] * 4, None)]},
            {"gpu4": {"alice": (1001, 1001), "bob": None, "carol": (3000, 3000)}},
        )
        fakes.problems = {"gpu4": ["UID 2001 (for bob in cluster.yml) already belongs to eve on gpu4"]}
        fakes.owners = {"gpu4": {2002}}
        return fakes

    async def probe_gpu4(self, pilot: Pilot) -> None:
        """Probe gpu4 from the Machines step."""
        await select_row(pilot, "#machine-table", "gpu4")
        await pilot.press("p")
        await settle(pilot)

    async def test_check_runs_by_itself_and_fix_needs_confirmation(self) -> None:
        fakes = self.fakes()
        fakes.release.clear()
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await self.probe_gpu4(pilot)
                self.assertEqual(fakes.checked, ["gpu4"])  # after the probe, without pressing c
                await go(pilot, 2)
                self.assertIn("already belongs to eve", text(app, "#uid-problems"))
                conflicts = app.screen.query_one("#uid-table", DataTable)
                self.assertEqual(conflicts.row_count, 1)
                self.assertIn("1001", " ".join(str(cell) for cell in conflicts.get_row("conflict-0")))
                # Fix only for a saved file.
                app.state.file.set_value(["cluster", "website", "hostname"], "hpc.example.org")
                await select_row(pilot, "#uid-table", "conflict-0")
                await pilot.press("f")
                await settle(pilot)
                self.assertEqual(fakes.planned, [])
                await pilot.press("ctrl+s")
                await settle(pilot)
                # The plan is read while a working dialog shows.
                await select_row(pilot, "#uid-table", "conflict-0")
                await pilot.press("f")
                await pilot.pause()
                self.assertIsInstance(app.screen, WorkingScreen)
                fakes.release.set()
                await settle(pilot)
                self.assertIn("groupmod -g 2000 alice", text(app, "#plan-text"))
                await pilot.click("#cancel")
                await settle(pilot)
                self.assertEqual(fakes.applied, [])
                await select_row(pilot, "#uid-table", "conflict-0")
                await pilot.press("f")
                await settle(pilot)
                await pilot.click("#apply")
                await settle(pilot)
                self.assertEqual(len(fakes.applied), 1)
                self.assertIsInstance(app.screen, MessageScreen)
                self.assertIn("now has UID 2000", text(app, "#messages"))
                await pilot.click("#cancel")
                await settle(pilot)
                self.assertEqual(app.screen.query_one("#uid-table", DataTable).row_count, 0)
                self.assertEqual(fakes.checked.count("gpu4"), 2)  # after the probe, and after the fix
                # Removing the machine forgets its check.
                await go(pilot, 0)
                await select_row(pilot, "#machine-table", "gpu4")
                await pilot.press("d")
                await pilot.pause()
                await answer(pilot, "yes")
                self.assertNotIn("gpu4", app.state.uid_checks)
                self.assertNotIn("gpu4", app.state.facts)

    async def test_failed_apply_shows_what_is_known(self) -> None:
        fakes = self.fakes()
        fakes.apply_error = "ssh: connection lost [/x]"
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await self.probe_gpu4(pilot)
                await go(pilot, 2)
                await select_row(pilot, "#uid-table", "conflict-0")
                await pilot.press("f")
                await settle(pilot)
                await pilot.click("#apply")
                await settle(pilot)
                self.assertIsInstance(app.screen, MessageScreen)
                self.assertIn("connection lost [/x]", text(app, "#messages"))
                self.assertIn("usermod -u 2000 alice", text(app, "#messages"))

    async def test_new_user_uid_suggestion(self) -> None:
        fakes = self.fakes()
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await self.probe_gpu4(pilot)
                await go(pilot, 2)
                for name, uid in (("carol", "3000"), ("dave", "2003")):
                    await pilot.press("a")
                    await pilot.pause()
                    app.screen.query_one("#user-name", Input).focus()
                    await fill(pilot, {"#user-name": name})
                    await pilot.press("enter")
                    await settle(pilot)
                    self.assertEqual(app.screen.query_one("#user-uid", Input).value, uid, name)
                    await pilot.press("escape")
                    await pilot.pause()


class BackupTest(unittest.IsolatedAsyncioTestCase):
    """Clearing the backup target keeps the backup's other settings; only "No backup" removes them."""

    async def test_clear_target_then_no_backup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await go(pilot, 5)
                await fill(pilot, {"#backup-to": ""})
                backup = app.state.file.get(["backup"])
                self.assertEqual(backup, {"time": "03:00", "exclude": [".cache/", ".venv/", "__pycache__/"]})
                await pilot.click("#no-backup")
                await pilot.pause()
                await answer(pilot, "yes")
                self.assertIsNone(app.state.file.get(["backup"]))


class InvalidFileTest(unittest.IsolatedAsyncioTestCase):
    """Files the wizard cannot edit show why; odd machine names do not break the steps."""

    async def test_machine_name_with_a_dot(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cluster.yml"
            path.write_text(EXAMPLE.read_text().replace("  gpu2:\n", "  gpu.2:\n"))
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await go(pilot, 1)
                self.assertEqual(len(app.screen.query("#scratch-x6770752e32")), 1)
                await go(pilot, 3)
                await go(pilot, 6)
                self.assertIn("gpu.2", text(app, "#errors ListItem Label"))

    async def test_dates_and_scalar_sections_are_listed(self) -> None:
        for change, expected in (
            ("nanohpc_version: null           # the", "nanohpc_version: 2024-01-01 # the"),
            ("policy:\n  max_submit_jobs_per_user: 30\n", "policy: 5\nold_policy:\n  max_submit_jobs_per_user: 30\n"),
        ):
            with self.subTest(change=expected), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "cluster.yml"
                path.write_text(EXAMPLE.read_text().replace(change, expected))
                app = WizardApp(path, None, Fakes({}, {}).dependencies())
                async with app.run_test(size=SIZE) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ProblemsScreen)
                    problems = text(app, "#problems")
                    self.assertIn("nanohpc_version" if "2024" in expected else "policy must be a mapping", problems)
                    await pilot.click("#quit")
                self.assertEqual(app.return_value, 1)


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    """Probe errors show with a hint and can be retried; an unsupported Ubuntu is a warning."""

    async def test_error_hint_retry_and_unsupported_ubuntu(self) -> None:
        host_key = "Host key verification failed."
        old = dataclasses.replace(facts("gpu9", ["10.0.0.9"], [], None), ubuntu="20.04", supported=False)
        fakes = Fakes({"gpu9": [facts("gpu9", [], [], host_key), old]}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                await add_machine(pilot, "gpu9", "gpu9")
                table = app.screen.query_one("#machine-table", DataTable)
                self.assertIn(host_key, str(table.get_row("gpu9")[-1]))
                self.assertIn("Accept the machine's host key", text(app, "#machine-facts"))
                await select_row(pilot, "#machine-table", "gpu9")
                await pilot.press("p")
                await settle(pilot)
                self.assertEqual(fakes.probed, ["gpu9", "gpu9"])
                self.assertIn("20.04 not supported", str(table.get_row("gpu9")[-1]))


class PreparationTest(unittest.IsolatedAsyncioTestCase):
    """After a probe, the Machines step lists what to prepare on the machine; skipped items show in Review."""

    async def test_checklist_and_skip(self) -> None:
        no_driver = dataclasses.replace(facts("gpu4", ["192.168.104.11"], [], None), sudo_ok=False)
        fakes = Fakes({"gpu4": [no_driver]}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await select_row(pilot, "#machine-table", "gpu4")
                await pilot.press("p")
                await settle(pilot)
                await select_row(pilot, "#machine-table", "gpu4")
                checklist = "\n".join(str(item.render()) for item in app.screen.query("#checklist Static"))
                self.assertIn("✓ SSH works", checklist)
                self.assertIn("i sudo asks for a password: the first nanohpc deploy asks for it once", checklist)
                self.assertIn("✗ NVIDIA driver", checklist)
                self.assertIn("sudo mkfs.ext4 /dev/vdb", checklist)  # the scratch disk in the file is empty
                app.screen.query_one("#skip-nvidia", Button).press()
                await settle(pilot)
                await go(pilot, 6)
                preparation = text(app, "#preparation")
                self.assertIn(
                    "gpu4: NVIDIA driver works (nvidia-smi lists the GPUs): skipped, to do later", preparation
                )
                self.assertNotIn("sudo", preparation)
                self.assertIn("front: not probed", preparation)


class StorageTest(unittest.IsolatedAsyncioTestCase):
    """Disks in use are never offered a mkfs; an image larger than the free space is refused."""

    def test_disk_rules(self) -> None:
        machine = facts("gpu4", ["10.0.0.4"], [], None)
        used = dataclasses.replace(machine, disks=[ROOT_DISK, ROOT_PART])
        problem = disk_problem(used, "/dev/nvme0n1", False)
        assert problem is not None
        self.assertIn("in use", problem)
        self.assertNotIn("mkfs", problem)
        self.assertIn("in use", str(disk_problem(used, "/dev/nvme0n1p1", True)))
        ext4 = Disk("/dev/vdc", 100.0, "ext4", None, None, "disk", None, [], True)
        with_ext4 = dataclasses.replace(machine, disks=[ext4])
        self.assertIsNone(disk_problem(with_ext4, "/dev/vdc", False))
        warning = str(scratch_warning(with_ext4, "/dev/vdc", None, None))
        self.assertIn("unused for 14 days", warning)
        self.assertIn("7 days after their jobs ended", warning)

    async def test_image_size_is_limited_by_free_space(self) -> None:
        small = dataclasses.replace(facts("gpu2", ["192.168.104.12"], [], None), free_gb=50.0)
        fakes = Fakes({"gpu2": [small]}, {})
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, fakes.dependencies())
            async with app.run_test(size=SIZE) as pilot:
                await select_row(pilot, "#machine-table", "gpu2")
                await pilot.press("p")
                await settle(pilot)
                await go(pilot, 1)
                self.assertIn("50 GB free", text(app, "#scratch-gb-gpu2-note"))
                await fill(pilot, {"#scratch-gb-gpu2": "80"})
                self.assertIn("refused", text(app, "#scratch-gb-gpu2-note"))
                self.assertEqual(app.state.file.get(["machines", "gpu2", "scratch", "image_gb"]), 2)
                await fill(pilot, {"#scratch-gb-gpu2": "40"})
                self.assertEqual(app.state.file.get(["machines", "gpu2", "scratch", "image_gb"]), 40)


class SmallTerminalTest(unittest.IsolatedAsyncioTestCase):
    """At 80x24 the footer, with the line about SETUP-for-AGENTS.md, shows on every step; the sidebar marks
    visited steps and steps with errors."""

    async def test_footer_and_sidebar_at_80x24(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = example_copy(folder)
            app = WizardApp(path, None, Fakes({}, {}).dependencies())
            async with app.run_test(size=(80, 24)) as pilot:
                for step in range(7):
                    await go(pilot, step)
                    agents = app.screen.query_one("#agents", Static)
                    self.assertIn("SETUP-for-AGENTS.md", str(agents.render()))
                    self.assertGreater(agents.region.height, 0)
                    self.assertLessEqual(agents.region.bottom, 23, step)
                await go(pilot, 4)
                await fill(pilot, {"#website-hostname": ""})
                await go(pilot, 0)
                labels = [str(label.render()) for label in app.screen.query("#steps Label").results(Label)]
                self.assertTrue(labels[0].startswith("■ 1"))
                self.assertTrue(labels[3].startswith("✓ 4"))
                self.assertIn("✗1", labels[4])


if __name__ == "__main__":
    unittest.main()
