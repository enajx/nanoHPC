"""Tests for the files nanoHPC generates from cluster.yml (Slurm configuration, hosts, job submit rules)."""

import copy
import unittest
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import check_config
from nanohpc.render import render

ROOT = Path(__file__).resolve().parents[1]


def names(config: dict[str, Any]) -> dict[str, str]:
    """Return hostnames equal to the machine names, as on a cluster whose machines are named like cluster.yml."""
    return {name: name for name in config["machines"]}


def config_of(change: Any = None) -> dict[str, Any]:
    """Return the validated example config, optionally changed first."""
    raw = copy.deepcopy(yaml.safe_load((ROOT / "examples" / "cluster.yml").read_text()))
    if change is not None:
        change(raw)
    config, errors = check_config(raw)
    assert errors == [], errors
    return config


class SlurmConfTest(unittest.TestCase):
    """slurm.conf follows the machines, partitions, and policy in cluster.yml."""

    def test_nodes_and_partitions(self) -> None:
        conf = render(config_of(), names(config_of()), False).slurm_conf
        self.assertIn("ClusterName=labcluster\n", conf)
        self.assertIn("SlurmctldHost=front(192.168.104.10)\n", conf)
        self.assertIn(
            "NodeName=gpu4 NodeHostname=gpu4 NodeAddr=192.168.104.11 CPUs=8 Boards=1 SocketsPerBoard=1 CoresPerSocket=4 "
            "ThreadsPerCore=2 RealMemory=4096 Gres=gpu:a6000:4 State=UNKNOWN\n",
            conf,
        )
        # CPU-only node: no Gres.
        self.assertIn(
            "NodeName=cpu1 NodeHostname=cpu1 NodeAddr=192.168.104.13 CPUs=4 Boards=1 SocketsPerBoard=1 CoresPerSocket=4 ThreadsPerCore=1 RealMemory=4096 State=UNKNOWN\n",
            conf,
        )
        # The storage and front machines are not Slurm nodes.
        self.assertNotIn("NodeName=store", conf)
        self.assertNotIn("NodeName=front", conf)
        self.assertIn("PartitionName=main Nodes=gpu4,gpu2,cpu1 Default=YES MaxTime=24:00:00 ", conf)
        self.assertIn("PartitionName=interactive Nodes=gpu4,gpu4i Default=NO MaxTime=08:00:00 ", conf)
        self.assertIn(" QoS=interactive ", conf)
        self.assertIn(" DefMemPerCPU=1024 DefCpuPerGPU=4 ", conf)

    def test_real_hostnames_are_used(self) -> None:
        config = config_of()
        hostnames = {name: f"lab-{name}" for name in config["machines"]}
        conf = render(config, hostnames, False).slurm_conf
        self.assertIn("SlurmctldHost=lab-front(192.168.104.10)\n", conf)
        self.assertIn("NodeName=gpu4 NodeHostname=lab-gpu4 NodeAddr=192.168.104.11 ", conf)

    def test_fair_share_policy(self) -> None:
        conf = render(config_of(), names(config_of()), False).slurm_conf
        for line in ("PriorityType=priority/multifactor", "PriorityWeightFairshare=10000", "PriorityWeightAge=1000",
                     "PriorityDecayHalfLife=7-00:00:00", "PriorityMaxAge=7-00:00:00", "JobSubmitPlugins=lua"):  # fmt: skip
            self.assertIn(line + "\n", conf)
        self.assertIn("TRESBillingWeights=CPU=0,Mem=0,GRES/gpu=1", conf)

    def test_simulated_cluster_accepts_smaller_vms(self) -> None:
        self.assertNotIn("config_overrides", render(config_of(), names(config_of()), False).slurm_conf)
        self.assertIn("SlurmdParameters=config_overrides\n", render(config_of(), names(config_of()), True).slurm_conf)


class GresConfTest(unittest.TestCase):
    """GPUs by device file: real ones from the driver, fake ones from placeholder files on simulated machines."""

    def test_real_and_fake_gpus(self) -> None:
        files = render(config_of(), names(config_of()), True)
        self.assertEqual(files.gres_conf["gpu4"], "Name=gpu Type=a6000 File=/dev/nvidia[0-3]\n")
        # Slurm ignores GPUs without device files, so fake GPUs get placeholder device files too.
        self.assertEqual(files.gres_conf["gpu2"], "Name=gpu Type=rtx6000ada File=/dev/nvidia[0-1]\n")
        self.assertEqual(files.gres_conf["cpu1"], "")

    def test_single_gpu_device(self) -> None:
        config = config_of(lambda raw: raw["machines"]["gpu2"]["gpu"].update(count=1))
        self.assertEqual(
            render(config, names(config), False).gres_conf["gpu2"], "Name=gpu Type=rtx6000ada File=/dev/nvidia0\n"
        )


