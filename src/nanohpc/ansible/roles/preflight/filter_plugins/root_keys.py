"""Ansible filter for the preflight check of root's own SSH key files (tasks/root_keys.yml).

nanoHPC makes sshd read root's keys only from /etc/ssh/authorized_keys/root, which holds the administrators' keys
from cluster.yml. A key in /root/.ssh that is not an administrator's key would stop working for root then.
Keys are compared by type and key data only: options and comments do not matter.
"""

import base64
import hashlib
import re
from typing import Any

# Key types start like this (ssh-ed25519, ssh-rsa, ecdsa-sha2-nistp256, sk-ssh-ed25519@openssh.com, and their
# certificates); no authorized_keys option does.
KEY_TYPE = re.compile(r"(ssh|ecdsa|sk)-\S+")
KEY_DATA = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def without_options(line: str) -> str:
    """Return an authorized_keys line without its leading options (`from="a b",no-pty ssh-ed25519 ...`): the
    options end at the first space outside double quotes, and a backslash escapes a quote inside them."""
    quoted = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == "\\" and quoted:
            index += 2
            continue
        if character == '"':
            quoted = not quoted
        elif character in " \t" and not quoted:
            return line[index:].lstrip()
        index += 1
    return ""


def key_parts(line: str) -> tuple[str, str, str] | None:
    """Return the type, base64 data, and comment of an authorized_keys line, or None when it holds no key that
    can be read."""
    text = line.strip()
    if not KEY_TYPE.fullmatch(text.split()[0]):
        text = without_options(text)
    parts = text.split(None, 2)
    if len(parts) < 2 or not KEY_TYPE.fullmatch(parts[0]) or not KEY_DATA.fullmatch(parts[1]) or len(parts[1]) % 4:
        return None
    return parts[0], parts[1], parts[2].strip() if len(parts) == 3 else ""


def fingerprint(data: str) -> str:
    """Return a key's SHA256 fingerprint, as ssh-keygen -l prints it."""
    digest = hashlib.sha256(base64.b64decode(data)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def root_keys_to_lose(files: list[dict[str, Any]], admin_keys: list[str]) -> list[str]:
    """Describe each key in root's key files that is not one of `admin_keys`, and each line that holds no key
    nanoHPC can read: `PATH: TYPE FINGERPRINT COMMENT` or `PATH line N: not a key line ...`.
    `files` are slurp results (`source`, base64 `content`); skipped ones (no `content`) are not read."""
    allowed = set()
    for key in admin_keys:
        parts = key_parts(key)
        if parts is not None:
            allowed.add(parts[:2])
    found = []
    for file in files:
        if "content" not in file:
            continue
        text = base64.b64decode(file["content"]).decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip() or line.strip().startswith("#"):
                continue
            parts = key_parts(line)
            if parts is None:
                found.append(f"{file['source']} line {number}: not a key line nanoHPC can read")
            elif parts[:2] not in allowed:
                key_type, data, comment = parts
                found.append(f"{file['source']}: {key_type} {fingerprint(data)} {comment or '(no comment)'}")
    return found


class FilterModule:
    """The filters of this role."""

    def filters(self) -> dict[str, Any]:
        return {"root_keys_to_lose": root_keys_to_lose}
