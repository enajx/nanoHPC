"""The wizard's dialogs: the cluster name, a machine, a user, a partition, a fix-uid plan, and yes/no."""

from dataclasses import dataclass
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from nanohpc.config import CLUSTER_NAME, JOB_TYPES, MACHINE_NAME, ROLES, USER_NAME
from nanohpc.probe import MachineFacts
from nanohpc.wizard.state import (
    cpu_from_facts,
    field_text,
    gpu_from_facts,
    ordered,
    parse_field,
    suggested_memory_mb,
    with_roles,
)

DEFAULT_BACKUP_PATH = "/srv/nanohpc-backup"


def buttons(ok_label: str) -> Horizontal:
    """Return the OK and Cancel buttons of a dialog."""
    return Horizontal(Button(ok_label, id="ok"), Button("Cancel", id="cancel", classes="secondary"), classes="buttons")


class Dialog[ResultType](ModalScreen[ResultType]):
    """A dialog box in the middle of the screen; escape or Cancel closes it without a result."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "cancel")]

    def action_cancel(self) -> None:
        """Close without a result."""
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self) -> None:
        """Close without a result."""
        self.dismiss(None)

    def fail(self, message: str) -> None:
        """Show why the form cannot be accepted."""
        self.query_one("#form-error", Static).update(message)

    def value(self, selector: str) -> str:
        """Return the stripped text of an input."""
        return self.query_one(selector, Input).value.strip()


class NameScreen(Dialog[str | None]):
    """Ask the name of a new cluster."""

    def compose(self) -> ComposeResult:
        """Show the name field."""
        with Vertical(classes="dialog"):
            yield Static("NEW CLUSTER", classes="heading")
            yield Label("The cluster's name (shown in the website, dashboards, and Slurm):")
            yield Input(placeholder="mylab", id="cluster-name")
            yield Static("", id="form-error", classes="form-error")
            yield Horizontal(
                Button("Create", id="create"), Button("Quit", id="cancel", classes="secondary"), classes="buttons"
            )

    @on(Button.Pressed, "#create")
    @on(Input.Submitted)
    def create(self) -> None:
        """Accept a valid name."""
        name = self.value("#cluster-name")
        if not CLUSTER_NAME.fullmatch(name):
            self.fail("Use lowercase letters, digits, and -, starting with a letter (at most 40 characters).")
            return
        self.dismiss(name)


class AddMachineScreen(Dialog[tuple[str, str] | None]):
    """Ask a new machine's name in cluster.yml and how to reach it with ssh."""

    def __init__(self, taken: list[str]) -> None:
        super().__init__()
        self.taken = taken

    def compose(self) -> ComposeResult:
        """Show the name and ssh target fields."""
        with Vertical(classes="dialog"):
            yield Static("ADD MACHINE", classes="heading")
            yield Label("Name in cluster.yml (letters, digits, _ or -):")
            yield Input(placeholder="gpu1", id="machine-name")
            yield Label("SSH host: a name, an address, or a Host from your ~/.ssh/config (empty: the name):")
            yield Input(placeholder="10.0.0.11", id="machine-target")
            yield Static(
                "nanohpc deploy reaches each machine with `ssh <name>`: the name must work with ssh "
                "(a Host in your SSH config, or a DNS name). The wizard probes the machine now (read-only).",
                classes="note",
            )
            yield Static("", id="form-error", classes="form-error")
            yield buttons("Add and probe")

    @on(Button.Pressed, "#ok")
    @on(Input.Submitted)
    def accept(self) -> None:
        """Accept a new, valid name."""
        name = self.value("#machine-name")
        if not MACHINE_NAME.fullmatch(name):
            self.fail("The name must be letters, digits, _ or -, starting with a letter.")
            return
        if name in self.taken:
            self.fail(f"{name} is already a machine.")
            return
        self.dismiss((name, self.value("#machine-target") or name))


