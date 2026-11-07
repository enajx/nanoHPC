"""The optional Slack agent's fields, shown in Extras (Slurm mode) and in Alerts (monitor mode)."""

import importlib.metadata
from collections.abc import Iterator

from textual.widget import Widget
from textual.widgets import Input, Static

from nanohpc.wizard.common import BoundInput, BoundSwitch, Edited, field
from nanohpc.wizard.state import WizardState, remove_value


def approvers_text(value: object) -> str:
    """Show the approvers as `name=SLACK_ID, ...`."""
    if not isinstance(value, list):
        return ""
    parts = []
    for item in value:
        if isinstance(item, dict):
            parts.append(
                f"{item.get('name', '')}={item.get('slack_id', '')}"
                if "slack_id" in item
                else str(item.get("name", ""))
            )
    return ", ".join(parts)


def parse_approvers(text: str) -> list[dict[str, str]]:
    """Read `name=SLACK_ID, ...`; an entry without `=` keeps only its name, so validation names what is missing."""
    approvers = []
    for part in (part.strip() for part in text.split(",")):
        if not part:
            continue
        name, _, member = (piece.strip() for piece in part.partition("="))
        approvers.append({"name": name, "slack_id": member} if member else {"name": name})
    return approvers


class ApproversInput(Input):
    """The Slack users who approve the agent's changes and pushes, as `name=SLACK_ID, ...`."""

    def __init__(self, state: WizardState) -> None:
        super().__init__(
            approvers_text(state.file.get(["agent", "approvers"])),
            placeholder="alice=U0123ABC, sam=U0456DEF",
            id="agent-approvers",
        )
        self.state = state

    def on_input_changed(self, event: Input.Changed) -> None:
        """Write the approvers list (empty text removes it)."""
        approvers = parse_approvers(event.value)
        if approvers == (self.state.file.get(["agent", "approvers"]) or []):
            return
        if approvers:
            self.state.file.set_value(["agent", "approvers"], approvers)
        else:
            remove_value(self.state.file, ["agent", "approvers"])
        self.post_message(Edited())


def agent_fields(state: WizardState, monitor: bool) -> Iterator[Widget]:
    """Yield the Slack agent's subheading, fields, and hints."""
    yield Static("Slack agent", classes="subheading")
    yield field("enabled", BoundSwitch(state, ["agent", "enabled"], "agent-enabled"))
    yield field("channel ID", BoundInput(state, ["agent", "channel"], "optional", "C0123ABC", "agent-channel"))
    yield field("approvers", ApproversInput(state))
    if monitor:
        yield field(
            "repository (SSH)",
            BoundInput(
                state, ["agent", "repository"], "optional", "git@github.com:lab/monitor-config.git", "agent-repository"
            ),
        )
        yield field(
            "nanoHPC version",
            BoundInput(
                state, ["nanohpc_version"], "text", importlib.metadata.version("nanohpc"), "agent-nanohpc-version"
            ),
        )
    yield Static(
        "Answers @-mentions in that channel. Approvers' changes run at once; anyone else's wait for an approver's "
        "Approve. With a repository (monitor mode), it pushes edits of this file there after an Approve and deploys "
        "them when asked, installing the nanoHPC version above. "
        "Limits: $1 a day, $1 a request, 10 requests a person (agent.daily_usd, request_usd, "
        f"requests_per_person). Put NANOHPC_SLACK_BOT_TOKEN, NANOHPC_SLACK_APP_TOKEN, and NANOHPC_ANTHROPIC_API_KEY "
        f"in .env next to {state.path.name}.",
        classes="hint",
    )
