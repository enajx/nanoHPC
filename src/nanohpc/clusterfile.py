"""The cluster.yml model that the setup wizard edits.

The file is read and written with ruamel.yaml in round-trip mode, so comments, key order, quoting, and
blank lines survive every edit. Each editing method changes only the part of the file it is about.

ruamel.yaml stores the comment and blank lines that follow a block (for example the blank line between two
sections) on the last value inside that block. An edit at the end of a block would move that text to the
wrong place, so the editing methods take it off before the change and put it back after it (`keep_end`).
"""

import io
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedBase, CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, ScalarString
from ruamel.yaml.tokens import CommentToken

from nanohpc.config import (
    ALERTS_DEFAULTS,
    AUTO_DEPLOY_DEFAULTS,
    HOME_DEFAULTS,
    POLICY_DEFAULTS,
    SCRATCH_DEFAULTS,
    TOP_FIELDS,
    UniqueKeyLoader,
    check_config,
    secrets,
    website_files,
)

# Machine fields written on one line, like `cpu: {sockets: 1, ...}` in the examples.
FLOW_MACHINE_FIELDS = frozenset(["cpu", "gpu", "scratch"])
STR_TAG = "tag:yaml.org,2002:str"

NEW_FILE = """\
# nanoHPC cluster configuration: the whole cluster in one file.
# Written by `nanohpc init`. Everything not written here uses nanoHPC's defaults;
# examples/cluster.yml in nanoHPC shows every setting with a comment.

cluster:
  name: NAME                    # shown in the website, dashboards, and Slurm
  admins: []                    # users with sudo and SSH access to every machine
  website:
    hostname: HOSTNAME
    https: letsencrypt          # letsencrypt | own
    path: /cluster/             # where the site is on the hostname; "/" uses the whole hostname
    allow: []                   # networks that may open the site, e.g. [10.0.0.0/8]; empty: anyone

# Every machine of the cluster, by name: its address and roles (front, home, backup, compute).
machines: {}

# The people who use the cluster: Linux name, UID (the same on every machine), and SSH public keys.
users: []

# Slurm partitions (queues); compute machines list the partitions they are in.
partitions:
  main:
    default: true
    jobs: any                   # batch | interactive | any
    max_time: "24:00:00"
"""

# The sections new() adds from config.py's defaults, each with the comment written above it.
NEW_SECTIONS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("policy", POLICY_DEFAULTS, "Queue policy for every user: limits, default CPUs and memory, and job priority."),
    ("home", HOME_DEFAULTS, "Disk quota for each user's home directory."),
    ("scratch", SCRATCH_DEFAULTS, "Files in scratch older than this many days are deleted."),
    ("alerts", ALERTS_DEFAULTS, "true: alerts go to Slack (NANOHPC_SLACK_WEBHOOK in .env next to this file)."),
    ("auto_deploy", AUTO_DEPLOY_DEFAULTS, "Automatic deploys: the front node deploys this file's Git repository."),
)


class NullRepresenter(RoundTripRepresenter):
    """Write None as `null`, as the examples do, instead of an empty value."""

    def represent_none(self, data: Any) -> Any:
        """Represent None as the word null."""
        return self.represent_scalar("tag:yaml.org,2002:null", "null")


NullRepresenter.add_representer(type(None), NullRepresenter.represent_none)


def yaml_handler() -> YAML:
    """Return a round-trip YAML reader and writer set up to reproduce the examples byte for byte."""
    handler = YAML(typ="rt")
    handler.Representer = NullRepresenter
    handler.preserve_quotes = True
    handler.indent(mapping=2, sequence=4, offset=2)
    handler.width = 4096  # never fold long lines such as SSH keys
    return handler


