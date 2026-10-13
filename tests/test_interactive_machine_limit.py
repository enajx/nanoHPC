"""Long batch jobs avoid machines reserved for interactive shells."""

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from nanohpc.config import check_config
from nanohpc.render import render

ROOT = Path(__file__).resolve().parents[1]


def config_of() -> dict:
    """Return the validated example cluster."""
    raw = yaml.safe_load((ROOT / "examples/cluster.yml").read_text())
    config, errors = check_config(raw)
    assert errors == [], errors
    return config


def run_lua(request: str, record: str | None, uid: int) -> tuple[int, str, str]:
    """Call the rendered Slurm entry point with a small Lua stand-in."""
    config = config_of()
    rendered = render(config, {name: name for name in config["machines"]}, False).job_submit_lua
    hook, _ = rendered.rsplit("return slurm.SUCCESS", 1)
    prelude = """
local message = ""
slurm = {
    SUCCESS = 0, ERROR = 1, NO_VAL = 4294967294, jobs = {},
    log_user = function(format, ...) message = string.format(format, ...) end,
}
"""
    call = (
        f"local job = {request}\nlocal code = slurm_job_submit(job, {{}}, {uid})\n"
        if record is None
        else f"local job = {request}\nlocal code = slurm_job_modify(job, {record}, {{}}, {uid})\n"
    )
    program = prelude + hook + call + 'io.write(code .. "|" .. message .. "|" .. tostring(job.exc_nodes))\n'
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "test.lua"
        script.write_text(program)
        result = subprocess.run(["lua", str(script)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    code, message, excluded = result.stdout.split("|", 2)
    return int(code), message, excluded


class InteractiveMachineLimitTests(unittest.TestCase):
    """Check config validation and the actual Lua hook behavior."""

    def test_config_requires_shared_interactive_machines_and_batch_jobs(self) -> None:
        raw = yaml.safe_load((ROOT / "examples/cluster.yml").read_text())
        self.assertEqual(config_of()["partitions"]["main"]["max_time_on_interactive_machines"], "04:00:00")
        bad = copy.deepcopy(raw)
        bad["machines"]["gpu4"]["partitions"] = ["main"]
        self.assertTrue(
            any("partitions.main.max_time_on_interactive_machines" in error for error in check_config(bad)[1])
        )
        bad = copy.deepcopy(raw)
        bad["partitions"]["main"]["jobs"] = "interactive"
        self.assertTrue(
            any("partitions.main.max_time_on_interactive_machines" in error for error in check_config(bad)[1])
        )
        bad = copy.deepcopy(raw)
        bad["machines"]["gpu2"]["partitions"].append("interactive")
        bad["machines"]["cpu1"]["partitions"].append("interactive")
        self.assertTrue(
            any("partitions.main.max_time_on_interactive_machines" in error for error in check_config(bad)[1])
        )
        bad["partitions"]["main"]["max_time"] = "04:00:00"
        self.assertFalse(
            any("partitions.main.max_time_on_interactive_machines" in error for error in check_config(bad)[1])
        )

    def test_long_main_job_excludes_shared_interactive_machine(self) -> None:
        base = 'user_id=1000, partition="main", script="#!/bin/bash"'
        self.assertEqual(run_lua(f"{{{base}, time_limit=241}}", None, 1000), (0, "", "gpu4"))
        self.assertEqual(run_lua(f'{{{base}, time_limit=241, exc_nodes="cpu1"}}', None, 1000), (0, "", "cpu1,gpu4"))
        self.assertEqual(run_lua(f'{{{base}, time_limit=241, exc_nodes="gpu4"}}', None, 1000), (0, "", "gpu4"))
        self.assertEqual(
            run_lua(f'{{{base}, time_limit=241, exc_nodes="gpu[3-4]"}}', None, 1000),
            (0, "", "gpu[3-4]"),
        )
        self.assertEqual(run_lua(f"{{{base}, time_limit=240}}", None, 1000), (0, "", "nil"))
        self.assertEqual(run_lua(f"{{{base}, time_limit=slurm.NO_VAL}}", None, 1000), (0, "", "gpu4"))
        self.assertEqual(
            run_lua(
                '{user_id=1000, partition="interactive", argc=2, argv={[0]="bash", [1]="-l"}, time_limit=60}',
                None,
                1000,
            ),
            (0, "", "nil"),
        )

    def test_explicit_interactive_machine_is_refused_for_long_job(self) -> None:
        code, message, _ = run_lua(
            '{user_id=1000, partition="main", script="x", time_limit=241, req_nodes="gpu4"}', None, 1000
        )
        self.assertEqual(code, 1)
        self.assertIn("gpu4", message)
        code, message, _ = run_lua(
            '{user_id=1000, partition="main", script="x", time_limit=241, req_nodes="gpu[2,4]"}', None, 1000
        )
        self.assertEqual(code, 1)
        self.assertIn("gpu4", message)
        self.assertIn("4 hours", message)
        code, message, _ = run_lua(
            '{user_id=1000, partition="main", script="x", time_limit=241, req_nodes="gpu[3-4]"}', None, 1000
        )
        self.assertEqual(code, 1)
        self.assertIn("gpu4", message)

    def test_user_cannot_remove_exclusion_or_raise_time_to_bypass_it(self) -> None:
        record = '{partition="main", time_limit=241}'
        self.assertEqual(run_lua('{exc_nodes=""}', record, 1000), (0, "", "gpu4"))
        code, message, _ = run_lua('{req_nodes="gpu4"}', record, 1000)
        self.assertEqual(code, 1)
        self.assertIn("gpu4", message)
        self.assertEqual(run_lua("{time_limit=241}", '{partition="main", time_limit=120}', 1000), (0, "", "gpu4"))
        self.assertEqual(
            run_lua("{time_limit=241}", '{partition="main", time_limit=120, exc_nodes="cpu1"}', 1000),
            (0, "", "cpu1,gpu4"),
        )
        code, message, _ = run_lua("{time_limit=241}", '{partition="main", time_limit=120, req_nodes="gpu4"}', 1000)
        self.assertEqual(code, 1)
        self.assertIn("gpu4", message)
        self.assertEqual(run_lua('{exc_nodes=""}', record, 0), (0, "", ""))

    def test_multi_partition_update_keeps_exclusion(self) -> None:
        record = '{partition="main,other", time_limit=241, exc_nodes="cpu1"}'
        self.assertEqual(run_lua('{exc_nodes=""}', record, 1000), (0, "", "gpu4"))
        self.assertEqual(run_lua("{time_limit=241}", record, 1000), (0, "", "cpu1,gpu4"))
        code, message, _ = run_lua('{req_nodes="gpu[3-4]"}', record, 1000)
        self.assertEqual(code, 1)
        self.assertIn("gpu4", message)


if __name__ == "__main__":
    unittest.main()
