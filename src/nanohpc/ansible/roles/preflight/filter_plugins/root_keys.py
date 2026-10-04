"""Ansible filter for the preflight check of root's own SSH key files (tasks/root_keys.yml).

nanoHPC makes sshd read root's keys only from /etc/ssh/authorized_keys/root, which holds the administrators' keys
from cluster.yml. A key in /root/.ssh that is not an administrator's key would stop working for root then.
Keys are compared by type and key data only: options and comments do not matter, except cloud-init's forced
command that only tells root to log in as another user, which gives no root access today.
"""

import base64
import hashlib
import re
from typing import Any

# Key types start like this (ssh-ed25519, ssh-rsa, ecdsa-sha2-nistp256, sk-ssh-ed25519@openssh.com, and their
# certificates); no authorized_keys option does.
KEY_TYPE = re.compile(r"(ssh|ecdsa|sk)-\S+")
KEY_DATA = re.compile(r"[A-Za-z0-9+/]+={0,2}")
# cloud-init with disable_root (the default on Ubuntu cloud images) gives root the default user's keys with
# DISABLE_USER_OPTS (cloudinit/ssh_util.py): command="echo 'Please login as the user \"ubuntu\" rather than the
# user \"root\".';echo;sleep 10;exit 142". Such a key only prints that and logs out. A command that has this text
# but also runs something else (exec, the client's command) gives root access and counts.
CLOUD_INIT_REFUSAL = "Please login as the user"
CLOUD_INIT_END = "exit 142"
RUNS_SOMETHING_ELSE = ("exec", "SSH_ORIGINAL_COMMAND")
# nanoHPC's own file of root's keys (accounts role), which this deploy writes.
NANOHPC_ROOT_KEYS = "/etc/ssh/authorized_keys/root"
ROOT_HOME = "/root"


def split_options(line: str) -> tuple[str, str]:
    """Split an authorized_keys line into its leading options (`from="a b",no-pty`) and the rest: the options end
    at the first space outside double quotes, and a backslash escapes a quote inside them."""
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
            return line[:index], line[index:].lstrip()
        index += 1
    return line, ""


def forced_command(options: str) -> str:
    """Return the command="..." option's command, without its quotes and escapes ("" when there is none)."""
    match = re.search(r'(?:^|,)command="((?:[^"\\]|\\.)*)"', options, re.IGNORECASE)
    return re.sub(r"\\(.)", r"\1", match.group(1)) if match else ""


def cloud_init_refusal(options: str) -> bool:
    """Whether a key's options force cloud-init's command that only tells root to log in as another user."""
    command = forced_command(options)
    return (
        CLOUD_INIT_REFUSAL in command
        and command.endswith(CLOUD_INIT_END)
        and not any(word in command for word in RUNS_SOMETHING_ELSE)
    )


def key_parts(line: str) -> tuple[str, str, str, str] | None:
    """Return the options, type, base64 data, and comment of an authorized_keys line, or None when it holds no key
    that can be read."""
    text = line.strip()
    options = ""
    if not KEY_TYPE.fullmatch(text.split()[0]):
        options, text = split_options(text)
    parts = text.split(None, 2)
    if len(parts) < 2 or not KEY_TYPE.fullmatch(parts[0]) or not KEY_DATA.fullmatch(parts[1]) or len(parts[1]) % 4:
        return None
    return options, parts[0], parts[1], parts[2].strip() if len(parts) == 3 else ""


def fingerprint(data: str) -> str:
    """Return a key's SHA256 fingerprint, as ssh-keygen -l prints it."""
    digest = hashlib.sha256(base64.b64decode(data)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def root_keys_to_lose(files: list[dict[str, Any]], admin_keys: list[str]) -> list[str]:
    """Describe each key in root's key files that is not one of `admin_keys`, and each line that holds no key
    nanoHPC can read: `PATH: TYPE FINGERPRINT COMMENT` or `PATH line N: not a key line ...`. A key whose forced
    command is cloud-init's refusal is left out: it gives no root access today.
    `files` are slurp results (`source`, base64 `content`); skipped ones (no `content`) are not read."""
    allowed = set()
    for key in admin_keys:
        parts = key_parts(key)
        if parts is not None:
            allowed.add(parts[1:3])
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
            elif parts[1:3] not in allowed and not cloud_init_refusal(parts[0]):
                _, key_type, data, comment = parts
                found.append(f"{file['source']}: {key_type} {fingerprint(data)} {comment or '(no comment)'}")
    return found


def root_key_files(sshd_settings: list[str]) -> list[str]:
    """Return the files sshd reads root's keys from, from the authorizedkeysfile line of `sshd -T` for root, as
    sshd expands them (%h root's home, %u root, %U 0, %% a %; a relative path is in root's home), without
    nanoHPC's own file. Empty when sshd reads only that file."""
    lines = [line for line in sshd_settings if line.startswith("authorizedkeysfile ")]
    if len(lines) != 1:
        raise ValueError(f"sshd -T for root printed {len(lines)} authorizedkeysfile lines, not one")
    tokens = {"h": ROOT_HOME, "u": "root", "U": "0", "%": "%"}
    files = []
    for path in lines[0].split()[1:]:
        if path == "none":
            continue
        expanded = re.sub(r"%(.)", lambda match: tokens[match.group(1)], path)
        full = expanded if expanded.startswith("/") else f"{ROOT_HOME}/{expanded}"
        if full != NANOHPC_ROOT_KEYS and full not in files:
            files.append(full)
    return files


class FilterModule:
    """The filters of this role."""

    def filters(self) -> dict[str, Any]:
        return {"root_keys_to_lose": root_keys_to_lose, "root_key_files": root_key_files}
