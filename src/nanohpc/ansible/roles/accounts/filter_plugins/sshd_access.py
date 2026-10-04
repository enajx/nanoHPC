"""Ansible filter for the accounts role's check of the effective SSH settings (`sshd -T`).

sshd refuses a user named by a DenyUsers pattern, or in a group named by a DenyGroups pattern, whatever the
user's keys. sshd's patterns use `*` (any characters) and `?` (one character); a DenyUsers pattern can end in
`@HOST` (after the last @), which limits it to logins from that host.
"""

import re
from typing import Any


def pattern_matches(pattern: str, name: str) -> bool:
    """Whether an sshd pattern (`*` and `?` wildcards) matches `name`."""
    expression = "".join(
        ".*" if character == "*" else "." if character == "?" else re.escape(character) for character in pattern
    )
    return re.fullmatch(expression, name) is not None


def sshd_refusals(settings: list[str], user: str, groups: list[str]) -> list[str]:
    """Return the denyusers and denygroups lines of `sshd -T` that refuse `user`, a member of `groups`. A DenyUsers
    pattern with `@HOST` counts for every host: the login may come from that host."""
    refusals = []
    for line in settings:
        keyword, _, pattern = line.partition(" ")
        user_pattern = pattern.rpartition("@")[0] if "@" in pattern else pattern
        if keyword == "denyusers" and pattern_matches(user_pattern, user) or keyword == "denygroups" and any(pattern_matches(pattern, group) for group in groups):
            refusals.append(line)
    return refusals


class FilterModule:
    """The filters of this role."""

    def filters(self) -> dict[str, Any]:
        return {"sshd_refusals": sshd_refusals}
