"""Step 7: the review: errors (as `nanohpc validate` reports them), what is left to prepare on the machines,
Save, the next steps, and the whole cluster.yml."""

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Label, ListItem, ListView, Static, TextArea

from nanohpc.wizard.common import GoToStep, SaveFile, Step
from nanohpc.wizard.dialogs import plain
from nanohpc.wizard.state import STEPS, error_step, preparation, wizard_errors


class ReviewStep(Step):
    """7 Review: the validation errors (each opens its step), the whole cluster.yml, Save, and the next steps."""

    def compose(self) -> ComposeResult:
        """Show the errors, what is left to prepare, the next steps, and the file."""
        state = self.state
        errors = wizard_errors(state)
        yield self.heading()
        if errors:
            yield Static(f"{len(errors)} error(s): select one to open its step.", classes="warning")
        else:
            yield plain(f"{state.path.name} is valid (as nanohpc validate checks it).", "ok", None)
        items = [ListItem(Label(Text(error_label(error))), name=error) for error in errors]
        yield ListView(*items, id="errors")
        with Horizontal(classes="buttons"):
            yield Button(Text(f"Save {state.path.name}"), id="save")
        todo = preparation(state)
        if todo:
            yield Static("Preparing the machines", classes="subheading")
            yield plain("\n".join(todo), "hint", "preparation")
        yield plain(
            "Next steps:\n"
            "1. Load your SSH key into your agent: ssh-add. (Optional: ssh-add -c asks you before each use of "
            "the key, but a deploy uses the key for every sudo call, so it asks many times.)\n"
            f"2. nanohpc deploy {state.path}",
            "note",
            "next-steps",
        )
        yield TextArea(state.file.as_text(), read_only=True, id="yaml")

    @on(ListView.Selected, "#errors")
    def open_error(self, event: ListView.Selected) -> None:
        """Open the step where the selected error is fixed."""
        self.post_message(GoToStep(error_step(event.item.name or "")))

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        """Save the file."""
        self.post_message(SaveFile())


def error_label(error: str) -> str:
    """Return an error with the name of the step that fixes it."""
    return f"{STEPS[error_step(error)][1]} · {error}"
