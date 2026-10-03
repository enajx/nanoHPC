"""The wizard's full-screen app: the steps in a sidebar, the current step's form, and a footer with the keys."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from ruamel.yaml.comments import CommentedMap
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Label, ListItem, ListView, Static

from nanohpc import clusterfile
from nanohpc.clusterfile import ClusterFile
from nanohpc.wizard.common import Edited, GoToStep, Probed, SaveFile, Step
from nanohpc.wizard.dialogs import ChoiceScreen, NameScreen, ProblemsScreen
from nanohpc.wizard.state import (
    AGENTS_URL,
    STEPS,
    USERS,
    Dependencies,
    WizardState,
    error_step,
    new_state,
    save_text,
    shape_problems,
    wizard_errors,
)
from nanohpc.wizard.steps import STEP_CLASSES
from nanohpc.wizard.users import UsersStep

GLOBAL_KEYS = "n next · b back · esc leave field · ctrl+s save · q quit"


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
        self.visited: set[int] = set()

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
            yield Static("", id="keys", markup=False)
            yield Static(f"AI agent install? See {AGENTS_URL}", id="agents", markup=False)

    async def on_mount(self) -> None:
        """Open the first step."""
        await self.go(0)

    def step(self, index: int) -> Step:
        """Return a step's widget."""
        return self.query_one(f"#{STEPS[index][0]}", Step)

    async def go(self, index: int) -> None:
        """Show a step with the file's current content."""
        self.current = index
        self.visited.add(index)
        self.query_one("#content", ContentSwitcher).current = STEPS[index][0]
        self.query_one("#steps", ListView).index = index
        step = self.step(index)
        await step.reload()
        step.focus_first()
        step.entered()
        keys = " · ".join(part for part in (step.KEYS, GLOBAL_KEYS) if part)
        self.query_one("#keys", Static).update(Text(keys))
        self.refresh_status()

    def refresh_status(self) -> None:
        """Mark the current step, the visited steps, and the steps with errors in the sidebar."""
        counts = [0] * len(STEPS)
        for error in wizard_errors(self.state):
            counts[error_step(error)] += 1
        for index, item in enumerate(self.query_one("#steps", ListView).query(ListItem)):
            mark = "■" if index == self.current else "✓" if index in self.visited else "□"
            errors = f" ✗{counts[index]}" if counts[index] else ""
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

    def on_probed(self, event: Probed) -> None:
        """Check the UIDs on a machine that was just probed."""
        self.query_one(f"#{STEPS[USERS][0]}", UsersStep).check([event.machine])

    async def on_go_to_step(self, event: GoToStep) -> None:
        """Open the step an error belongs to."""
        await self.go(event.index)


class WizardApp(App[int]):
    """`nanohpc init`: write a new cluster.yml, or open an existing one to change it.

    The wizard only reads the machines (probe, UID check, fix-uid plan) except for fix-uid, which changes a
    machine only after the administrator confirms its plan. The file is written only when the administrator
    saves (ctrl+s, the Save button, or Save when quitting), and only when it changed."""

    CSS_PATH = "wizard.tcss"
    TITLE = "nanoHPC setup"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit_wizard", "quit"),
        Binding("ctrl+q", "quit_wizard", "quit", priority=True),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, path: Path, ssh_config: Path | None, deps: Dependencies) -> None:
        super().__init__()
        self.path = path
        self.ssh_config = ssh_config
        self.deps = deps
        self.wizard: WizardState | None = None
        self.saved = False  # the file was written in this session
        self.saved_errors = 0  # errors of the file as last saved
        # Read before the app starts, so an unreadable file (not YAML) is reported in the terminal.
        self.opened: ClusterFile | None = None
        self.problems: list[str] = []
        if path.exists():
            data = clusterfile.yaml_handler().load(path.read_text())
            self.problems = shape_problems(data)
            if isinstance(data, CommentedMap) and not self.problems:
                self.opened = ClusterFile(data)

    @property
    def state(self) -> WizardState:
        """Return the wizard's state (once the file is open)."""
        if self.wizard is None:
            raise RuntimeError("no cluster.yml is open yet")
        return self.wizard

    def on_mount(self) -> None:
        """Open the file, or say why it cannot be opened, or ask the new cluster's name first."""
        self.theme = "textual-light"
        if self.problems:
            self.push_screen(ProblemsScreen(str(self.path), self.problems))
        elif self.opened is not None:
            self.open(self.opened, self.opened.as_text())
        else:
            self.push_screen(NameScreen(), self.named)

    def named(self, name: str | None) -> None:
        """Start a new file for the named cluster; no name quits without writing anything."""
        if name is None:
            self.exit(1)
            return
        self.open(clusterfile.new(name), None)

    def open(self, file: ClusterFile, saved: str | None) -> None:
        """Show the steps for the file."""
        self.wizard = new_state(self.path, self.ssh_config, self.deps, file, saved)
        self.push_screen(WizardScreen(self.wizard))

    def action_save(self) -> None:
        """Save the file (asking first when it has errors)."""
        self.request_save(None)

    def on_save_file(self, event: SaveFile) -> None:
        """Save when a step asks."""
        self.request_save(None)

    def request_save(self, after: Callable[[], None] | None) -> None:
        """Write the file when it changed; a file with errors is written only after the administrator agrees.
        `after` runs once the file is written (or had nothing to write)."""
        state = self.wizard
        if state is None:
            return
        if not state.dirty():
            self.notify("No changes to save.", markup=False)
            if after is not None:
                after()
            return
        errors = wizard_errors(state)

        def answer(choice: str | None) -> None:
            if choice == "yes":
                self.write(state, len(errors))
                if after is not None:
                    after()

        if errors:
            question = (
                f"{self.path.name} has {len(errors)} error(s) (see Review), so nanohpc deploy will refuse it. "
                "Save anyway?"
            )
            self.push_screen(ChoiceScreen(question, [("Save anyway", "yes"), ("Cancel", "no")]), answer)
        else:
            answer("yes")

    def write(self, state: WizardState, errors: int) -> None:
        """Write the file to disk."""
        text = state.file.as_text()
        save_text(self.path, text)
        state.saved_text = text
        self.saved = True
        self.saved_errors = errors
        note = f" with {errors} error(s)" if errors else ""
        self.notify(f"Saved {self.path}{note}", markup=False)

    def action_quit_wizard(self) -> None:
        """Quit; with unsaved changes, ask whether to save them first."""
        state = self.wizard
        if state is None or not state.dirty():
            self.exit(0 if state is not None else 1)
            return

        def answer(choice: str | None) -> None:
            if choice == "save":
                self.request_save(lambda: self.exit(0))
            elif choice == "discard":
                self.exit(0)

        question = f"Save changes to {self.path.name}?"
        self.push_screen(
            ChoiceScreen(question, [("Save", "save"), ("Don't save", "discard"), ("Cancel", "cancel")]), answer
        )


def run(path: Path, ssh_config: Path | None, deps: Dependencies) -> int:
    """Run the wizard on a cluster.yml (created if missing) and return the exit code."""
    if path.exists() and not path.is_file():
        print(f"{path}: not a file", file=sys.stderr)
        return 1
    if not path.parent.is_dir():
        print(f"{path.parent}: folder not found", file=sys.stderr)
        return 1
    app = WizardApp(path, ssh_config, deps)
    code = app.run()
    if code is None:
        return 1
    if app.saved and app.saved_errors == 0:
        print(f"Saved {path}. Next: ssh-add (load your key), then nanohpc deploy {path}")
    elif app.saved:
        print(f"Saved {path} with {app.saved_errors} error(s): nanohpc validate {path} lists them.")
    return code
