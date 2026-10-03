"""Steps 1 and 2: the machines (probe, roles, hardware, and the preparation checklist) and the storage."""

from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Select, Static
from textual.worker import Worker, WorkerState

from nanohpc.config import HOME_DEFAULTS, SCRATCH_DEFAULTS, is_ipv4
from nanohpc.probe import MachineFacts, empty_facts
from nanohpc.wizard.common import BoundInput, Edited, Probed, Step, cell, field
from nanohpc.wizard.dialogs import AddMachineScreen, AddressScreen, ConfirmScreen, MachineScreen, plain
from nanohpc.wizard.state import (
    WizardState,
    checklist,
    default_partition,
    disk_label,
    disk_problem,
    facts_details,
    field_text,
    fill_from_facts,
    free_gb,
    needs_address,
    probe_summary,
    scratch_warning,
    widget_id,
    with_roles,
)

DEFAULT_IMAGE_GB = 100
STORAGE_ADVICE = (
    "Three kinds of storage: home (small, safe, backed up), shared scratch (large, fast, cleaned by age; "
    "not built in nanoHPC yet), and local scratch (on each compute machine, for one job's data). "
    "Before adding shared storage, get a faster network (10 GbE or more). As the cluster grows, move /home "
    "off the front node to its own storage machine (or a NAS)."
)


def roles_of(machine: dict[str, Any] | None) -> list[str]:
    """Return a machine's roles."""
    return list((machine or {}).get("roles") or [])


class MachinesStep(Step):
    """1 Machines: add machines, probe them over SSH (read-only), set their roles and hardware, and prepare
    them (the checklist under the table)."""

    KEYS = "a add · enter edit · p probe · d remove"
    BINDINGS: ClassVar[list[BindingType]] = [("a", "add", "add"), ("p", "probe", "probe"), ("d", "remove", "remove")]

    def compose(self) -> ComposeResult:
        """Show the machines table, its buttons, and the selected machine's facts and checklist."""
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
        yield plain("", "card", "machine-facts")
        yield Static("Preparing the machine", classes="subheading")
        yield Vertical(id="checklist")

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
                cell(name),
                cell(field_text(machine.get("address"))),
                cell(" ".join(roles_of(machine))),
                cell(hardware(machine)),
                cell(probe_summary(self.state.facts.get(name), name in self.state.probing)),
                key=name,
            )
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        self.show_facts()

    @on(DataTable.RowHighlighted, "#machine-table")
    def show_facts(self) -> None:
        """Show what the probe found about the machine under the cursor, and its preparation checklist."""
        name = self.selected("#machine-table")
        facts = self.state.facts.get(name) if name else None
        target = self.state.target(name) if name else ""
        if facts is not None:
            lines = facts_details(facts, target)
        else:
            lines = ["Press p to probe the machine under the cursor."]
        if name and target != name:
            lines.append(f"Probed as `ssh {target}`; nanohpc deploy uses `ssh {name}`.")
        self.query_one("#machine-facts", Static).update(Text("\n".join(lines)))
        box = self.query_one("#checklist", Vertical)
        box.remove_children()
        if name is None or facts is None:
            box.mount(plain("Probe the machine to see what to prepare on it.", "hint", None))
            return
        for item in checklist(self.state, name):
            skipped = (name, item.key) in self.state.skipped
            mark = "i" if item.info else "✓" if item.ok else "skipped, to do later:" if skipped else "✗"
            text = f"{mark} {item.label}" + ("" if item.ok else f"\n  {item.hint}")
            row = Horizontal(classes="check-row")
            box.mount(row)
            row.mount(plain(text, "check" if item.ok else "check todo", None))
            if not item.ok:
                label = "Undo skip" if skipped else "Skip, I'll do it later"
                row.mount(Button(label, id=f"skip-{item.key}", classes="secondary skip"))

    @on(Button.Pressed, ".skip")
    def skip(self, event: Button.Pressed) -> None:
        """Mark a preparation item as done later (or undo that), for this session."""
        name = self.selected("#machine-table")
        if name is None:
            return
        item = (name, (event.button.id or "").removeprefix("skip-"))
        if item in self.state.skipped:
            self.state.skipped.discard(item)
        else:
            self.state.skipped.add(item)
        self.show_facts()

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
        has = {role: any(role in roles_of(machine) for machine in machines.values()) for role in ("front", "home")}
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
        name = self.selected("#machine-table")
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
        """Remove the machine under the cursor, after asking; forget what the session learned about it."""
        name = self.selected("#machine-table")
        if name is None:
            return

        def remove(answer: str | None) -> None:
            if answer == "yes":
                self.state.file.remove_machine(name)
                self.state.forget(name)
                self.changed()
                self.fill_table()

        self.app.push_screen(ConfirmScreen(f"Remove the machine {name} from cluster.yml?"), remove)

    @on(Button.Pressed, "#probe-again")
    def action_probe(self) -> None:
        """Probe the machine under the cursor (again, for example after a timeout)."""
        name = self.selected("#machine-table")
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
        """Keep a finished probe's facts, fill in what the file does not have yet, ask for the address when
        the machine has several, and start the UID check on it."""
        if event.worker.group != "probe" or event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        name = event.worker.name
        self.state.probing.discard(name)
        machines = self.state.file.machines()
        if name not in machines:
            return
        error = self.worker_error(event)
        facts = event.worker.result if error is None else empty_facts(self.state.target(name), error)
        assert isinstance(facts, MachineFacts)
        self.state.facts[name] = facts
        machine = machines[name] or {}
        filled = fill_from_facts(machine, facts, default_partition(self.state.file.partitions()))
        if filled != machine:
            self.state.file.set_machine(name, filled)
            self.changed()
        self.fill_table()
        if needs_address(filled, facts):
            self.app.push_screen(AddressScreen(name, facts.addresses), lambda address: self.set_address(name, address))
        if facts.error is None:
            self.post_message(Probed(name))

    def set_address(self, name: str, address: str | None) -> None:
        """Write the address chosen among the probed ones."""
        machines = self.state.file.machines()
        if address is None or name not in machines:
            return
        self.state.file.set_machine(name, {**(machines[name] or {}), "address": address})
        self.changed()
        self.fill_table()


