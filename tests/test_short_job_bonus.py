"""The short-job bonus and the waiting-time weight: defaults, checks, job_submit.lua, and the website text."""

import copy
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import check_config
from nanohpc.deploy import site_data
from nanohpc.render import render

ROOT = Path(__file__).resolve().parents[1]


def config_of(change: Callable[[dict[str, Any]], None] | None) -> dict[str, Any]:
    """Return the validated example cluster (main: 24 hours, default; interactive: 8 hours), changed first."""
    raw = copy.deepcopy(yaml.safe_load((ROOT / "examples/cluster.yml").read_text()))
    if change is not None:
        change(raw)
    config, errors = check_config(raw)
    assert errors == [], errors
    return config


def errors_of(change: Callable[[dict[str, Any]], None]) -> list[str]:
    """Return the configuration errors after a change to the example cluster."""
    raw = copy.deepcopy(yaml.safe_load((ROOT / "examples/cluster.yml").read_text()))
    change(raw)
    return check_config(raw)[1]


def set_policy(values: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Return a change that sets these policy values in the raw cluster.yml."""
    return lambda raw: raw["policy"].update(values)


def site_factor(config: dict[str, Any], request: str) -> str:
    """Submit one job to the rendered job_submit.lua with a small Lua stand-in; return its site_factor."""
    rendered = render(config, {name: name for name in config["machines"]}, False).job_submit_lua
    hook, _ = rendered.rsplit("return slurm.SUCCESS", 1)
    prelude = "slurm = { SUCCESS = 0, ERROR = 1, NO_VAL = 4294967294, jobs = {}, log_user = function() end }\n"
    program = (
        prelude + hook + f"local job = {request}\nlocal code = slurm_job_submit(job, {{}}, 1000)\n"
        'io.write(code .. "|" .. tostring(job.site_factor))\n'
    )
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "test.lua"
        script.write_text(program)
        result = subprocess.run(["lua", str(script)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    code, factor = result.stdout.split("|")
    assert code == "0", result.stdout
    return factor


class ShortJobBonusTest(unittest.TestCase):
    """policy.short_job_bonus falls in a straight line to 0 at the default partition's max_time."""

    def test_defaults_and_slurm_conf(self) -> None:
        config = config_of(
            lambda raw: [raw["policy"].pop(key) for key in ("age_weight", "short_job_bonus") if key in raw["policy"]]
        )
        self.assertEqual((config["policy"]["age_weight"], config["policy"]["short_job_bonus"]), (0, 5000))
        self.assertIn(
            "PriorityWeightAge=0\n", render(config, {name: name for name in config["machines"]}, False).slurm_conf
        )

    def test_zero_is_allowed_and_negative_is_refused(self) -> None:
        self.assertEqual(errors_of(lambda raw: raw["policy"].update(age_weight=0, short_job_bonus=0)), [])
        for key, value in (
            ("short_job_bonus", -1),
            ("short_job_bonus", "5000"),
            ("age_weight", -1),
            ("age_weight", True),
        ):
            with self.subTest(key=key, value=value):
                errors = errors_of(set_policy({key: value}))
                self.assertIn(f"policy.{key} must be 0 or a positive integer", errors)

    def test_bonus_falls_in_a_straight_line(self) -> None:
        """5000 x (1 - requested minutes / 1440), rounded; no --time means the partition's maximum."""
        config = config_of(None)
        main = 'user_id=1000, partition="main", script="#!/bin/bash"'
        for minutes, bonus in ((60, "4792"), (240, "4167"), (720, "2500"), (1440, "0")):
            with self.subTest(minutes=minutes):
                self.assertEqual(site_factor(config, f"{{{main}, time_limit={minutes}}}"), bonus)
        self.assertEqual(site_factor(config, f"{{{main}, time_limit=slurm.NO_VAL}}"), "0")
        self.assertEqual(site_factor(config, '{user_id=1000, script="#!/bin/bash", time_limit=60}'), "4792")
        shell = 'user_id=1000, partition="interactive", argc=2, argv={[0]="bash", [1]="-l"}'
        self.assertEqual(site_factor(config, f"{{{shell}, time_limit=60}}"), "4792")
        # The interactive partition's own maximum (8 hours) counts against the default partition's 24 hours.
        self.assertEqual(site_factor(config, f"{{{shell}, time_limit=slurm.NO_VAL}}"), "3333")

    def test_bonus_follows_the_settings(self) -> None:
        config = config_of(lambda raw: raw["policy"].update(short_job_bonus=0))
        self.assertEqual(site_factor(config, '{user_id=1000, script="#!/bin/bash", time_limit=60}'), "0")
        config = config_of(lambda raw: raw["partitions"]["main"].update(max_time="12:00:00"))
        self.assertEqual(site_factor(config, '{user_id=1000, script="#!/bin/bash", time_limit=360}'), "2500")

    def test_website_text_follows_the_settings(self) -> None:
        site = site_data(config_of(None))
        self.assertEqual(
            site["priority_rule"],
            "Jobs with a shorter --time get a bonus of up to 5000 points that falls in a straight line to 0 at the "
            "main partition's time limit (24:00:00); it shows in the SITE column of sprio. Waiting time does not "
            "add priority.",
        )
        self.assertTrue(site["resubmit_rule"].startswith("No. Priority comes from the GPU-hours you used"))
        self.assertIn("and the --time you ask for", site["resubmit_rule"])
        site = site_data(config_of(lambda raw: raw["policy"].update(age_weight=1000, short_job_bonus=0)))
        self.assertEqual(
            site["priority_rule"], "Waiting time also adds priority, up to 1000 points after 7-00:00:00 of waiting."
        )
        self.assertTrue(site["resubmit_rule"].startswith("Yes:"))
        self.assertIn("loses the waiting-time priority", site["resubmit_rule"])
        self.assertNotIn("--time", site["resubmit_rule"])


if __name__ == "__main__":
    unittest.main()
