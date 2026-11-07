"""The Slurm task prolog: an interactive shell says it is on the compute node; batch jobs and later steps print nothing."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from nanohpc.config import check_config
from nanohpc.render import render

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/nanohpc/files/nanohpc-task-prolog"
CONFIG_TASKS = ROOT / "src/nanohpc/ansible/roles/slurm_compute/tasks/slurm_config.yml"
JOB = (
    "JobId=3811 JobName=bash UserId=alice(2000) Priority=1 Partition=interactive BatchFlag={batch} "
    "Restarts=0 StartTime=2026-11-05T16:11:43 EndTime=2026-11-05T17:11:43 Deadline=N/A"
)
GREETING = (
    "print You are on the compute node gpu4, in job 3811, with {gpus} until {end}.\n"
    "print Run your code here (no need for srun again). Type exit to release the GPU.\n"
)


class TaskPrologTest(unittest.TestCase):
    """Run the prolog as Slurm does, with a stand-in scontrol that gives the job, or fails."""

    def run_prolog(self, variables: dict[str, str], scontrol: str) -> str:
        with tempfile.TemporaryDirectory(prefix="task-prolog-") as directory:
            stand_in = Path(directory) / "scontrol"
            stand_in.write_text(f"#!/bin/sh\n{scontrol}\n")
            stand_in.chmod(0o755)
            environment = dict(
                os.environ,
                SLURM_JOB_ID="3811",
                SLURMD_NODENAME="gpu4",
                PATH=f"{directory}{os.pathsep}{os.environ['PATH']}",
                **variables,
            )
            result = subprocess.run(["sh", str(SCRIPT)], env=environment, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_only_the_interactive_shell_gets_the_two_lines(self) -> None:
        shell = {"SLURM_STEP_ID": "0", "SLURM_PTY_PORT": "40405"}
        job = f"echo '{JOB.format(batch=0)}'"
        never = "echo called >&2; exit 1"  # scontrol must not be asked outside the shell
        cases = [
            ("shell, 1 GPU", {**shell, "SLURM_GPUS_ON_NODE": "1"}, job, GREETING.format(gpus="1 GPU", end="17:11")),
            ("shell, 2 GPUs", {**shell, "SLURM_GPUS_ON_NODE": "2"}, job, GREETING.format(gpus="2 GPUs", end="17:11")),
            ("shell, no GPU", shell, job, GREETING.format(gpus="no GPU", end="17:11")),
            (
                "shell, scontrol fails",
                {**shell, "SLURM_GPUS_ON_NODE": "1"},
                "exit 1",
                GREETING.format(gpus="1 GPU", end="an end time Slurm could not give"),
            ),
            ("second step in the shell", {**shell, "SLURM_STEP_ID": "1", "SLURM_GPUS_ON_NODE": "1"}, never, ""),
            ("batch script", {"SLURM_STEP_ID": "4294967291", "SLURM_GPUS_ON_NODE": "1"}, never, ""),
            ("srun in a batch job or salloc", {"SLURM_STEP_ID": "0", "SLURM_GPUS_ON_NODE": "1"}, never, ""),
        ]
        for name, variables, scontrol, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.run_prolog(variables, scontrol), expected)


class TaskPrologDeployTest(unittest.TestCase):
    def test_slurm_conf_names_the_prolog(self) -> None:
        raw = yaml.safe_load((ROOT / "examples/cluster.yml").read_text())
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        conf = render(config, {name: name for name in config["machines"]}, False).slurm_conf
        self.assertIn("TaskProlog=/etc/slurm/nanohpc-task-prolog\n", conf)

    def test_prolog_is_installed_before_the_configuration_that_names_it(self) -> None:
        """A slurm.conf naming a missing prolog would make every job on that machine fail."""
        tasks = yaml.safe_load(CONFIG_TASKS.read_text())
        names = [task["name"] for task in tasks]
        prolog = next(index for index, task in enumerate(tasks) if "nanohpc-task-prolog" in str(task))
        self.assertLess(prolog, names.index("Install slurm.conf and cgroup.conf"))
        self.assertEqual(tasks[prolog]["ansible.builtin.copy"]["dest"], "/etc/slurm/nanohpc-task-prolog")
        self.assertEqual(tasks[prolog]["ansible.builtin.copy"]["mode"], "0755")


if __name__ == "__main__":
    unittest.main()
