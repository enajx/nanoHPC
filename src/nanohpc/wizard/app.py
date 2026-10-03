"""The wizard's full-screen app: the steps in a sidebar, the current step's form, and a footer with the keys."""

import sys
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Label, ListItem, ListView, Static

from nanohpc import clusterfile
from nanohpc.clusterfile import ClusterFile
from nanohpc.wizard.dialogs import NameScreen
from nanohpc.wizard.state import AGENTS_URL, STEPS, Dependencies, WizardState, error_step
from nanohpc.wizard.steps import STEP_CLASSES, Edited, GoToStep, SaveFile, Step

GLOBAL_KEYS = "n next · b back · esc leave a field · ctrl+s save · q quit (saves)"


class WizardScreen(Screen[None]):
    """The wizard's main screen."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "next", "next step"),
        Binding("b", "back", "back"),
        Binding("escape", "leave_field", "leave the field"),
    ]

    def __init__(self, state: WizardState) -> None:
        super().__init__()
        self.state = state
        self.current = 0

    def compose(self) -> ComposeResult:
        """Show the sidebar, the steps, and the footer."""
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("nanoHPC setup", id="title")
                yield ListView(*[ListItem(Label(title)) for _, title in STEPS], id="steps")
            with ContentSwitcher(initial=STEPS[0][0], id="content"):
                for index, step in enumerate(STEP_CLASSES):
                    yield step(self.state, index)
        with Vertical(id="footer"):
            yield Static("", id="keys")
            yield Static(f"Prefer an installation by an AI agent? See {AGENTS_URL}", id="agents")

    async def on_mount(self) -> None:
        """Open the first step."""
        await self.go(0)

    def step(self, index: int) -> Step:
        """Return a step's widget."""
        return self.query_one(f"#{STEPS[index][0]}", Step)

    async def go(self, index: int) -> None:
        """Show a step with the file's current content."""
        self.current = index
        self.query_one("#content", ContentSwitcher).current = STEPS[index][0]
        self.query_one("#steps", ListView).index = index
        step = self.step(index)
        await step.reload()
        step.focus_first()
        keys = " · ".join(part for part in (step.KEYS, GLOBAL_KEYS) if part)
        self.query_one("#keys", Static).update(keys)
        self.refresh_status()

    def refresh_status(self) -> None:
        """Mark the current step and the steps with validation errors in the sidebar."""
        counts = [0] * len(STEPS)
        for error in self.state.file.validate():
            counts[error_step(error)] += 1
        for index, item in enumerate(self.query_one("#steps", ListView).query(ListItem)):
            mark = "■" if index == self.current else "□"
            errors = f"  ✗{counts[index]}" if counts[index] else ""
            item.query_one(Label).update(f"{mark} {index + 1} {STEPS[index][1]}{errors}")
            item.set_class(index == self.current, "current")
            item.set_class(counts[index] > 0, "has-errors")

    async def action_next(self) -> None:
        """Open the next step."""
        if self.current < len(STEPS) - 1:
            await self.go(self.current + 1)

    async def action_back(self) -> None:
        """Open the step before."""
        if self.current > 0:
            await self.go(self.current - 1)

    def action_leave_field(self) -> None:
        """Leave the focused field, so the step keys work again."""
        self.set_focus(None)

    @on(ListView.Selected, "#steps")
    async def step_chosen(self, event: ListView.Selected) -> None:
        """Open the step chosen in the sidebar."""
        index = event.list_view.index
        if index is not None and index != self.current:
            await self.go(index)

    def on_edited(self, event: Edited) -> None:
        """Update the sidebar after a change."""
        self.refresh_status()

    async def on_go_to_step(self, event: GoToStep) -> None:
        """Open the step an error belongs to."""
        await self.go(event.index)


class WizardApp(App[int]):
    """`nanohpc init`: write a new cluster.yml, or open an existing one to change it.

    The wizard only reads the machines (probe, UID check, fix-uid plan) except for fix-uid, which changes a
    machine only after the administrator confirms its plan."""

    CSS_PATH = "wizard.tcss"
    TITLE = "nanoHPC setup"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit_wizard", "quit (saves)"),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, path: Path, ssh_config: Path | None, deps: Dependencies) -> None:
        super().__init__()
        self.path = path
        self.ssh_config = ssh_config
        self.deps = deps
        # Read before the app starts, so an unreadable file is reported in the terminal.
        self.opened: ClusterFile | None = clusterfile.load(path) if path.exists() else None
        self.wizard: WizardState | None = None

    @property
    def state(self) -> WizardState:
        """Return the wizard's state (once the file is open)."""
        if self.wizard is None:
            raise RuntimeError("no cluster.yml is open yet")
        return self.wizard

    def on_mount(self) -> None:
        """Open the file, or ask the new cluster's name first."""
        self.theme = "textual-light"
        if self.opened is not None:
            self.open(self.opened)
        else:
            self.push_screen(NameScreen(), self.named)

    def named(self, name: str | None) -> None:
        """Start a new file for the named cluster; no name quits without writing anything."""
        if name is None:
            self.exit(1)
            return
        self.open(clusterfile.new(name))

    def open(self, file: ClusterFile) -> None:
        """Show the steps for the file."""
        self.wizard = WizardState(self.path, self.ssh_config, self.deps, file, {}, {}, set(), {}, {})
        self.push_screen(WizardScreen(self.wizard))

    def action_save(self) -> None:
        """Write the file to disk."""
        if self.wizard is None:
            return
        self.wizard.file.save(self.path)
        errors = len(self.wizard.file.validate())
        note = "" if not errors else f" ({errors} error(s) left: see Review)"
        self.notify(f"Saved {self.path}{note}")

    def on_save_file(self, event: SaveFile) -> None:
        """Save when a step asks."""
        self.action_save()

    def action_quit_wizard(self) -> None:
        """Save the file (when one is open) and quit."""
        if self.wizard is not None:
            self.wizard.file.save(self.path)
        self.exit(0)


def run(path: Path, ssh_config: Path | None, deps: Dependencies) -> int:
    """Run the wizard on a cluster.yml (created if missing) and return the exit code."""
    if path.exists() and not path.is_file():
        print(f"{path}: not a file", file=sys.stderr)
        return 1
    if not path.parent.is_dir():
        print(f"{path.parent}: folder not found", file=sys.stderr)
        return 1
    code = WizardApp(path, ssh_config, deps).run()
    if code is None:
        return 1
    if code == 0:
        print(f"Saved {path}. Next: ssh-add (load your key), then nanohpc deploy {path}")
    return code