class MachineScreen(Dialog[dict[str, Any] | None]):
    """Edit one machine: address, roles, and for compute machines CPUs, memory, and GPUs. The result is the
    machine's new fields."""

    def __init__(self, name: str, machine: dict[str, Any], facts: MachineFacts | None, partition: str | None) -> None:
        super().__init__()
        self.name_ = name
        self.machine = machine
        self.facts = facts if facts is not None and facts.error is None else None
        self.partition = partition

    def compose(self) -> ComposeResult:
        """Show the machine's fields, filled from the file, else from the probe."""
        machine, facts = self.machine, self.facts
        roles = machine.get("roles") or []
        cpu = machine.get("cpu") or (cpu_from_facts(facts) if facts else {})
        memory = machine.get("memory_mb") or (suggested_memory_mb(facts) if facts else None)
        gpu = machine.get("gpu") or (gpu_from_facts(facts) if facts else None) or {}
        with VerticalScroll(classes="dialog"):
            yield Static(f"MACHINE {self.name_}", classes="heading")
            yield Label("Address on the cluster network (IPv4):")
            yield Input(field_text(machine.get("address")), id="address")
            if facts is not None and facts.addresses:
                address = machine.get("address")
                yield Label("or choose one the probe found:")
                yield Select(
                    [(item, item) for item in facts.addresses],
                    prompt="probed addresses",
                    value=address if address in facts.addresses else Select.NULL,
                    id="probed-address",
                )
            yield Label("Roles:")
            with Horizontal(classes="roles"):
                for role in ROLES:
                    yield Checkbox(role, role in roles, id=f"role-{role}")
            yield Static(
                "front: login, Slurm controller, monitoring, website (one machine). compute: runs jobs. "
                "home: serves /home (the front node, or a storage machine). backup: receives the nightly backup.",
                classes="hint",
            )
            with Vertical(id="compute-fields"):
                yield Static("Compute (from the probe; change if needed)", classes="subheading")
                yield self.row("CPU sockets", "sockets", cpu.get("sockets"))
                yield self.row("cores per socket", "cores", cpu.get("cores_per_socket"))
                yield self.row("threads per core", "threads", cpu.get("threads_per_core"))
                yield self.row("memory MB", "memory", memory)
                yield self.row("GPU type", "gpu-type", gpu.get("type"))
                yield self.row("GPU count", "gpu-count", gpu.get("count"))
                if facts is not None and facts.gpus:
                    yield Static(f"Probed GPUs: {', '.join(facts.gpus)}", classes="hint")
            with Vertical(id="backup-fields"):
                yield Label("Backup folder on this machine:")
                yield Input((machine.get("backup") or {}).get("path") or DEFAULT_BACKUP_PATH, id="backup-path")
            yield Static("", id="form-error", classes="form-error")
            yield buttons("Save")

    def row(self, label: str, id: str, value: Any) -> Horizontal:
        """Return a form row: a label and an input."""
        return Horizontal(Label(label, classes="field-label"), Input(field_text(value), id=id), classes="row")

    def on_mount(self) -> None:
        """Show the fields of the checked roles only."""
        self.show_role_fields()

    @on(Checkbox.Changed)
    def show_role_fields(self) -> None:
        """Show the compute fields for a compute machine, the backup folder for a backup machine."""
        self.query_one("#compute-fields").display = self.query_one("#role-compute", Checkbox).value
        self.query_one("#backup-fields").display = self.query_one("#role-backup", Checkbox).value

    @on(Select.Changed, "#probed-address")
    def choose_address(self, event: Select.Changed) -> None:
        """Use the chosen probed address."""
        if event.value is not Select.NULL:
            self.query_one("#address", Input).value = str(event.value)

    @on(Button.Pressed, "#ok")
    def accept(self) -> None:
        """Return the machine's new fields."""
        roles = [role for role in ROLES if self.query_one(f"#role-{role}", Checkbox).value]
        if not roles:
            self.fail("Choose at least one role.")
            return
        machine = with_roles(self.machine, roles)
        machine["address"] = self.value("#address")
        if "compute" in roles:
            machine["cpu"] = {
                "sockets": parse_field(self.value("#sockets"), "int"),
                "cores_per_socket": parse_field(self.value("#cores"), "int"),
                "threads_per_core": parse_field(self.value("#threads"), "int"),
            }
            machine["memory_mb"] = parse_field(self.value("#memory"), "int")
            gpu_type, count = self.value("#gpu-type"), self.value("#gpu-count")
            if gpu_type or count not in ("", "0"):
                machine["gpu"] = {"type": gpu_type, "count": parse_field(count, "int")}
            else:
                machine.pop("gpu", None)
            if not machine.get("partitions") and self.partition is not None:
                machine["partitions"] = [self.partition]
        if "backup" in roles:
            machine["backup"] = {"path": self.value("#backup-path")}
        self.dismiss(ordered(machine))


@dataclass(frozen=True)
class UserForm:
    """A user as entered in the user dialog."""

    name: str
    uid: int
    ssh_keys: list[str]
    admin: bool


class UserScreen(Dialog[UserForm | None]):
    """Add or edit a user: name, UID, SSH public keys, and whether they are an administrator."""

    def __init__(self, user: dict[str, Any] | None, admin: bool, uid: int, taken: list[str]) -> None:
        super().__init__()
        self.user = user
        self.admin = admin
        self.uid = uid
        self.taken = taken

    def compose(self) -> ComposeResult:
        """Show the user's fields."""
        user = self.user or {}
        with VerticalScroll(classes="dialog"):
            yield Static("USER" if self.user is None else f"USER {user['name']}", classes="heading")
            yield Label("Linux user name:")
            yield Input(user.get("name", ""), placeholder="alice", id="user-name", disabled=self.user is not None)
            yield Label("UID (the same on every machine; suggested: the next free one from 2000):")
            yield Input(field_text(user.get("uid", self.uid)), id="user-uid")
            yield Label("SSH public keys, one per line (ssh-ed25519 AAAA... comment):")
            yield TextArea("\n".join(user.get("ssh_keys") or []), id="user-keys")
            yield Checkbox("administrator (sudo and SSH on every machine)", self.admin, id="user-admin")
            yield Static("", id="form-error", classes="form-error")
            yield buttons("Save")

    @on(Button.Pressed, "#ok")
    def accept(self) -> None:
        """Return the user, if the name and UID are usable."""
        name, uid = self.value("#user-name"), self.value("#user-uid")
        if not USER_NAME.fullmatch(name):
            self.fail("The name must be a lowercase Linux user name.")
            return
        if self.user is None and name in self.taken:
            self.fail(f"{name} is already a user.")
            return
        if not uid.isdigit():
            self.fail("The UID must be a whole number, like 2000.")
            return
        keys = [line.strip() for line in self.query_one("#user-keys", TextArea).text.splitlines() if line.strip()]
        self.dismiss(UserForm(name, int(uid), keys, self.query_one("#user-admin", Checkbox).value))


