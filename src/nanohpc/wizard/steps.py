"""The wizard's seven steps. Each step shows part of cluster.yml and writes every change to the file in
memory at once; the file on disk changes only on save (ctrl+s, the Save button, or quit).

A step posts `Edited` after each change, so the screen can update the validation marks in the sidebar.
Probes, UID checks, and fix-uid run in thread workers, so the screen stays responsive.
"""

import importlib.metadata
import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, ListItem, ListView, Select, Static, Switch, TextArea
from textual.worker import Worker, WorkerState

from nanohpc.config import HOME_DEFAULTS, POLICY_DEFAULTS, SCRATCH_DEFAULTS, is_ipv4
from nanohpc.fixuid import FixPlan, format_plan
from nanohpc.probe import MachineFacts, empty_facts
from nanohpc.wizard.dialogs import (
    AddMachineScreen,
    ConfirmScreen,
    MachineScreen,
    PartitionForm,
    PartitionScreen,
    PlanScreen,
    UserForm,
    UserScreen,
)
from nanohpc.wizard.state import (
    STEPS,
    WizardState,
    default_partition,
    disk_advice,
    disk_label,
    error_step,
    facts_details,
    field_text,
    fill_from_facts,
    next_free_uid,
    probe_summary,
    remove_value,
    uid_conflicts,
    with_roles,
    write_field,
)

DEFAULT_IMAGE_GB = 100
STORAGE_ADVICE = (
    "Three kinds of storage: home (small, safe, backed up), shared scratch (large, fast, cleaned by age; "
    "not built in nanoHPC yet), and local scratch (on each compute machine, for one job's data). "
    "Before adding shared storage, get a faster network (10 GbE or more). As the cluster grows, move /home "
    "off the front node to its own storage machine (or a NAS)."
)


class Edited(Message):
    """The file in memory changed."""


class SaveFile(Message):
    """Save the file to disk."""


class GoToStep(Message):
    """Show another step."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class BoundInput(Input):
    """An input for one setting of cluster.yml. Each change is written to the file; empty text removes the
    setting, so nanoHPC's default applies (shown as the placeholder)."""

    def __init__(self, state: WizardState, path: list[str], kind: str, placeholder: str, id: str) -> None:
        super().__init__(field_text(state.file.get(path)), placeholder=placeholder, id=id)
        self.state = state
        self.path = path
        self.kind = kind

    def on_input_changed(self, event: Input.Changed) -> None:
        """Write the new text to the file."""
        if write_field(self.state.file, self.path, event.value.strip(), self.kind):
            self.post_message(Edited())


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
        super().__init__(id=STEPS[index][0], classes="step")
        self.state = state
        self.index = index

    def heading(self) -> Static:
        """Return the step's heading."""
        return Static(f"{self.index + 1} · {STEPS[self.index][1].upper()}", classes="heading")

    async def reload(self) -> None:
        """Show the file's current content again."""
        await self.recompose()
        self.fill()

    def fill(self) -> None:
        """Fill the step's tables after it is composed (steps without tables have nothing to do)."""

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

    def worker_error(self, event: Worker.StateChanged) -> str | None:
        """Return the error of a failed worker, after telling the administrator."""
        if event.state != WorkerState.ERROR:
            return None
        message = f"{event.worker.name} failed: {event.worker.error}"
        self.notify(message, severity="error", timeout=15)
        return message


