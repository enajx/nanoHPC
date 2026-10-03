"""Step 3: the users, and the UID check on the probed machines (with fix-uid after confirmation)."""

import shlex
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from nanohpc.fixuid import FixPlan, format_plan
from nanohpc.wizard.common import Step, cell
from nanohpc.wizard.dialogs import (
    ConfirmScreen,
    MessageScreen,
    PlanScreen,
    UserForm,
    UserScreen,
    WorkingScreen,
    plain,
)
from nanohpc.wizard.state import (
    UidCheck,
    WizardState,
    field_text,
    next_free_uid,
    suggest_uid,
    uid_conflicts,
    wizard_errors,
)


class UsersStep(Step):
    """3 Users: names, UIDs, SSH keys, administrators, and the UID check on every probed machine. The check
    runs by itself when the step opens and after each probe."""

    KEYS = "a add · enter edit · d remove · c check · f fix"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("a", "add", "add"),
        ("d", "remove", "remove"),
        ("c", "check", "check"),
        ("f", "fix", "fix"),
    ]

    def __init__(self, state: WizardState, index: int) -> None:
        super().__init__(state, index)
        self.conflict_keys: dict[str, tuple[str, str]] = {}  # uid-table row key -> (machine, user)
        self.working: WorkingScreen | None = None
        self.applying: FixPlan | None = None

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
        yield plain("", "hint", "uid-summary")
        conflicts: DataTable = DataTable(id="uid-table", cursor_type="row")
        conflicts.add_columns("machine", "user", "on the machine (UID, GID)", "cluster.yml UID")
        yield conflicts
        yield plain("", "warning", "uid-problems")
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

    def entered(self) -> None:
        """Check the probed machines that were not checked yet."""
        self.check([name for name in self.state.probed() if name not in self.state.uid_checks])

    def fill_tables(self) -> None:
        """Show the users and what the UID check found."""
        if not self.query("#user-table"):
            return
        admins = self.state.file.get(["cluster", "admins"]) or []
        table = self.query_one("#user-table", DataTable)
        row = table.cursor_row
        table.clear()
        for user in self.state.file.users():
            name = str(user.get("name"))
            keys = len(user.get("ssh_keys") or [])
            table.add_row(
                cell(name),
                cell(field_text(user.get("uid"))),
                cell(keys),
                cell("yes" if name in admins else ""),
                key=name,
            )
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        conflicts = self.query_one("#uid-table", DataTable)
        conflicts.clear()
        self.conflict_keys = {}
        expected = {user.get("name"): user.get("uid") for user in self.state.file.users()}
        for index, (machine, user, ids) in enumerate(uid_conflicts(self.state)):
            key = f"conflict-{index}"
            self.conflict_keys[key] = (machine, user)
            conflicts.add_row(cell(machine), cell(user), cell(f"{ids[0]}, {ids[1]}"), cell(expected[user]), key=key)
        problems = [
            f"{machine}: {problem}" for machine, check in self.state.uid_checks.items() for problem in check.problems
        ]
        widget = self.query_one("#uid-problems", Static)
        widget.update(Text("\n".join(problems)))
        widget.display = bool(problems)
        self.query_one("#uid-summary", Static).update(Text(self.summary()))

    def summary(self) -> str:
        """Return which machines the UID check covered."""
        machines = list(self.state.file.machines())
        probed = self.state.probed()
        checked = [name for name in machines if name in self.state.uid_checks]
        lines = []
        if not checked:
            lines.append("Not checked yet: the check runs on the probed machines (read-only).")
        else:
            count = len(uid_conflicts(self.state)) + sum(
                len(check.problems) for check in self.state.uid_checks.values()
            )
            lines.append(f"Checked on {', '.join(checked)}: {count} problem(s).")
        missing = [name for name in machines if name not in probed]
        if missing:
            lines.append(f"Not probed (probe them in step 1 to check): {', '.join(missing)}.")
        lines += [f"✗ {name}: {error}" for name, error in self.state.uid_errors.items()]
        return "\n".join(lines)

    def suggest(self, name: str) -> int:
        """Return the UID to suggest for a new user (runs in a thread: reads the probed machines)."""
        deps, ssh_config = self.state.deps, self.state.ssh_config
        targets = [self.state.target(machine) for machine in self.state.probed()]
        found = [deps.user_ids(target, ssh_config, [name])[name] for target in targets]

        def taken(uid: int) -> bool:
            return any(deps.uid_owner(target, ssh_config, uid) is not None for target in targets)

        return suggest_uid(self.state.file.users(), found, taken)

    @on(Button.Pressed, "#add-user")
    def action_add(self) -> None:
        """Ask for a new user."""
        users = self.state.file.users()
        taken = [str(user.get("name")) for user in users]
        suggest = self.suggest if self.state.probed() else None
        self.app.push_screen(UserScreen(None, False, next_free_uid(users), taken, suggest), self.saved)

    @on(DataTable.RowSelected, "#user-table")
    @on(Button.Pressed, "#edit-user")
    def edit(self) -> None:
        """Edit the user under the cursor."""
        name = self.selected("#user-table")
        if name is None:
            return
        user = next(user for user in self.state.file.users() if user.get("name") == name)
        admins = self.state.file.get(["cluster", "admins"]) or []
        self.app.push_screen(UserScreen(user, name in admins, 0, [], None), self.saved)

    def saved(self, form: UserForm | None) -> None:
        """Write the user, then check the UIDs again."""
        if form is None:
            return
        self.state.file.set_user(form.name, form.uid, form.ssh_keys, form.admin)
        self.changed()
        self.fill_tables()
        self.check(self.state.probed())

    @on(Button.Pressed, "#remove-user")
    def action_remove(self) -> None:
        """Remove the user under the cursor, after asking."""
        name = self.selected("#user-table")
        if name is None:
            return

        def remove(answer: str | None) -> None:
            if answer == "yes":
                self.state.file.remove_user(name)
                self.changed()
                self.fill_tables()
                self.check(self.state.probed())

        self.app.push_screen(ConfirmScreen(f"Remove the user {name} from cluster.yml?"), remove)

    @on(Button.Pressed, "#check-uids")
    def action_check(self) -> None:
        """Check every probed machine again."""
        if not self.state.probed():
            self.tell("Probe the machines first (step 1).", "warning")
            return
        self.check(self.state.probed())

    def check(self, machines: list[str]) -> None:
        """Read the users' UIDs and the UID problems on machines (read-only), in thread workers."""
        users = [
            (str(user["name"]), user["uid"])
            for user in self.state.file.users()
            if isinstance(user.get("name"), str) and isinstance(user.get("uid"), int)
        ]
        if not users:
            return
        names = [name for name, _ in users]
        deps, ssh_config = self.state.deps, self.state.ssh_config
        for machine in machines:
            self.state.uid_errors.pop(machine, None)
            target = self.state.target(machine)
            self.run_worker(
                lambda target=target: UidCheck(
                    deps.user_ids(target, ssh_config, names), deps.uid_problems(target, ssh_config, users)
                ),
                name=machine,
                group="uid-check",
                thread=True,
                exit_on_error=False,
            )

    @on(Button.Pressed, "#fix-uid")
    def action_fix(self) -> None:
        """Make the fix-uid plan for the selected conflict (read-only), then show it. Only for a saved, valid
        file: fix-uid follows the UIDs of cluster.yml."""
        key = self.selected("#uid-table")
        if key is None or key not in self.conflict_keys:
            self.tell("No UID conflict selected.", "warning")
            return
        if self.state.dirty() or wizard_errors(self.state):
            self.tell(
                "Save the file (ctrl+s) and fix its errors first: fix-uid uses the UIDs of the saved, valid "
                "cluster.yml.",
                "warning",
            )
            return
        machine, user = self.conflict_keys[key]
        uid = next(entry["uid"] for entry in self.state.file.users() if entry.get("name") == user)
        deps, ssh_config, target = self.state.deps, self.state.ssh_config, self.state.target(machine)
        self.show_working(f"reading what fix-uid would change for {user} on {machine} (read-only)")
        self.run_worker(
            lambda: deps.plan_fix(target, ssh_config, user, uid),
            name=machine,
            group="fix-plan",
            thread=True,
            exit_on_error=False,
        )

    def show_working(self, text: str) -> None:
        """Show the working dialog while a machine is busy."""
        self.working = WorkingScreen(text)
        self.app.push_screen(self.working)

    def hide_working(self) -> None:
        """Close the working dialog."""
        if self.working is not None:
            self.working.dismiss()
            self.working = None

    def show_plan(self, machine: str, plan: FixPlan) -> None:
        """Show the plan; apply it only when the administrator confirms."""

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.apply(machine, plan)

        title = f"FIX UID: {plan.user} on {machine}"
        self.app.push_screen(PlanScreen(title, format_plan(plan), plan.ok), confirmed)

    def apply(self, machine: str, plan: FixPlan) -> None:
        """Apply a confirmed plan in a thread worker; the UIDs on that machine are checked again after it."""
        deps, ssh_config, target = self.state.deps, self.state.ssh_config, self.state.target(machine)
        self.show_working(f"fix-uid is changing {plan.user} on {machine}")
        self.applying = plan
        self.run_worker(
            lambda: deps.apply_fix(target, ssh_config, plan),
            name=machine,
            group="fix-apply",
            thread=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Show the results of UID checks, fix-uid plans, and applied plans."""
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        machine, group, result = event.worker.name, event.worker.group, event.worker.result
        if group == "uid-check":
            if event.state == WorkerState.ERROR:
                self.state.uid_errors[machine] = str(event.worker.error)
            else:
                assert isinstance(result, UidCheck)
                self.state.uid_checks[machine] = result
        elif group == "fix-plan":
            self.hide_working()
            if event.state == WorkerState.ERROR:
                lines = [f"The read-only check on {machine} failed: {event.worker.error}", "Nothing was changed."]
                self.app.push_screen(MessageScreen("FIX UID", lines, True))
            else:
                assert isinstance(result, FixPlan)
                self.show_plan(machine, result)
        elif group == "fix-apply":
            self.hide_working()
            plan = self.applying
            assert plan is not None
            if event.state == WorkerState.ERROR:
                lines = [
                    f"fix-uid on {machine} stopped with an error: {event.worker.error}",
                    "Some of these commands may have run (as root, in this order):",
                    *[f"  {shlex.join(command)}" for command in plan.commands],
                    "The UIDs on the machine are checked again now; check the machine by hand.",
                ]
                self.app.push_screen(MessageScreen(f"FIX UID: {plan.user} on {machine}", lines, True))
            else:
                assert isinstance(result, list)
                self.app.push_screen(MessageScreen(f"FIX UID: {plan.user} on {machine}", result, False))
            self.check([machine])
        self.fill_tables()