def plain(value: Any) -> Any:
    """Return `value` as plain Python data: dicts, lists, str, int, float, bool, and None."""
    if isinstance(value, dict):
        return {plain(key): plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    for kind in (str, int, float):
        if isinstance(value, kind):
            return kind(value)
    raise TypeError(f"unexpected value in cluster.yml: {value!r}")


def scalar(value: Any, old: Any) -> Any:
    """Return a new scalar for the file. A string keeps the quote style of the value it replaces, and is
    quoted when the YAML reader nanoHPC deploys with would read it as something else (like 24:00:00)."""
    if not isinstance(value, str):
        return value
    if isinstance(old, ScalarString):
        return type(old)(value)
    if yaml.resolver.Resolver().resolve(yaml.ScalarNode, value, (True, False)) != STR_TAG:
        return DoubleQuotedScalarString(value)
    return value


def to_node(value: Any, flow: bool) -> Any:
    """Convert plain Python data to ruamel.yaml nodes. Lists of scalars are written on one line, lists of
    mappings as blocks; `flow` writes a mapping on one line."""
    if isinstance(value, dict):
        node = CommentedMap()
        for key, item in value.items():
            node[key] = to_node(item, flow)
        if flow:
            node.fa.set_flow_style()
        return node
    if isinstance(value, list):
        seq = CommentedSeq([to_node(item, False) for item in value])
        if not any(isinstance(item, dict) for item in value):
            seq.fa.set_flow_style()
        return seq
    return scalar(value, None)


def is_block(value: Any) -> bool:
    """Return whether `value` is a mapping or list written as a non-empty block (not on one line)."""
    return isinstance(value, (CommentedMap, CommentedSeq)) and len(value) > 0 and not value.fa.flow_style()


def end_holder(parent: CommentedBase, key: Any) -> tuple[CommentedBase, Any]:
    """Return the container and key whose comment holds the text that follows `parent[key]`."""
    value = parent[key]
    if is_block(value):
        last = list(value)[-1] if isinstance(value, CommentedMap) else len(value) - 1
        return end_holder(value, last)
    return parent, key


def is_empty(value: Any) -> bool:
    """Return whether `value` is an empty mapping or list (written as {} or [])."""
    return isinstance(value, (CommentedMap, CommentedSeq)) and len(value) == 0


def get_after(container: CommentedBase, key: Any) -> CommentToken | None:
    """Return the comment token after `container[key]` (its own comment and the lines that follow)."""
    entry = container.ca.items.get(key)
    return None if entry is None else entry[2 if isinstance(container, CommentedMap) else 0]


def set_after(container: CommentedBase, key: Any, token: CommentToken) -> None:
    """Set the comment token after `container[key]`."""
    entry = container.ca.items.setdefault(key, [None, None, None, None])
    entry[2 if isinstance(container, CommentedMap) else 0] = token


def take_rest(parent: CommentedBase, key: Any) -> str:
    """Remove and return the text after `parent[key]` from its first blank line on (the text that belongs
    to what follows, such as the blank line before the next section). The value's own comment, and comment
    lines right under it, stay. Lists also keep such text as their end comment, which is taken too."""
    ends = []
    node = parent[key]
    while is_block(node):
        if node.ca.end:
            ends.insert(0, "".join(token.value for token in node.ca.end))
            node.ca.end = []
        node = node[list(node)[-1] if isinstance(node, CommentedMap) else len(node) - 1]
    container, last = end_holder(parent, key)
    token = get_after(container, last)
    lines = token.value.splitlines(keepends=True) if token is not None else []
    own = lines[:1]
    for line in lines[1:]:
        if line.strip() == "":
            break
        own.append(line)
    if token is not None:
        token.value = "".join(own)
    return "".join(lines[len(own) :]) + "".join(ends)


def add_rest(parent: CommentedBase, key: Any, rest: str) -> None:
    """Put `rest` (from take_rest) back after `parent[key]`, after the value's own comment."""
    if not rest:
        return
    container, last = end_holder(parent, key)
    token = get_after(container, last)
    if token is None or token.value == "":
        set_after(container, last, CommentToken("\n" + rest, CommentMark(0), None))
    else:
        token.value += rest


@contextmanager
def keep_end(parent: CommentedBase, key: Any) -> Iterator[None]:
    """Keep the text that follows `parent[key]` after it while the block at `parent[key]` is changed."""
    rest = take_rest(parent, key)
    yield
    flow_if_empty(parent[key])
    add_rest(parent, key, rest)


def flow_if_empty(value: Any) -> None:
    """Write every empty mapping or list under `value` as {} or [] (ruamel.yaml writes broken YAML for an
    empty block)."""
    if is_empty(value):
        value.fa.set_flow_style()
    elif isinstance(value, CommentedMap):
        for item in value.values():
            flow_if_empty(item)
    elif isinstance(value, CommentedSeq):
        for item in value:
            flow_if_empty(item)


def blank_line_before(mapping: CommentedMap, key: Any) -> None:
    """Write a blank line before `key` in `mapping`, as between sections and machines in the examples."""
    entry = mapping.ca.items.setdefault(key, [None, None, None, None])
    entry[1] = [CommentToken("\n", CommentMark(0), None), *(entry[1] or [])]


def merge(parent: CommentedBase, key: Any, value: Any, flow: bool) -> None:
    """Set `parent[key]` to `value`. A mapping is updated in place: its keys keep their order and comments,
    keys not in `value` are removed, and new keys go at its end. An unchanged value is left as written."""
    old = parent[key]
    if old == value:
        return
    if isinstance(old, CommentedMap) and isinstance(value, dict):
        with keep_end(parent, key):
            update_mapping(old, value, frozenset(), flow)
        return
    if isinstance(value, (dict, list)):
        node = to_node(value, flow)
        if is_block(old) and isinstance(node, CommentedSeq):
            node.fa.set_block_style()  # a list written as a block stays a block
        parent[key] = node
        return
    parent[key] = scalar(value, old)


def update_mapping(mapping: CommentedMap, values: dict[str, Any], flow_fields: frozenset[str], flow: bool) -> None:
    """Make `mapping` hold exactly `values`: keys not in `values` are removed, existing keys are merged in
    place, and new keys go at the end. New mappings under `flow_fields` (or all, with `flow`) are written
    on one line."""
    for name in [name for name in mapping if name not in values]:
        del mapping[name]
    for name, item in values.items():
        child_flow = flow or name in flow_fields
        if name in mapping:
            merge(mapping, name, item, child_flow)
        else:
            mapping[name] = to_node(item, child_flow)


def remove_item(container: CommentedBase, key: Any) -> None:
    """Remove `container[key]` with the text written before it. For the last item, that text is the
    blank line or comment after the item before it, which is dropped too."""
    keys = list(container) if isinstance(container, CommentedMap) else list(range(len(container)))
    position = keys.index(key)
    if position == len(keys) - 1 and position > 0:
        take_rest(container, keys[position - 1])
    del container[key]


class ClusterFile:
    """One cluster.yml, kept as ruamel.yaml round-trip data so that edits keep comments and order."""

    def __init__(self, data: CommentedMap) -> None:
        self.data = data

    def as_text(self) -> str:
        """Return the YAML text exactly as save() writes it."""
        stream = io.StringIO()
        yaml_handler().dump(self.data, stream)
        return stream.getvalue()

    def save(self, path: Path) -> None:
        """Write the file to `path` (mode 0644): first to a temporary file next to it, then renamed over it,
        so a crash never leaves half a file."""
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(self.as_text())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)

    def checked(self) -> tuple[dict[str, Any], list[str]]:
        """Return the configuration config.check_config makes of the current content, read back the way
        `nanohpc deploy` reads the file, and its errors (duplicate keys included)."""
        loader = UniqueKeyLoader(self.as_text())
        raw = loader.get_single_data()
        loader.dispose()
        config, errors = check_config(raw)
        return config, loader.duplicates + errors

    def validate(self) -> list[str]:
        """Return the errors config.check_config finds in the current content (duplicate keys included)."""
        return self.checked()[1]

    def validate_in(self, folder: Path) -> list[str]:
        """Return the errors `nanohpc validate` reports for the current content saved as a cluster.yml in
        `folder`: those of validate(), then (when there are none) missing or wrong website files (certificate,
        key, logo, relative to `folder`) and missing or malformed secrets in `folder`/.env."""
        config, errors = self.checked()
        if errors:
            return errors
        return website_files(config["cluster"]["website"], folder) + secrets(config, folder)

    def get(self, path: Sequence[str | int]) -> Any:
        """Return the value at `path` (keys, or indexes in lists) as plain Python data; None when it or one
        of its parents is missing or null."""
        value: Any = self.data
        for part in path:
            if (isinstance(value, dict) and part in value) or (
                isinstance(value, list) and isinstance(part, int) and -len(value) <= part < len(value)
            ):
                value = value[part]
            else:
                return None
        return plain(value)

    def set_value(self, path: list[str], value: Any) -> None:
        """Set the setting at `path`, for example ["cluster", "website", "hostname"]. Missing sections and
        keys are created (a new section goes in the usual section order); everything else is kept."""
        if not path:
            raise ValueError("set_value needs a path")
        section = path[0]
        if section not in self.data:
            self.add_section(section)
        if len(path) == 1:
            with keep_end(self.data, section):
                merge(self.data, section, value, False)
            return
        with keep_end(self.data, section):
            parent = self.data
            for part in path[:-1]:
                if parent.get(part) is None:
                    parent[part] = CommentedMap()
                if not isinstance(parent[part], CommentedMap):
                    raise TypeError(f"{'.'.join(path)}: {part} is not a mapping")
                parent = parent[part]
            if path[-1] in parent:
                merge(parent, path[-1], value, False)
            else:
                if len(parent) == 0:
                    parent.fa.set_block_style()  # an empty {} becomes a block
                parent[path[-1]] = to_node(value, False)

    def add_section(self, section: str) -> None:
        """Add an empty top-level section in the order of config.TOP_FIELDS, after a blank line."""
        if section not in TOP_FIELDS:
            raise ValueError(f"{section} is not a cluster.yml section")
        order = TOP_FIELDS.index(section)
        keys = list(self.data)
        position = len([key for key in keys if key in TOP_FIELDS and TOP_FIELDS.index(key) < order])
        if position == 0:
            self.data.insert(0, section, None)
            return
        with keep_end(self.data, keys[position - 1]):
            self.data.insert(position, section, None)
        blank_line_before(self.data, section)

    def block(self, section: str, empty: CommentedBase) -> Any:
        """Return the top-level `section` as a block mapping or list, created empty if missing."""
        if self.data.get(section) is None:
            if section not in self.data:
                self.add_section(section)
            self.data[section] = empty
        return self.data[section]

    def machines(self) -> dict[str, dict[str, Any]]:
        """Return the machines as plain Python data, by name."""
        return self.get(["machines"]) or {}

    def users(self) -> list[dict[str, Any]]:
        """Return the users as plain Python data."""
        return self.get(["users"]) or []

    def partitions(self) -> dict[str, dict[str, Any]]:
        """Return the partitions as plain Python data, by name."""
        return self.get(["partitions"]) or {}

    def set_entry(
        self, section: str, name: str, values: dict[str, Any], flow_fields: frozenset[str], separate: bool
    ) -> None:
        """Add or replace one named entry of a mapping section (a machine or a partition). A new entry goes
        at the end, after a blank line when `separate`; an existing one keeps its comments where possible.
        New mappings under `flow_fields` are written on one line."""
        entries = self.block(section, CommentedMap())
        with keep_end(self.data, section):
            if name in entries:
                with keep_end(entries, name):
                    update_mapping(entries[name], values, flow_fields, False)
                return
            had_entries = len(entries) > 0
            entries.fa.set_block_style()
            entries[name] = CommentedMap()
            update_mapping(entries[name], values, flow_fields, False)
            if separate and had_entries:
                blank_line_before(entries, name)

    def remove_entry(self, section: str, name: str) -> None:
        """Remove one named entry of a mapping section; KeyError if it is not there."""
        entries = self.data.get(section)
        if not isinstance(entries, CommentedMap) or name not in entries:
            raise KeyError(f"{section}.{name} is not in the file")
        with keep_end(self.data, section):
            remove_item(entries, name)

    def set_machine(self, name: str, values: dict[str, Any]) -> None:
        """Add or replace the machine `name` with `values` (its fields, as in cluster.yml)."""
        self.set_entry("machines", name, values, FLOW_MACHINE_FIELDS, True)

    def remove_machine(self, name: str) -> None:
        """Remove the machine `name`."""
        self.remove_entry("machines", name)

    def set_partition(self, name: str, values: dict[str, Any]) -> None:
        """Add or replace the partition `name` with `values` (default, jobs, max_time, max_gpus_per_user)."""
        self.set_entry("partitions", name, values, frozenset(), False)

    def remove_partition(self, name: str) -> None:
        """Remove the partition `name` (compute machines that list it are left as they are)."""
        self.remove_entry("partitions", name)

    def set_user(self, name: str, uid: int, ssh_keys: list[str], admin: bool) -> None:
        """Add or replace the user `name`, and add it to or remove it from cluster.admins."""
        users = self.block("users", CommentedSeq())
        values = {"name": name, "uid": uid, "ssh_keys": [DoubleQuotedScalarString(key) for key in ssh_keys]}
        with keep_end(self.data, "users"):
            index = self.user_index(name)
            if index is None:
                users.fa.set_block_style()
                users.append(to_node(values, False))
            else:
                with keep_end(users, index):
                    update_mapping(users[index], values, frozenset(), False)
        admins = self.get(["cluster", "admins"]) or []
        if admin and name not in admins:
            self.set_value(["cluster", "admins"], [*admins, name])
        if not admin and name in admins:
            self.set_value(["cluster", "admins"], [item for item in admins if item != name])

    def remove_user(self, name: str) -> None:
        """Remove the user `name`, also from cluster.admins; KeyError if it is not there."""
        index = self.user_index(name)
        if index is None:
            raise KeyError(f"user {name} is not in the file")
        with keep_end(self.data, "users"):
            remove_item(self.data["users"], index)
        admins = self.get(["cluster", "admins"]) or []
        if name in admins:
            self.set_value(["cluster", "admins"], [item for item in admins if item != name])

    def user_index(self, name: str) -> int | None:
        """Return the position of the user `name` in users, or None if there is no such user."""
        users = self.data.get("users")
        if not isinstance(users, CommentedSeq):
            return None
        for index, user in enumerate(users):
            if isinstance(user, dict) and user.get("name") == name:
                return index
        return None


def load(path: Path) -> ClusterFile:
    """Read a cluster.yml for editing; ValueError if it is not a YAML mapping."""
    data = yaml_handler().load(path.read_text())
    if not isinstance(data, CommentedMap):
        raise TypeError(f"{path}: the file must be a YAML mapping with the sections of cluster.yml")
    return ClusterFile(data)


def new(name: str) -> ClusterFile:
    """Return a new cluster.yml with simple defaults and short comments, for the cluster `name`. It needs
    machines and users before it is valid."""
    data = yaml_handler().load(NEW_FILE)
    file = ClusterFile(data)
    file.set_value(["cluster", "name"], name)
    file.set_value(["cluster", "website", "hostname"], f"{name}.example.org")
    for section, defaults, comment in NEW_SECTIONS:
        file.set_value([section], dict(defaults))
        data.ca.items[section][1] = [
            CommentToken("\n", CommentMark(0), None),
            CommentToken(f"# {comment}\n", CommentMark(0), None),
        ]
    return file