class MachinesStep(Step):
    """1 Machines: add machines, probe them over SSH (read-only), and set their roles and hardware."""

    KEYS = "a add · enter edit · p probe · d remove"
    BINDINGS: ClassVar[list[BindingType]] = [("a", "add", "add"), ("p", "probe", "probe"), ("d", "remove", "remove")]

    def compose(self) -> ComposeResult:
        """Show the machines table and its buttons."""
        yield self.heading()
        yield Static(
            "List every machine of the cluster. The wizard probes each one over SSH (read-only): CPUs, memory, "
            "GPUs, disks, and Ubuntu version. Probing is optional for machines already in the file.",
            classes="hint",
        )
        table: DataTable = DataTable(id="machine-table", cursor_type="row", zebra_stripes=False)
        table.add_columns("name", "address", "roles", "hardware", "probe")
        yield table
        with Horizontal(classes="buttons"):
            yield Button("+ Add machine", id="add-machine")
            yield Button("Edit", id="edit-machine", classes="secondary")
            yield Button("Probe again", id="probe-again", classes="secondary")
            yield Button("Probe all", id="probe-all", classes="secondary")
            yield Button("Remove", id="remove-machine", classes="secondary")
        yield Static("", id="machine-facts", classes="card")

    def fill(self) -> None:
        """Fill the table."""
        self.fill_table()

    def fill_table(self) -> None:
        """Show every machine with what the probe found."""
        table = self.query_one("#machine-table", DataTable)
        row = table.cursor_row
        table.clear()
        for name, machine in self.state.file.machines().items():
            machine = machine or {}
            table.add_row(
                name,
                field_text(machine.get("address")),
                " ".join(machine.get("roles") or []),
                hardware(machine),
                probe_summary(self.state.facts.get(name), name in self.state.probing),
                key=name,
            )
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        self.show_facts()

    def selected(self) -> str | None:
        """Return the machine under the cursor."""
        table = self.query_one("#machine-table", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(DataTable.RowHighlighted, "#machine-table")
    def show_facts(self) -> None:
        """Show everything the probe found about the machine under the cursor."""
        name = self.selected()
        facts = self.state.facts.get(name) if name else None
        lines = facts_details(facts) if facts is not None else ["Press p to probe the machine under the cursor."]
        target = self.state.target(name) if name else None
        if name and target != name:
            lines.append(f"Probed as `ssh {target}`; nanohpc deploy uses `ssh {name}`.")
        self.query_one("#machine-facts", Static).update("\n".join(lines))

    @on(Button.Pressed, "#add-machine")
    def action_add(self) -> None:
        """Ask for a new machine, add it, and probe it."""
        self.app.push_screen(AddMachineScreen(list(self.state.file.machines())), self.added)

    def added(self, result: tuple[str, str] | None) -> None:
        """Add the new machine: the first is the front node (and /home), the next ones compute machines."""
        if result is None:
            return
        name, target = result
        machines = self.state.file.machines()
        has = {
            role: any(role in ((machine or {}).get("roles") or []) for machine in machines.values())
            for role in ("front", "home")
        }
        roles = ["compute"]
        if not has["front"]:
            roles = ["front"] if has["home"] else ["front", "home"]
        self.state.file.set_machine(name, {"address": target if is_ipv4(target) else "", "roles": roles})
        self.state.targets[name] = target
        self.changed()
        self.fill_table()
        table = self.query_one("#machine-table", DataTable)
        table.move_cursor(row=table.get_row_index(name))
        self.probe([name])

    @on(DataTable.RowSelected, "#machine-table")
    @on(Button.Pressed, "#edit-machine")
    def edit(self) -> None:
        """Edit the machine under the cursor."""
        name = self.selected()
        if name is None:
            return
        machine = self.state.file.machines()[name] or {}
        partition = default_partition(self.state.file.partitions())
        self.app.push_screen(
            MachineScreen(name, machine, self.state.facts.get(name), partition),
            lambda values: self.edited(name, values),
        )

    def edited(self, name: str, values: dict[str, Any] | None) -> None:
        """Write the edited machine."""
        if values is None:
            return
        self.state.file.set_machine(name, values)
        self.changed()
        self.fill_table()

    @on(Button.Pressed, "#remove-machine")
    def action_remove(self) -> None:
        """Remove the machine under the cursor, after asking."""
        name = self.selected()
        if name is None:
            return

        def remove(yes: bool | None) -> None:
            if yes:
                self.state.file.remove_machine(name)
                self.state.facts.pop(name, None)
                self.changed()
                self.fill_table()

        self.app.push_screen(ConfirmScreen(f"Remove the machine {name} from cluster.yml?"), remove)

    @on(Button.Pressed, "#probe-again")
    def action_probe(self) -> None:
        """Probe the machine under the cursor."""
        name = self.selected()
        if name is not None:
            self.probe([name])

    @on(Button.Pressed, "#probe-all")
    def probe_all(self) -> None:
        """Probe every machine."""
        self.probe(list(self.state.file.machines()))

    def probe(self, names: list[str]) -> None:
        """Probe machines in thread workers (read-only); each row shows "… probing" until its result."""
        deps, ssh_config = self.state.deps, self.state.ssh_config
        for name in names:
            if name in self.state.probing:
                continue
            self.state.probing.add(name)
            target = self.state.target(name)
            self.run_worker(
                lambda target=target: deps.probe_machine(target, ssh_config),
                name=name,
                group="probe",
                thread=True,
                exit_on_error=False,
            )
        self.fill_table()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Keep a finished probe's facts, and fill in what the file does not have yet."""
        if event.worker.group != "probe" or event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        name = event.worker.name
        error = self.worker_error(event)
        facts = event.worker.result if error is None else empty_facts(self.state.target(name), error)
        assert isinstance(facts, MachineFacts)
        self.state.probing.discard(name)
        self.state.facts[name] = facts
        machines = self.state.file.machines()
        if name in machines:
            machine = machines[name] or {}
            filled = fill_from_facts(machine, facts, default_partition(self.state.file.partitions()))
            if filled != machine:
                self.state.file.set_machine(name, filled)
                self.changed()
        self.fill_table()


def hardware(machine: dict[str, Any]) -> str:
    """Return a compute machine's CPUs, memory, and GPUs as written in the file."""
    if "compute" not in (machine.get("roles") or []):
        return ""
    parts = []
    cpu = machine.get("cpu")
    if isinstance(cpu, dict):
        parts.append("×".join(str(cpu.get(key, "?")) for key in ("sockets", "cores_per_socket", "threads_per_core")))
    if machine.get("memory_mb") is not None:
        parts.append(f"{machine['memory_mb']} MB")
    gpu = machine.get("gpu")
    if isinstance(gpu, dict):
        parts.append(f"{gpu.get('count')}× {gpu.get('type')}")
    return " · ".join(parts)


class StorageStep(Step):
    """2 Storage: where /home lives and on which disk, scratch on each compute machine, and the quotas."""

    def compose(self) -> ComposeResult:
        """Show the home and scratch choices with the disks the probe found."""
        state = self.state
        machines = state.file.machines()
        yield self.heading()
        yield Static("/home", classes="subheading")
        candidates = [
            name for name, machine in machines.items() if "compute" not in ((machine or {}).get("roles") or [])
        ]
        home = next(
            (name for name, machine in machines.items() if "home" in ((machine or {}).get("roles") or [])), None
        )
        yield field(
            "served by",
            Select(
                [(name, name) for name in candidates],
                prompt="choose the front node or a storage machine",
                value=home if home in candidates else Select.NULL,
                id="home-machine",
            ),
        )
        if home is not None:
            device = ((machines[home] or {}).get("home") or {}).get("device")
            yield field("on", self.disk_select("home-device", home, device, ("the root disk", "root")))
            advice = disk_advice(state.facts.get(home), device, True) if device else None
            yield Static(advice or "", id="home-advice", classes="warning" if advice else "")
        yield Static("Scratch on each compute machine", classes="subheading")
        for name, machine in machines.items():
            machine = machine or {}
            if "compute" not in (machine.get("roles") or []):
                continue
            scratch = machine.get("scratch") or {}
            device = scratch.get("device")
            choice = device or ("image" if "image_gb" in scratch else None)
            yield field(
                name, self.disk_select(f"scratch-{name}", name, choice, ("an image file on the root disk", "image"))
            )
            if choice == "image":
                yield field(
                    "image size GB",
                    BoundInput(state, ["machines", name, "scratch", "image_gb"], "int", "100", f"scratch-gb-{name}"),
                )
            advice = disk_advice(state.facts.get(name), device, False) if device else None
            if advice:
                yield Static(advice, id=f"scratch-advice-{name}", classes="warning")
        yield Static("Quotas and cleanup", classes="subheading")
        for key, label in (("quota_soft_gb", "home soft limit GB"), ("quota_hard_gb", "home hard limit GB")):
            yield field(label, BoundInput(state, ["home", key], "int", str(HOME_DEFAULTS[key]), f"home-{key}"))
        yield field(
            "grace period",
            BoundInput(state, ["home", "quota_grace"], "text", HOME_DEFAULTS["quota_grace"], "home-quota_grace"),
        )
        yield field(
            "scratch cleanup days",
            BoundInput(
                state, ["scratch", "cleanup_days"], "int", str(SCRATCH_DEFAULTS["cleanup_days"]), "scratch-cleanup"
            ),
        )
        yield Static(STORAGE_ADVICE, classes="note")

    def disk_select(self, id: str, machine: str, value: str | None, first: tuple[str, str]) -> Select[str]:
        """Return a choice of the machine's probed disks (with size, filesystem, mount point), after `first`.
        A device in the file that the probe did not find (or a machine not probed) is listed as it is."""
        facts = self.state.facts.get(machine)
        options = [first]
        if facts is not None and facts.error is None:
            options += [(disk_label(disk), disk.path) for disk in facts.disks]
        if value is not None and value not in [option[1] for option in options]:
            options.append((f"{value} (from the file)", value))
        if facts is None:
            options.append(("(probe the machine in step 1 to list its disks)", "probe"))
        selected = value if value is not None else first[1] if first[1] == "root" else Select.NULL
        return Select(options, value=selected, prompt="choose", id=id)

    @on(Select.Changed, "#home-machine")
    async def home_machine(self, event: Select.Changed) -> None:
        """Move the home role to the chosen machine."""
        if event.value is Select.NULL:
            return
        machines = self.state.file.machines()
        chosen = str(event.value)
        if "home" in ((machines[chosen] or {}).get("roles") or []):
            return
        for name, machine in machines.items():
            machine = machine or {}
            roles = machine.get("roles") or []
            if "home" in roles:
                self.state.file.set_machine(name, with_roles(machine, [role for role in roles if role != "home"]))
        machine = machines[chosen] or {}
        self.state.file.set_machine(chosen, {**machine, "roles": [*(machine.get("roles") or []), "home"]})
        self.changed()
        await self.reload()

    @on(Select.Changed, "#home-device")
    async def home_device(self, event: Select.Changed) -> None:
        """Use the chosen disk for /home (or the root disk)."""
        machines = self.state.file.machines()
        home = next(name for name, machine in machines.items() if "home" in ((machine or {}).get("roles") or []))
        machine = dict(machines[home] or {})
        current = (machine.get("home") or {}).get("device") or "root"
        if event.value in (Select.NULL, "probe", current):
            return
        if event.value == "root":
            machine.pop("home", None)
        else:
            machine["home"] = {"device": str(event.value)}
        self.state.file.set_machine(home, machine)
        self.changed()
        await self.reload()

    async def on_select_changed(self, event: Select.Changed) -> None:
        """Use the chosen disk, or an image file, for a compute machine's scratch."""
        select_id = event.select.id or ""
        if not select_id.startswith("scratch-") or event.value in (Select.NULL, "probe"):
            return
        name = select_id.removeprefix("scratch-")
        machine = dict(self.state.file.machines()[name] or {})
        scratch = machine.get("scratch") or {}
        current = scratch.get("device") or ("image" if "image_gb" in scratch else None)
        if event.value == current:
            return
        if event.value == "image":
            machine["scratch"] = {"image_gb": DEFAULT_IMAGE_GB}
        else:
            machine["scratch"] = {"device": str(event.value)}
        self.state.file.set_machine(name, machine)
        self.changed()
        await self.reload()


class UsersStep(Step):
    """3 Users: names, UIDs, SSH keys, administrators, and the UID check on every probed machine."""

    KEYS = "a add · enter edit · d remove · c check UIDs · f fix the selected conflict"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("a", "add", "add"),
        ("d", "remove", "remove"),
        ("c", "check", "check"),
        ("f", "fix", "fix"),
    ]

    def compose(self) -> ComposeResult:
        """Show the users and the UID check."""
        yield self.heading()
        yield Static(
            "Each user has the same UID on every machine. Administrators get sudo and SSH on every machine.",
            classes="hint",
        )
        table: DataTable = DataTable(id="user-table", cursor_type="row")
        table.add_columns("name", "uid", "keys", "admin")
        yield table
        with Horizontal(classes="buttons"):
            yield Button("+ Add user", id="add-user")
            yield Button("Edit", id="edit-user", classes="secondary")
            yield Button("Remove", id="remove-user", classes="secondary")
            yield Button("Check UIDs", id="check-uids", classes="secondary")
        yield Static("UID check", classes="subheading")
        yield Static("", id="uid-summary", classes="hint")
        conflicts: DataTable = DataTable(id="uid-table", cursor_type="row")
        conflicts.add_columns("machine", "user", "on the machine (UID, GID)", "cluster.yml UID")
        yield conflicts
        with Horizontal(classes="buttons"):
            yield Button("Fix…", id="fix-uid", classes="secondary")
        yield Static(
            "Fix shows what nanohpc fix-uid would change on that machine (a read-only check). Nothing changes "
            "until you confirm. The deploy also stops on a UID conflict, without changing that machine.",
            classes="hint",
        )

    def fill(self) -> None:
        """Fill the tables."""
        self.fill_tables()

    def fill_tables(self) -> None:
        """Show the users and the conflicts the UID check found."""
        admins = self.state.file.get(["cluster", "admins"]) or []
        table = self.query_one("#user-table", DataTable)
        row = table.cursor_row
        table.clear()
        for user in self.state.file.users():
            name = str(user.get("name"))
            keys = len(user.get("ssh_keys") or [])
            table.add_row(name, field_text(user.get("uid")), str(keys), "yes" if name in admins else "", key=name)
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        conflicts = self.query_one("#uid-table", DataTable)
        conflicts.clear()
        expected = {user.get("name"): user.get("uid") for user in self.state.file.users()}
        for machine, user, ids in uid_conflicts(self.state):
            conflicts.add_row(machine, user, f"{ids[0]}, {ids[1]}", str(expected[user]), key=f"{machine}:{user}")
        self.query_one("#uid-summary", Static).update(self.summary())

    def summary(self) -> str:
        """Return which machines the UID check covered."""
        machines = list(self.state.file.machines())
        probed = [name for name in machines if name in self.state.facts and self.state.facts[name].error is None]
        checked = [name for name in machines if name in self.state.uid_checks]
        lines = []
        if not checked:
            lines.append("Not checked yet. Press c to check every probed machine (read-only).")
        else:
            count = len(uid_conflicts(self.state))
            lines.append(f"Checked on {', '.join(checked)}: {count} conflict(s).")
        missing = [name for name in machines if name not in probed]
        if missing:
            lines.append(f"Not probed (probe them in step 1 to check): {', '.join(missing)}.")
        lines += [f"✗ {name}: {error}" for name, error in self.state.uid_errors.items()]
        return "\n".join(lines)

    def selected(self, table_id: str) -> str | None:
        """Return the key of the row under the cursor of a table."""
        table = self.query_one(table_id, DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(Button.Pressed, "#add-user")
    def action_add(self) -> None:
        """Ask for a new user."""
        users = self.state.file.users()
        taken = [str(user.get("name")) for user in users]
        self.app.push_screen(UserScreen(None, False, next_free_uid(users), taken), self.saved)

    @on(DataTable.RowSelected, "#user-table")
    @on(Button.Pressed, "#edit-user")
    def edit(self) -> None:
        """Edit the user under the cursor."""
        name = self.selected("#user-table")
        if name is None:
            return
        user = next(user for user in self.state.file.users() if user.get("name") == name)
        admins = self.state.file.get(["cluster", "admins"]) or []
        self.app.push_screen(UserScreen(user, name in admins, 0, []), self.saved)

    def saved(self, form: UserForm | None) -> None:
        """Write the user."""
        if form is None:
            return
        self.state.file.set_user(form.name, form.uid, form.ssh_keys, form.admin)
        self.changed()
        self.fill_tables()

    @on(Button.Pressed, "#remove-user")
    def action_remove(self) -> None:
        """Remove the user under the cursor, after asking."""
        name = self.selected("#user-table")
        if name is None:
            return

        def remove(yes: bool | None) -> None:
            if yes:
                self.state.file.remove_user(name)
                self.changed()
                self.fill_tables()

        self.app.push_screen(ConfirmScreen(f"Remove the user {name} from cluster.yml?"), remove)

    @on(Button.Pressed, "#check-uids")
    def action_check(self) -> None:
        """Read the users' UIDs on every probed machine (read-only), in thread workers."""
        names = [str(user.get("name")) for user in self.state.file.users()]
        machines = [
            name
            for name in self.state.file.machines()
            if name in self.state.facts and self.state.facts[name].error is None
        ]
        if not machines or not names:
            self.notify("Add users and probe the machines (step 1) first.", severity="warning")
            return
        deps, ssh_config = self.state.deps, self.state.ssh_config
        for machine in machines:
            self.state.uid_errors.pop(machine, None)
            target = self.state.target(machine)
            self.run_worker(
                lambda target=target: deps.user_ids(target, ssh_config, names),
                name=machine,
                group="uid-check",
                thread=True,
                exit_on_error=False,
            )

    @on(Button.Pressed, "#fix-uid")
    def action_fix(self) -> None:
        """Make the fix-uid plan for the selected conflict (read-only), then show it."""
        key = self.selected("#uid-table")
        if key is None:
            self.notify("No UID conflict selected: run the UID check (c) first.", severity="warning")
            return
        machine, user = key.split(":", 1)
        uid = next(entry["uid"] for entry in self.state.file.users() if entry.get("name") == user)
        deps, ssh_config, target = self.state.deps, self.state.ssh_config, self.state.target(machine)
        self.run_worker(
            lambda: deps.plan_fix(target, ssh_config, user, uid),
            name=machine,
            group="fix-plan",
            thread=True,
            exit_on_error=False,
        )

    def show_plan(self, machine: str, plan: FixPlan) -> None:
        """Show the plan; apply it only when the administrator confirms."""

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.apply(machine, plan)

        title = f"FIX UID: {plan.user} on {machine}"
        self.app.push_screen(PlanScreen(title, format_plan(plan), plan.ok), confirmed)

    def apply(self, machine: str, plan: FixPlan) -> None:
        """Apply a confirmed plan, then read the UIDs on that machine again, in a thread worker."""
        deps, ssh_config, target = self.state.deps, self.state.ssh_config, self.state.target(machine)
        names = [str(user.get("name")) for user in self.state.file.users()]

        def apply_and_check() -> tuple[int, str, dict[str, tuple[int, int] | None]]:
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = deps.apply_fix(target, ssh_config, plan)
            return code, output.getvalue(), deps.user_ids(target, ssh_config, names)

        self.run_worker(apply_and_check, name=machine, group="fix-apply", thread=True, exit_on_error=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Show the results of UID checks, fix-uid plans, and applied plans."""
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        machine, group, result = event.worker.name, event.worker.group, event.worker.result
        error = self.worker_error(event)
        if group == "uid-check":
            if error is not None:
                self.state.uid_errors[machine] = error
            else:
                assert isinstance(result, dict)
                self.state.uid_checks[machine] = result
        elif group == "fix-plan" and error is None:
            assert isinstance(result, FixPlan)
            self.show_plan(machine, result)
        elif group == "fix-apply" and error is None:
            assert isinstance(result, tuple)
            code, output, ids = result
            self.state.uid_checks[machine] = ids
            self.notify(output.strip() or f"fix-uid exit code {code}", severity="information" if code == 0 else "error")
        self.fill_tables()


class PartitionsStep(Step):
    """4 Partitions and policy: the Slurm queues with their machines, and the queue policy."""

    KEYS = "a add · enter edit · d remove"
    BINDINGS: ClassVar[list[BindingType]] = [("a", "add", "add"), ("d", "remove", "remove")]
    POLICY_LABELS: ClassVar[dict[str, str]] = {
        "max_submit_jobs_per_user": "jobs queued per user",
        "max_gpus_per_user": "GPUs per user (or unlimited)",
        "default_cpus_per_gpu": "CPUs per GPU (default)",
        "default_memory_mb_per_cpu": "memory MB per CPU (default)",
        "fairshare_weight": "fair-share weight",
        "fairshare_half_life": "fair-share half-life",
        "age_weight": "waiting-time weight",
        "age_max": "waiting time counted up to",
    }

    def compose(self) -> ComposeResult:
        """Show the partitions table and the policy fields."""
        yield self.heading()
        yield Static(
            "Start from the defaults: one partition, main, for any job, 24 hours. Add partitions only when "
            "needed (for example interactive on some GPU machines).",
            classes="hint",
        )
        table: DataTable = DataTable(id="partition-table", cursor_type="row")
        table.add_columns("name", "default", "jobs", "max time", "GPUs per user", "machines")
        yield table
        with Horizontal(classes="buttons"):
            yield Button("+ Add partition", id="add-partition")
            yield Button("Edit", id="edit-partition", classes="secondary")
            yield Button("Remove", id="remove-partition", classes="secondary")
        yield Static("Policy (empty: nanoHPC's default, shown in grey)", classes="subheading")
        for key, label in self.POLICY_LABELS.items():
            kind = "text" if key in ("fairshare_half_life", "age_max") else "int"
            yield field(
                label, BoundInput(self.state, ["policy", key], kind, str(POLICY_DEFAULTS[key]), f"policy-{key}")
            )

    def members(self, partition: str) -> list[str]:
        """Return the compute machines in a partition."""
        return [
            name
            for name, machine in self.state.file.machines().items()
            if partition in ((machine or {}).get("partitions") or [])
        ]

    def compute(self) -> list[str]:
        """Return the compute machines."""
        return [
            name
            for name, machine in self.state.file.machines().items()
            if "compute" in ((machine or {}).get("roles") or [])
        ]

    def fill(self) -> None:
        """Fill the table."""
        self.fill_table()

    def fill_table(self) -> None:
        """Show every partition."""
        table = self.query_one("#partition-table", DataTable)
        table.clear()
        for name, partition in self.state.file.partitions().items():
            partition = partition or {}
            table.add_row(
                name,
                "yes" if partition.get("default") is True else "",
                str(partition.get("jobs", "any")),
                field_text(partition.get("max_time")),
                field_text(partition.get("max_gpus_per_user", "unlimited")),
                ", ".join(self.members(name)),
                key=name,
            )

    def selected(self) -> str | None:
        """Return the partition under the cursor."""
        table = self.query_one("#partition-table", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(Button.Pressed, "#add-partition")
    def action_add(self) -> None:
        """Ask for a new partition."""
        taken = list(self.state.file.partitions())
        self.app.push_screen(PartitionScreen(None, {}, [], self.compute(), taken), self.saved)

    @on(DataTable.RowSelected, "#partition-table")
    @on(Button.Pressed, "#edit-partition")
    def edit(self) -> None:
        """Edit the partition under the cursor."""
        name = self.selected()
        if name is None:
            return
        partition = self.state.file.partitions()[name] or {}
        self.app.push_screen(PartitionScreen(name, partition, self.members(name), self.compute(), []), self.saved)

    def saved(self, form: PartitionForm | None) -> None:
        """Write the partition, make it the only default if it is the default, and update its machines."""
        if form is None:
            return
        file = self.state.file
        if form.values.get("default") is True:
            for name, partition in file.partitions().items():
                if name != form.name and (partition or {}).get("default") is True:
                    file.set_partition(name, {key: value for key, value in partition.items() if key != "default"})
        file.set_partition(form.name, form.values)
        for name in self.compute():
            machine = file.machines()[name] or {}
            partitions = list(machine.get("partitions") or [])
            if name in form.members and form.name not in partitions:
                file.set_machine(name, {**machine, "partitions": [*partitions, form.name]})
            elif name not in form.members and form.name in partitions:
                file.set_machine(name, {**machine, "partitions": [item for item in partitions if item != form.name]})
        self.changed()
        self.fill_table()

    @on(Button.Pressed, "#remove-partition")
    def action_remove(self) -> None:
        """Remove the partition under the cursor (and from its machines), after asking."""
        name = self.selected()
        if name is None:
            return

        def remove(yes: bool | None) -> None:
            if not yes:
                return
            file = self.state.file
            file.remove_partition(name)
            for machine_name in self.members(name):
                machine = file.machines()[machine_name] or {}
                partitions = [item for item in machine.get("partitions") or [] if item != name]
                file.set_machine(machine_name, {**machine, "partitions": partitions})
            self.changed()
            self.fill_table()

        self.app.push_screen(ConfirmScreen(f"Remove the partition {name}?"), remove)


class WebsiteStep(Step):
    """5 Website: the cluster's name, hostname, path, HTTPS, logo, and who can open it."""

    def compose(self) -> ComposeResult:
        """Show the website's fields and the notes about access."""
        state = self.state
        website = ["cluster", "website"]
        yield self.heading()
        yield field("cluster name", BoundInput(state, ["cluster", "name"], "text", "mylab", "cluster-name-field"))
        yield field(
            "hostname", BoundInput(state, [*website, "hostname"], "text", "cluster.example.org", "website-hostname")
        )
        yield field("path", BoundInput(state, [*website, "path"], "text", "/cluster/", "website-path"))
        https = state.file.get([*website, "https"])
        yield field(
            "HTTPS",
            Select(
                [("Let's Encrypt (free certificate)", "letsencrypt"), ("my own certificate", "own")],
                value=https if https in ("letsencrypt", "own") else Select.NULL,
                id="website-https",
            ),
        )
        if https == "own":
            yield field(
                "certificate file",
                BoundInput(state, [*website, "certificate"], "text", "cert.pem", "website-certificate"),
            )
            yield field(
                "certificate key file",
                BoundInput(state, [*website, "certificate_key"], "text", "key.pem", "website-certificate_key"),
            )
        else:
            yield Static(
                "Let's Encrypt needs the hostname to reach the front node on port 80 from the internet.",
                classes="hint",
            )
        yield field("logo (optional)", BoundInput(state, [*website, "logo"], "text", "logo.png", "website-logo"))
        yield field(
            "who can open it",
            BoundInput(state, [*website, "allow"], "list", "anyone; or networks like 10.0.0.0/8", "website-allow"),
        )
        yield Static(
            "Recommended, not required: a private network such as WireGuard or Tailscale, for security and "
            "simpler administrator access. Then limit the site to it above.",
            classes="note",
        )
        yield Static(
            "To show the cluster site on the lab's own website (labwebsite.com/cluster/), "
            f"nanohpc forwarding-rules {state.path} prints the rules for its web server (nginx, Apache, Caddy).",
            classes="hint",
        )

    @on(Select.Changed, "#website-https")
    async def https_changed(self, event: Select.Changed) -> None:
        """Write the HTTPS choice; Let's Encrypt drops the own certificate's files."""
        path = ["cluster", "website", "https"]
        if event.value is Select.NULL or event.value == self.state.file.get(path):
            return
        self.state.file.set_value(path, str(event.value))
        if event.value == "letsencrypt":
            for key in ("certificate", "certificate_key"):
                remove_value(self.state.file, ["cluster", "website", key])
        self.changed()
        await self.reload()


class ExtrasStep(Step):
    """6 Extras: the backup, Slack alerts, automatic deploys, and the pinned nanoHPC version."""

    def compose(self) -> ComposeResult:
        """Show the extras' fields."""
        state = self.state
        backup_machines = [
            name for name, machine in state.file.machines().items() if "backup" in ((machine or {}).get("roles") or [])
        ]
        yield self.heading()
        yield Static("Backup of /home (every night)", classes="subheading")
        yield field(
            "backup to",
            Input(
                field_text(state.file.get(["backup", "to"])),
                placeholder=" or ".join([*backup_machines, "user@host:/path"]) + "; empty: no backup",
                id="backup-to",
            ),
        )
        yield Static("Alerts", classes="subheading")
        yield field("Slack alerts", BoundSwitch(state, ["alerts", "slack"], "alerts-slack"))
        yield Static(
            f"The Slack webhook goes in .env next to {state.path.name}: NANOHPC_SLACK_WEBHOOK=https://hooks.slack.com/... "
            "(never commit .env).",
            classes="hint",
        )
        yield Static("Automatic deploys", classes="subheading")
        auto = ["auto_deploy"]
        yield field("enabled", BoundSwitch(state, [*auto, "enabled"], "auto-enabled"))
        yield field(
            "repository (SSH)",
            BoundInput(
                state, [*auto, "repository"], "text", "git@github.com:lab/cluster-config.git", "auto-repository"
            ),
        )
        yield field("branch", BoundInput(state, [*auto, "branch"], "text", "main", "auto-branch"))
        yield field(
            "check every minutes", BoundInput(state, [*auto, "every_minutes"], "int", "10", "auto-every_minutes")
        )
        yield field("GitHub webhook", BoundSwitch(state, [*auto, "webhook"], "auto-webhook"))
        yield Static(
            "The front node deploys this file's repository by itself. The webhook's secret goes in .env: "
            "NANOHPC_DEPLOY_WEBHOOK_SECRET (for example openssl rand -hex 32).",
            classes="hint",
        )
        yield field(
            "nanoHPC version",
            BoundInput(state, ["nanohpc_version"], "text", importlib.metadata.version("nanohpc"), "nanohpc-version"),
        )
        yield Static("The version the front node installs; needed for automatic deploys.", classes="hint")

    @on(Input.Changed, "#backup-to")
    def backup_changed(self, event: Input.Changed) -> None:
        """Write the backup target; empty text turns the backup off."""
        text = event.value.strip()
        if text == field_text(self.state.file.get(["backup", "to"])):
            return
        if text:
            self.state.file.set_value(["backup", "to"], text)
        else:
            remove_value(self.state.file, ["backup"])
        self.changed()


class ReviewStep(Step):
    """7 Review: the validation errors (each opens its step), the whole cluster.yml, Save, and the next steps."""

    def compose(self) -> ComposeResult:
        """Show the errors, the next steps, and the file."""
        state = self.state
        errors = state.file.validate()
        yield self.heading()
        if errors:
            yield Static(f"{len(errors)} error(s): select one to open its step.", classes="warning")
        else:
            yield Static("cluster.yml is valid.", classes="ok")
        items = [ListItem(Label(f"{error_label(error)}"), name=error) for error in errors]
        yield ListView(*items, id="errors")
        with Horizontal(classes="buttons"):
            yield Button(f"Save {state.path.name}", id="save")
        yield Static(
            "Next steps:\n"
            "1. Load your SSH key into your agent: ssh-add. (Optional: ssh-add -c asks you before each use of "
            "the key, but a deploy uses the key for every sudo call, so it asks many times.)\n"
            f"2. nanohpc deploy {state.path}",
            id="next-steps",
            classes="note",
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


STEP_CLASSES: tuple[type[Step], ...] = (
    MachinesStep,
    StorageStep,
    UsersStep,
    PartitionsStep,
    WebsiteStep,
    ExtrasStep,
    ReviewStep,
)
