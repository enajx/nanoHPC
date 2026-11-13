"""CrowdSec's fields, shown in Extras (Slurm mode) and in Alerts (monitor mode)."""

from collections.abc import Iterator

from textual.widget import Widget
from textual.widgets import Static

from nanohpc.wizard.common import BoundInput, BoundSwitch, field
from nanohpc.wizard.state import WizardState


def crowdsec_fields(state: WizardState) -> Iterator[Widget]:
    """Yield CrowdSec's subheading, its switch, the never-block networks, and a hint."""
    yield Static("CrowdSec", classes="subheading")
    yield field("enabled", BoundSwitch(state, ["crowdsec", "enabled"], "crowdsec-enabled"))
    yield field(
        "never block",
        BoundInput(state, ["crowdsec", "never_block"], "list", "192.0.2.0/24, 198.51.100.7", "crowdsec-never-block"),
    )
    yield Static(
        "Blocks addresses that attack SSH, for 4 hours, on every machine. It reads only the SSH log, so it never "
        "blocks Slurm, the website, Grafana, or the monitoring, and it sends nothing to CrowdSec's central service. "
        "The machines in this file are never blocked; add your own networks (comma-separated) above.",
        classes="hint",
    )
