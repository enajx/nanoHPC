"""Gate daily Ubuntu security updates on a successful, reviewed manual update."""

import re
from pathlib import Path

from nanohpc import restart

MARKER = Path("/var/lib/nanohpc/automatic-security-updates-enabled")
CONFIG = Path("/etc/apt/apt.conf.d/99nanohpc-auto-updates")

# These package families are always left to an administrator's confirmed update.
NVIDIA_CUDA = re.compile(r"nvidia|cuda")

ACTIVATE_PROGRAM = f"""
import os
import subprocess
from pathlib import Path

marker = Path({str(MARKER)!r})
config = Path({str(CONFIG)!r})
disabled = 'APT::Periodic::Unattended-Upgrade "0";'
enabled = 'APT::Periodic::Unattended-Upgrade "1";'
contents = config.read_text()
if disabled not in contents and enabled not in contents:
    raise RuntimeError('nanoHPC automatic update setting is missing')
if disabled in contents:
    temporary = config.with_suffix('.new')
    temporary.write_text(contents.replace(disabled, enabled))
    temporary.chmod(0o644)
    os.replace(temporary, config)
checked = subprocess.run(['/usr/local/sbin/nanohpc-auto-updates-check', '--activating'], check=False)
if checked.returncode:
    temporary = config.with_suffix('.new')
    temporary.write_text(contents if marker.exists() else contents.replace(enabled, disabled))
    temporary.chmod(0o644)
    os.replace(temporary, config)
    raise SystemExit(checked.returncode)
marker.write_text('enabled after a confirmed nanohpc update\\n')
"""


def waiting_security(packages: list[str]) -> list[str]:
    """List eligible security packages still waiting after a manual update."""
    return sorted(name for name in packages if not NVIDIA_CUDA.search(name))


def activate(machine: str, ssh_config: Path | None) -> None:
    """Mark one updated machine ready and enable its managed daily upgrade setting."""
    restart.root(machine, ssh_config, ["/usr/bin/python3", "-c", ACTIVATE_PROGRAM], 30)
    setting = restart.read(machine, ssh_config, "apt-config shell enabled APT::Periodic::Unattended-Upgrade", 30)
    if setting != "enabled='1'":
        raise RuntimeError(f"{machine}: automatic security update setting did not become active: {setting}")
    restart.root(machine, ssh_config, ["/usr/local/sbin/nanohpc-auto-updates-check"], 30)
