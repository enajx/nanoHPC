"""Ansible callback that records, per machine, the tasks that changed (or would change, in check mode) and
the task that failed. `nanohpc deploy` reads the record to summarize its dry run. It writes JSON to the path in
the NANOHPC_RECORD environment variable when the playbook ends:
{"changed": {machine: [task names]}, "failed": {machine: "task name: message"}}.
"""

import json
import os
from typing import Any

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
name: nanohpc_record
type: aggregate
short_description: record changed and failed tasks per machine for nanohpc deploy
description:
  - Writes the tasks that changed and the task that failed on each machine to the file in NANOHPC_RECORD.
"""


def failure_message(result: dict[str, Any]) -> str:
    """Return what a failed task says: its message, the failed items' messages for a loop, and the last line
    of its error output."""
    messages = [str(result["msg"])] if result.get("msg") else []
    for item in result.get("results", []):
        if isinstance(item, dict) and item.get("failed") and item.get("msg"):
            messages.append(str(item["msg"]))
    stderr = str(result.get("stderr", "")).strip()
    if stderr:
        messages.append(stderr.splitlines()[-1])
    return " ".join(" ".join(messages).split()) or "failed with no message"


class CallbackModule(CallbackBase):
    """Collect changed and failed tasks per machine; write them out when the playbook ends."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "nanohpc_record"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        super().__init__()
        self.changed: dict[str, list[str]] = {}
        self.failed: dict[str, str] = {}

    def v2_runner_on_ok(self, result: Any) -> None:
        if result._result.get("changed"):
            names = self.changed.setdefault(result._host.get_name(), [])
            name = result._task.get_name()
            if name not in names:
                names.append(name)

    def v2_runner_on_failed(self, result: Any, ignore_errors: bool = False) -> None:
        if not ignore_errors:
            self.failed[result._host.get_name()] = f"{result._task.get_name()}: {failure_message(result._result)}"

    def v2_runner_on_unreachable(self, result: Any) -> None:
        self.failed[result._host.get_name()] = f"{result._task.get_name()}: {failure_message(result._result)}"

    def v2_playbook_on_stats(self, stats: Any) -> None:
        path = os.environ.get("NANOHPC_RECORD")
        if not path:
            return
        # A failure that a rescue block handled does not count: only machines that ended failed or unreachable.
        failed = {
            host: text
            for host, text in self.failed.items()
            if stats.failures.get(host, 0) > 0 or stats.dark.get(host, 0) > 0
        }
        with open(path, "w", encoding="utf-8") as record:
            json.dump({"changed": self.changed, "failed": failed}, record)
