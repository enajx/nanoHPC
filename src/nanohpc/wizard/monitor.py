"""Monitor-only wizard steps: machines, displayed login names, and alerts."""

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Select, Static

from nanohpc.config import MACHINE_NAME
from nanohpc.wizard.common import BoundSwitch, Step, cell, field
from nanohpc.wizard.dialogs import ConfirmScreen, plain
from nanohpc.wizard.forms import WebsiteStep
from nanohpc.wizard.review import ReviewStep
from nanohpc.wizard.state import field_text, parse_field


class MonitorMachinesStep(Step):
    """Add machines by name and address, and choose the monitoring host."""

    KEYS = "a add · enter edit · d remove"
    BINDINGS: ClassVar[list[BindingType]] = [("a", "add", "add"), ("d", "remove", "remove")]

    def compose(self) -> ComposeResult:
        """Show monitored machines and the host selection."""
        yield self.heading()
        yield Static(
            "List each machine to monitor. GPU models and counts are discovered during deploy.", classes="hint"
        )
        table: DataTable = DataTable(id="monitor-machine-table", cursor_type="row")
        table.add_columns("name", "address", "aliases")
        yield table
        yield field("name", Input(placeholder="gpu1", id="monitor-machine-name"))
        yield field("address", Input(placeholder="192.168.1.11", id="monitor-machine-address"))
        yield field("aliases", Input(placeholder="optional, comma separated", id="monitor-machine-aliases"))
        with Horizontal(classes="buttons"):
            yield Button("Add or update", id="monitor-machine-add")
            yield Button("Remove selected", id="monitor-machine-remove", classes="secondary")
        machines = list(self.state.file.machines())
        options = [(name, name) for name in machines]
        selected = self.state.file.get(["cluster", "monitor_host"])
        yield field(
            "monitoring host",
            Select(options, value=selected if selected in machines else Select.NULL, id="monitor-host"),
        )
        yield plain("The monitoring host also serves the website and may run work itself.", "hint", None)

    def fill(self) -> None:
        """Fill the machines table."""
        table = self.query_one("#monitor-machine-table", DataTable)
        table.clear()
        for name, values in self.state.file.machines().items():
            table.add_row(
                cell(name), cell(values.get("address", "")), cell(", ".join(values.get("aliases") or [])), key=name
            )

    @on(DataTable.RowSelected, "#monitor-machine-table")
    def select_machine(self) -> None:
        """Put a selected machine's fields in the edit form."""
        name = self.selected("#monitor-machine-table")
        if name is None:
            return
        machine = self.state.file.machines()[name]
        self.query_one("#monitor-machine-name", Input).value = name
        self.query_one("#monitor-machine-address", Input).value = str(machine.get("address", ""))
        self.query_one("#monitor-machine-aliases", Input).value = field_text(machine.get("aliases"))

    @on(Button.Pressed, "#monitor-machine-add")
    def action_add(self) -> None:
        """Add or update a machine from the form."""
        name = self.query_one("#monitor-machine-name", Input).value.strip()
        address = self.query_one("#monitor-machine-address", Input).value.strip()
        aliases = self.query_one("#monitor-machine-aliases", Input).value.strip()
        if not MACHINE_NAME.fullmatch(name):
            self.tell("Machine name must start with a letter and contain only letters, digits, _ or -.", "error")
            return
        self.state.file.set_machine(name, {"address": address, "aliases": parse_field(aliases, "list")})
        self.changed()
        self.fill()
        selector = self.query_one("#monitor-host", Select)
        selector.set_options([(item, item) for item in self.state.file.machines()])
        host = self.state.file.get(["cluster", "monitor_host"])
        if host in self.state.file.machines():
            selector.value = host

    @on(Button.Pressed, "#monitor-machine-remove")
    def action_remove(self) -> None:
        """Remove the selected machine after confirmation."""
        name = self.selected("#monitor-machine-table")
        if name is None:
            return

        async def remove(answer: str | None) -> None:
            if answer == "yes":
                self.state.file.remove_machine(name)
                if self.state.file.get(["cluster", "monitor_host"]) == name:
                    self.state.file.set_value(["cluster", "monitor_host"], None)
                self.changed()
                await self.reload()

        self.app.push_screen(ConfirmScreen(f"Remove {name} from monitoring?"), remove)

    @on(Select.Changed, "#monitor-host")
    def host_changed(self, event: Select.Changed) -> None:
        """Store the chosen monitoring host."""
        if event.value is Select.NULL:
            return
        if event.value != self.state.file.get(["cluster", "monitor_host"]):
            self.state.file.set_value(["cluster", "monitor_host"], str(event.value))
            self.changed()


class MonitorUsersStep(Step):
    """Edit displayed login names without managing accounts or SSH keys."""

    def compose(self) -> ComposeResult:
        """Show one comma-separated login name field."""
        yield self.heading()
        yield Static("Existing login names to show on the website. Leave empty to show none.", classes="hint")
        yield field("login names", Input(field_text(self.state.file.users()), id="monitor-users"))

    @on(Input.Changed, "#monitor-users")
    def users_changed(self, event: Input.Changed) -> None:
        """Store names as a list, including an empty list."""
        users = parse_field(event.value, "list")
        if users != self.state.file.users():
            self.state.file.set_value(["users"], users)
            self.changed()


class MonitorAlertsStep(Step):
    """Configure Slack delivery for monitoring alerts."""

    def compose(self) -> ComposeResult:
        """Show the optional Slack switch and secret instructions."""
        yield self.heading()
        yield field("Slack alerts", BoundSwitch(self.state, ["alerts", "slack"], "alerts-slack"))
        yield plain(
            f"If enabled, put NANOHPC_SLACK_WEBHOOK=https://hooks.slack.com/... in .env next to {self.state.path.name}.",
            "hint",
            None,
        )


MONITOR_STEP_CLASSES: tuple[type[Step], ...] = (
    MonitorMachinesStep,
    MonitorUsersStep,
    WebsiteStep,
    MonitorAlertsStep,
    ReviewStep,
)