def hardware(machine: dict[str, Any]) -> str:
    """Return a compute machine's CPUs, memory, and GPUs as written in the file."""
    if "compute" not in roles_of(machine):
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


class ScratchSizeInput(BoundInput):
    """The size of a scratch image file: refuses a size larger than the free space on the root disk."""

    def __init__(self, state: WizardState, machine: str, free: float | None, id: str) -> None:
        super().__init__(state, ["machines", machine, "scratch", "image_gb"], "int", str(DEFAULT_IMAGE_GB), id)
        self.free = free

    def accept(self, text: str) -> bool:
        """Refuse a size larger than the free space (the file keeps the last size that fits)."""
        refused = self.free is not None and text.isdigit() and int(text) > self.free
        note = self.screen.query(f"#{self.id}-note")
        if note:
            message = f"refused: only {self.free:g} GB free on the root disk" if refused else self.note_text()
            note.first(Static).update(Text(message))
        return not refused

    def note_text(self) -> str:
        """Return the free space note."""
        return f"{self.free:g} GB free on the root disk" if self.free is not None else "free space: not probed"


class StorageStep(Step):
    """2 Storage: where /home lives and on which disk, scratch on each compute machine, and the quotas."""

    def __init__(self, state: WizardState, index: int) -> None:
        super().__init__(state, index)
        self.scratch_ids: dict[str, str] = {}  # id of a scratch choice -> machine

    def compose(self) -> ComposeResult:
        """Show the home and scratch choices with the disks the probe found."""
        state = self.state
        machines = state.file.machines()
        cleanup = state.file.get(["scratch", "cleanup_days"])
        yield self.heading()
        yield Static("/home", classes="subheading")
        candidates = [name for name, machine in machines.items() if "compute" not in roles_of(machine)]
        home = next((name for name, machine in machines.items() if "home" in roles_of(machine)), None)
        yield field(
            "served by",
            Select(
                [(Text(name), name) for name in candidates],
                prompt="choose the front node or a storage machine",
                value=home if home in candidates else Select.NULL,
                id="home-machine",
            ),
        )
        if home is not None:
            device = ((machines[home] or {}).get("home") or {}).get("device")
            yield field("on", self.disk_select("home-device", home, device, ("the root disk", "root")))
            problem = disk_problem(state.facts.get(home), device, True) if device else None
            yield plain(problem or "", "warning" if problem else "", "home-advice")
        yield Static("Scratch on each compute machine", classes="subheading")
        self.scratch_ids = {}
        for name, machine in machines.items():
            machine = machine or {}
            if "compute" not in roles_of(machine):
                continue
            scratch = machine.get("scratch") or {}
            device = scratch.get("device")
            choice = device or ("image" if "image_gb" in scratch else None)
            select_id = widget_id("scratch", name)
            self.scratch_ids[select_id] = name
            yield field(name, self.disk_select(select_id, name, choice, ("an image file on the root disk", "image")))
            if choice == "image":
                size_id = widget_id("scratch-gb", name)
                size = ScratchSizeInput(state, name, free_gb(state.facts.get(name)), size_id)
                yield field("image size GB", size)
                yield plain(size.note_text(), "hint", f"{size_id}-note")
            if device:
                facts = state.facts.get(name)
                advice = disk_problem(facts, device, False) or scratch_warning(facts, device, cleanup)
                if advice:
                    yield plain(advice, "warning", widget_id("scratch-advice", name))
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
        """Return a choice of the machine's probed disks (with size, filesystem, and whether they are in use),
        after `first`. A device in the file that the probe did not find (or a machine not probed) is listed as
        it is."""
        facts = self.state.facts.get(machine)
        options: list[tuple[Text, str]] = [(Text(first[0]), first[1])]
        if facts is not None and facts.error is None:
            options += [(Text(disk_label(disk)), disk.path) for disk in facts.disks]
        if value is not None and value not in [option[1] for option in options]:
            options.append((Text(f"{value} (from the file)"), value))
        if facts is None:
            options.append((Text("(probe the machine in step 1 to list its disks)"), "probe"))
        selected = value if value is not None else first[1] if first[1] == "root" else Select.NULL
        return Select(options, value=selected, prompt="choose", id=id)

    @on(Select.Changed, "#home-machine")
    async def home_machine(self, event: Select.Changed) -> None:
        """Move the home role to the chosen machine."""
        if event.value is Select.NULL:
            return
        machines = self.state.file.machines()
        chosen = str(event.value)
        if "home" in roles_of(machines[chosen]):
            return
        for name, machine in machines.items():
            roles = roles_of(machine)
            if "home" in roles:
                self.state.file.set_machine(name, with_roles(machine or {}, [role for role in roles if role != "home"]))
        machine = machines[chosen] or {}
        self.state.file.set_machine(chosen, {**machine, "roles": [*roles_of(machine), "home"]})
        self.changed()
        await self.reload()

    @on(Select.Changed, "#home-device")
    async def home_device(self, event: Select.Changed) -> None:
        """Use the chosen disk for /home (or the root disk)."""
        machines = self.state.file.machines()
        home = next(name for name, machine in machines.items() if "home" in roles_of(machine))
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
        name = self.scratch_ids.get(event.select.id or "")
        if name is None or event.value in (Select.NULL, "probe"):
            return
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
        self.post_message(Edited())
        await self.reload()