@dataclass(frozen=True)
class PartitionForm:
    """A partition as entered in the partition dialog: its fields and the compute machines in it."""

    name: str
    values: dict[str, Any]
    members: list[str]


class PartitionScreen(Dialog[PartitionForm | None]):
    """Add or edit a partition (a Slurm queue) and choose its compute machines."""

    def __init__(
        self, name: str | None, partition: dict[str, Any], members: list[str], compute: list[str], taken: list[str]
    ) -> None:
        super().__init__()
        self.name_ = name
        self.partition = partition
        self.members = members
        self.compute = compute
        self.taken = taken

    def compose(self) -> ComposeResult:
        """Show the partition's fields."""
        partition = self.partition
        with VerticalScroll(classes="dialog"):
            yield Static("PARTITION" if self.name_ is None else f"PARTITION {self.name_}", classes="heading")
            yield Label("Name (lowercase):")
            yield Input(self.name_ or "", placeholder="interactive", id="partition-name", disabled=bool(self.name_))
            yield Checkbox(
                "default partition (jobs go here unless they ask for another)",
                partition.get("default") is True,
                id="partition-default",
            )
            yield Label("Jobs: any, batch (sbatch only), or interactive (only the shell):")
            yield Select(
                [(kind, kind) for kind in JOB_TYPES],
                value=partition.get("jobs", "any"),
                allow_blank=False,
                id="partition-jobs",
            )
            yield Label('Longest run time (Slurm time, like "24:00:00" or "7-00:00:00"):')
            yield Input(field_text(partition.get("max_time", "24:00:00")), id="partition-max-time")
            yield Label("Most GPUs one user may use at once (empty: unlimited):")
            yield Input(field_text(partition.get("max_gpus_per_user")), id="partition-max-gpus")
            yield Label("Compute machines in this partition:")
            for machine in self.compute:
                yield Checkbox(machine, machine in self.members, id=f"member-{machine}")
            yield Static("", id="form-error", classes="form-error")
            yield buttons("Save")

    @on(Button.Pressed, "#ok")
    def accept(self) -> None:
        """Return the partition's fields and machines."""
        name = self.value("#partition-name")
        if not name:
            self.fail("The partition needs a name.")
            return
        if self.name_ is None and name in self.taken:
            self.fail(f"{name} is already a partition.")
            return
        values: dict[str, Any] = {}
        if self.query_one("#partition-default", Checkbox).value:
            values["default"] = True
        jobs = self.query_one("#partition-jobs", Select).value
        if jobs != "any" or "jobs" in self.partition:
            values["jobs"] = jobs
        values["max_time"] = self.value("#partition-max-time")
        gpus = self.value("#partition-max-gpus")
        if gpus and gpus != "unlimited":
            values["max_gpus_per_user"] = parse_field(gpus, "int")
        members = [machine for machine in self.compute if self.query_one(f"#member-{machine}", Checkbox).value]
        self.dismiss(PartitionForm(name, values, members))


class PlanScreen(Dialog[bool | None]):
    """Show a fix-uid plan (found read-only) and apply it only when the administrator confirms."""

    def __init__(self, title: str, plan: str, ok: bool) -> None:
        super().__init__()
        self.title_ = title
        self.plan = plan
        self.ok = ok

    def compose(self) -> ComposeResult:
        """Show the plan and the Apply and Cancel buttons."""
        with VerticalScroll(classes="dialog wide"):
            yield Static(self.title_, classes="heading")
            yield Static(self.plan, id="plan-text", classes="command")
            if self.ok:
                yield Static(
                    "Nothing has changed yet. Apply runs these commands as root on the machine "
                    "(the same as nanohpc fix-uid --apply), then checks the UIDs again.",
                    classes="warning",
                )
            else:
                yield Static("This plan cannot proceed: fix the reasons above first.", classes="warning")
            with Horizontal(classes="buttons"):
                yield Button("Apply on the machine", id="apply", disabled=not self.ok, classes="danger")
                yield Button("Cancel", id="cancel", classes="secondary")

    @on(Button.Pressed, "#apply")
    def apply(self) -> None:
        """Confirm."""
        self.dismiss(True)


class ConfirmScreen(Dialog[bool | None]):
    """Ask yes or no."""

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        """Show the question."""
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield buttons("Yes")

    @on(Button.Pressed, "#ok")
    def yes(self) -> None:
        """Answer yes."""
        self.dismiss(True)
