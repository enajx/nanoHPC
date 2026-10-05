"""What the wizard's steps share: their messages, the inputs bound to settings of cluster.yml, and the step
base class. Text from the machines or the file is shown as plain text (`cell`, `plain`), never as markup."""

from typing import Any

from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Input, Label, Static, Switch
from textual.worker import Worker, WorkerState

from nanohpc.wizard.state import WizardState, field_text, write_field


class Edited(Message):
    """The file in memory changed."""


class SaveFile(Message):
    """Save the file to disk (asking first when it has errors)."""


class Probed(Message):
    """A machine was probed (the UID check can run on it)."""

    def __init__(self, machine: str) -> None:
        super().__init__()
        self.machine = machine


class GoToStep(Message):
    """Show another step."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


def cell(value: Any) -> Text:
    """Return a table cell that shows `value` as plain text."""
    return Text(str(value))


class BoundInput(Input):
    """An input for one setting of cluster.yml. Each change is written to the file at once; what empty text
    does depends on `kind` (see state.write_field)."""

    def __init__(self, state: WizardState, path: list[str], kind: str, placeholder: str, id: str) -> None:
        super().__init__(field_text(state.file.get(path)), placeholder=placeholder, id=id)
        self.state = state
        self.path = path
        self.kind = kind

    def on_input_changed(self, event: Input.Changed) -> None:
        """Write the new text to the file."""
        if self.accept(event.value.strip()) and write_field(self.state.file, self.path, event.value.strip(), self.kind):
            self.post_message(Edited())

    def accept(self, text: str) -> bool:
        """Return whether the text may be written (subclasses refuse some values)."""
        return True


class BoundSwitch(Switch):
    """A switch for one true/false setting of cluster.yml."""

    def __init__(self, state: WizardState, path: list[str], id: str) -> None:
        super().__init__(state.file.get(path) is True, id=id)
        self.state = state
        self.path = path

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Write the new value to the file."""
        if event.value != (self.state.file.get(self.path) is True):
            self.state.file.set_value(self.path, event.value)
            self.post_message(Edited())


def field(label: str, widget: Widget) -> Horizontal:
    """Return a form row: a label and its field."""
    return Horizontal(Label(label, classes="field-label"), widget, classes="row")


class Step(VerticalScroll):
    """One step of the wizard. `KEYS` is shown in the footer while the step is open."""

    KEYS = ""

    def __init__(self, state: WizardState, index: int) -> None:
        super().__init__(id=state.steps[index][0], classes="step")
        self.state = state
        self.index = index

    def heading(self) -> Static:
        """Return the step's heading."""
        return Static(f"{self.index + 1} · {self.state.steps[self.index][1].upper()}", classes="heading")

    async def reload(self) -> None:
        """Show the file's current content again."""
        await self.recompose()
        self.fill()

    def fill(self) -> None:
        """Fill the step's tables after it is composed (steps without tables have nothing to do)."""

    def entered(self) -> None:
        """Called each time the step is opened (after reload)."""

    def changed(self) -> None:
        """Tell the screen the file changed."""
        self.post_message(Edited())

    def focus_first(self) -> None:
        """Focus the step's main table, so the step's keys work at once; in a form, focus nothing, so n and b
        work until a field is chosen (tab or a click)."""
        tables = self.query(DataTable)
        if tables:
            tables.first().focus()
        else:
            self.screen.set_focus(None)

    def tell(self, message: str, severity: str) -> None:
        """Show a short notification as plain text."""
        self.notify(message, severity="error" if severity == "error" else "warning", markup=False, timeout=8)

    def worker_error(self, event: Worker.StateChanged) -> str | None:
        """Return the error of a failed worker, after telling the administrator."""
        if event.state != WorkerState.ERROR:
            return None
        message = f"{event.worker.name}: {event.worker.error}"
        self.tell(message, "error")
        return message

    def selected(self, table_id: str) -> str | None:
        """Return the key of the row under the cursor of a table."""
        table = self.query_one(table_id, DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