class QosTest(unittest.TestCase):
    """One QoS per partition carries its time and GPU limits."""

    def test_partition_limits(self) -> None:
        qos = {item["name"]: item for item in render(config_of(), names(config_of()), False).qos}
        # max_wall is written the way sacctmgr prints it, so deploy can tell whether it changed.
        self.assertEqual(
            qos["main"], {"name": "main", "max_wall": "1-00:00:00", "max_gpus_per_user": None, "max_gpus_per_job": 6}
        )
        self.assertEqual(
            qos["interactive"],
            {"name": "interactive", "max_wall": "08:00:00", "max_gpus_per_user": 2, "max_gpus_per_job": 2},
        )

    def test_wall_times_as_slurm_prints_them(self) -> None:
        cases = [
            ("24:00:00", "1-00:00:00"),
            ("36:30:00", "1-12:30:00"),
            ("7-00:00:00", "7-00:00:00"),
            ("00:30:00", "00:30:00"),
        ]
        for given, printed in cases:
            config = config_of(lambda raw, value=given: raw["partitions"]["main"].update(max_time=value))
            qos = {item["name"]: item for item in render(config, names(config), False).qos}
            self.assertEqual(qos["main"]["max_wall"], printed, given)

    def test_cpu_only_partition_refuses_gpus(self) -> None:
        def cpu_partition(raw: dict[str, Any]) -> None:
            raw["partitions"]["cpu"] = {"max_time": "12:00:00"}
            raw["machines"]["cpu1"]["partitions"] = ["cpu"]

        qos = {
            item["name"]: item for item in render(config_of(cpu_partition), names(config_of(cpu_partition)), False).qos
        }
        self.assertEqual(qos["cpu"]["max_gpus_per_job"], 0)


class JobSubmitTest(unittest.TestCase):
    """job_submit.lua explains refusals, with the job type, time, and GPU limits of each partition."""

    def test_partition_tables(self) -> None:
        lua = render(config_of(), names(config_of()), False).job_submit_lua
        self.assertIn('local job_types = { ["main"] = "batch", ["interactive"] = "interactive" }', lua)
        self.assertIn('local walls = { ["main"] = 1440, ["interactive"] = 480 }', lua)
        self.assertIn('local gpu_limits = { ["main"] = 6, ["interactive"] = 2 }', lua)
        self.assertIn('local default_partition = "main"', lua)
        self.assertIn("local submit_limit = 30", lua)

    def test_days_in_time_limits(self) -> None:
        config = config_of(lambda raw: raw["partitions"]["main"].update(max_time="2-12:30:00"))
        self.assertIn('["main"] = 3630', render(config, names(config), False).job_submit_lua)

    def test_shared_gpu_limit_caps_partitions(self) -> None:
        config = config_of(lambda raw: raw["policy"].update(max_gpus_per_user=1))
        self.assertIn(
            'local gpu_limits = { ["main"] = 1, ["interactive"] = 1 }',
            render(config, names(config), False).job_submit_lua,
        )


class HostsTest(unittest.TestCase):
    """Every machine resolves every other machine by name and alias."""

    def test_hosts_block(self) -> None:
        hosts = render(config_of(), names(config_of()), False).hosts
        self.assertIn("192.168.104.10 front cluster.example.org\n", hosts)
        self.assertIn("192.168.104.20 store\n", hosts)


if __name__ == "__main__":
    unittest.main()


class HomeExportsTest(unittest.TestCase):
    """The home machine exports /home to every other machine that uses it."""

    def test_home_on_front(self) -> None:
        exports = render(config_of(), names(config_of()), False).home_exports
        # The front node serves /home; the compute machines mount it; the backup machine does not.
        lines = exports.splitlines()
        self.assertEqual(
            lines,
            [f"/home 192.168.104.{n}(rw,sync,no_root_squash,no_subtree_check)" for n in (11, 12, 13, 14)],
        )

    def test_home_on_storage_machine(self) -> None:
        def move_home(raw: dict[str, Any]) -> None:
            raw["machines"]["front"]["roles"] = ["front"]
            del raw["machines"]["front"]["home"]
            raw["machines"]["store"] = {"address": "192.168.104.20", "roles": ["home"]}
            del raw["backup"]

        config = config_of(move_home)
        lines = render(config, names(config), False).home_exports.splitlines()
        self.assertIn("/home 192.168.104.10(rw,sync,no_root_squash,no_subtree_check)", lines)
        self.assertEqual(len(lines), 5)
