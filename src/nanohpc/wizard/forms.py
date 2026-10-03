"""Steps 4 to 6: partitions and policy, the website, and the extras (backup, alerts, automatic deploys)."""

import importlib.metadata
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Select, Static

from nanohpc.config import POLICY_DEFAULTS
from nanohpc.wizard.common import BoundInput, BoundSwitch, Step, cell, field
from nanohpc.wizard.dialogs import ConfirmScreen, PartitionForm, PartitionScreen, plain
from nanohpc.wizard.state import field_text, remove_value


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
        table = self.query_one("#partition-table", DataTable)
        table.clear()
        for name, partition in self.state.file.partitions().items():
            partition = partition or {}
            table.add_row(
                cell(name),
                cell("yes" if partition.get("default") is True else ""),
                cell(partition.get("jobs", "any")),
                cell(field_text(partition.get("max_time"))),
                cell(field_text(partition.get("max_gpus_per_user", "unlimited"))),
                cell(", ".join(self.members(name))),
                key=name,
            )

    @on(Button.Pressed, "#add-partition")
    def action_add(self) -> None:
        """Ask for a new partition."""
        taken = list(self.state.file.partitions())
        self.app.push_screen(PartitionScreen(None, {}, [], self.compute(), taken), self.saved)

    @on(DataTable.RowSelected, "#partition-table")
    @on(Button.Pressed, "#edit-partition")
    def edit(self) -> None:
        """Edit the partition under the cursor."""
        name = self.selected("#partition-table")
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
        self.fill()

    @on(Button.Pressed, "#remove-partition")
    def action_remove(self) -> None:
        """Remove the partition under the cursor (and from its machines), after asking."""
        name = self.selected("#partition-table")
        if name is None:
            return

        def remove(answer: str | None) -> None:
            if answer != "yes":
                return
            file = self.state.file
            members = self.members(name)
            file.remove_partition(name)
            for machine_name in members:
                machine = file.machines()[machine_name] or {}
                partitions = [item for item in machine.get("partitions") or [] if item != name]
                file.set_machine(machine_name, {**machine, "partitions": partitions})
            self.changed()
            self.fill()

        self.app.push_screen(ConfirmScreen(f"Remove the partition {name}?"), remove)


class WebsiteStep(Step):
    """5 Website: the cluster's name, hostname, path, HTTPS, logo, and who can open it."""

    def compose(self) -> ComposeResult:
        """Show the website's fields and the notes about access."""
        state = self.state
        website = ["cluster", "website"]
        yield self.heading()
        yield field("cluster name", BoundInput(state, ["cluster", "name"], "required", "mylab", "cluster-name-field"))
        yield field(
            "hostname", BoundInput(state, [*website, "hostname"], "required", "cluster.example.org", "website-hostname")
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
                BoundInput(state, [*website, "certificate"], "optional", "cert.pem", "website-certificate"),
            )
            yield field(
                "certificate key file",
                BoundInput(state, [*website, "certificate_key"], "optional", "key.pem", "website-certificate_key"),
            )
            yield Static("Paths on this computer, relative to the folder of cluster.yml.", classes="hint")
        else:
            yield Static(
                "Let's Encrypt needs the hostname to reach the front node on port 80 from the internet.",
                classes="hint",
            )
        yield field("logo (optional)", BoundInput(state, [*website, "logo"], "optional", "logo.png", "website-logo"))
        yield field(
            "who can open it",
            BoundInput(state, [*website, "allow"], "list", "anyone; or networks like 10.0.0.0/8", "website-allow"),
        )
        yield Static(
            "Recommended, not required: a private network such as WireGuard or Tailscale, for security and "
            "simpler administrator access. Then limit the site to it above.",
            classes="note",
        )
        yield plain(
            "To show the cluster site on the lab's own website (labwebsite.com/cluster/), "
            f"nanohpc forwarding-rules {state.path} prints the rules for its web server (nginx, Apache, Caddy).",
            "hint",
            None,
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
                placeholder=" or ".join([*backup_machines, "user@host:/path"]),
                id="backup-to",
            ),
        )
        if state.file.get(["backup"]) is not None:
            with Horizontal(classes="buttons"):
                yield Button("No backup", id="no-backup", classes="secondary")
        yield Static("Alerts", classes="subheading")
        yield field("Slack alerts", BoundSwitch(state, ["alerts", "slack"], "alerts-slack"))
        yield plain(
            f"The Slack webhook goes in .env next to {state.path.name}: "
            "NANOHPC_SLACK_WEBHOOK=https://hooks.slack.com/... (never commit .env).",
            "hint",
            None,
        )
        yield Static("Automatic deploys", classes="subheading")
        auto = ["auto_deploy"]
        yield field("enabled", BoundSwitch(state, [*auto, "enabled"], "auto-enabled"))
        yield field(
            "repository (SSH)",
            BoundInput(
                state, [*auto, "repository"], "optional", "git@github.com:lab/cluster-config.git", "auto-repository"
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
        """Write the backup target. Empty text removes only the target (the backup's time and exclusions
        stay); "No backup" removes the whole backup section."""
        text = event.value.strip()
        if text == field_text(self.state.file.get(["backup", "to"])):
            return
        if text:
            self.state.file.set_value(["backup", "to"], text)
        else:
            remove_value(self.state.file, ["backup", "to"])
        self.changed()

    @on(Button.Pressed, "#no-backup")
    def no_backup(self) -> None:
        """Turn the backup off: remove the backup section, after asking."""

        async def answer(choice: str | None) -> None:
            if choice == "yes":
                remove_value(self.state.file, ["backup"])
                self.changed()
                await self.reload()

        self.app.push_screen(ConfirmScreen("Turn the nightly backup off (remove the backup section)?"), answer)
