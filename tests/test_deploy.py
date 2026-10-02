"""Tests for what `nanohpc deploy` prepares before running Ansible (inventory, variables, files)."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from nanohpc.config import load_config
from nanohpc.deploy import SLURM, monitor_machines, prepare

ROOT = Path(__file__).resolve().parents[1]


class PrepareTest(unittest.TestCase):
    """The work folder holds everything Ansible needs, generated from cluster.yml."""

    def setUp(self) -> None:
        config, errors = load_config(ROOT / "examples" / "cluster.yml", True)
        assert errors == [], errors
        self.config = config
        self.hostnames = {name: f"host-{name}" for name in config["machines"]}
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inventory_groups_follow_roles(self) -> None:
        prepare(self.config, self.hostnames, None, True, ["gpu4", "gpu2", "gpu4i"], self.work)
        groups = yaml.safe_load((self.work / "inventory.yml").read_text())["all"]["children"]
        self.assertEqual(sorted(groups["role_front"]["hosts"]), ["front"])
        self.assertEqual(sorted(groups["role_home"]["hosts"]), ["front"])
        self.assertEqual(sorted(groups["role_backup"]["hosts"]), ["store"])
        self.assertEqual(sorted(groups["role_compute"]["hosts"]), ["cpu1", "gpu2", "gpu4", "gpu4i"])
        self.assertEqual(sorted(groups["role_slurm"]["children"]), ["role_compute", "role_front"])
        for role in ("role_front", "role_home", "role_backup", "role_compute"):
            self.assertEqual(groups[role]["vars"]["ansible_remote_tmp"], "$XDG_RUNTIME_DIR/ansible-tmp")
        self.assertNotIn("vars", yaml.safe_load((self.work / "inventory.yml").read_text())["all"])

    def test_variables(self) -> None:
        prepare(self.config, self.hostnames, None, True, ["gpu2"], self.work)
        variables = json.loads((self.work / "vars.json").read_text())["nanohpc"]
        self.assertEqual(variables["cluster_name"], "labcluster")
        self.assertEqual([user["name"] for user in variables["users"]], ["alice", "bob"])
        self.assertEqual(variables["admins"], ["alice"])
        # Only real GPUs are checked with nvidia-smi.
        self.assertEqual(variables["real_gpus"], {"gpu4": 4, "gpu4i": 4})
        # Fake GPUs get placeholder device files.
        self.assertEqual(variables["fake_gpus"], {"gpu2": 2})
        self.assertEqual(variables["max_gpus_per_user"], -1)
        self.assertEqual(variables["slurm"]["version"], SLURM["version"])
        self.assertEqual(variables["files"], str(self.work / "files"))

    def test_home_and_scratch_variables(self) -> None:
        prepare(self.config, self.hostnames, None, True, [], self.work)
        variables = json.loads((self.work / "vars.json").read_text())["nanohpc"]
        home = variables["home"]
        self.assertEqual(home["server"], "front")
        self.assertEqual(home["server_address"], "192.168.104.10")
        self.assertEqual(home["device"], "/dev/vdb")
        self.assertEqual(home["clients"], ["gpu4", "gpu2", "cpu1", "gpu4i"])
        # setquota counts 1 KiB blocks; the example asks for 300 and 400 GB.
        self.assertEqual(
            home["quotas"][0], {"name": "alice", "soft_kib": 300 * 1024 * 1024, "hard_kib": 400 * 1024 * 1024}
        )
        self.assertEqual(home["grace_seconds"], 7 * 86400)
        scratch = variables["scratch"]
        self.assertEqual(scratch["machines"]["gpu4"], {"device": "/dev/vdb", "image_gb": None})
        self.assertEqual(scratch["machines"]["gpu2"], {"device": None, "image_gb": 2})
        self.assertEqual(scratch["cleanup_days"], 14)
        self.assertIn("/home 192.168.104.11(rw", (self.work / "files" / "exports").read_text())
        self.assertIn("node-store", (self.work / "files" / "prometheus.yml").read_text())

    def test_metrics_variables(self) -> None:
        prepare(self.config, self.hostnames, None, True, ["gpu2"], self.work)
        metrics = json.loads((self.work / "vars.json").read_text())["nanohpc"]["metrics"]
        self.assertEqual(metrics["front_address"], "192.168.104.10")
        self.assertEqual(metrics["gpus"]["gpu2"], {"count": 2, "type": "rtx6000ada", "fake": True})
        self.assertEqual(metrics["gpus"]["gpu4"], {"count": 4, "type": "a6000", "fake": False})
        self.assertNotIn("cpu1", metrics["gpus"])
        self.assertEqual(set(metrics["prometheus"]["sha256"]), {"amd64", "arm64"})
        self.assertEqual(metrics["grafana"]["version"], "13.2.3")
        self.assertEqual(set(metrics["grafana"]["sha256"]), {"amd64", "arm64"})

    def test_website_variables_and_site_data(self) -> None:
        prepare(self.config, self.hostnames, None, False, [], self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["hostname"], "cluster.example.org")
        self.assertEqual(website["path"], "/cluster/")
        self.assertEqual(website["grafana_url"], "https://cluster.example.org/cluster/grafana/")
        self.assertEqual(website["https"], "letsencrypt")
        self.assertEqual(website["allow"], [])
        self.assertIsNone(website["forwarded_by"])
        self.assertEqual(website["build"], "package")
        self.assertIsNone(website["logo"])
        self.assertEqual(website["certbot"]["version"], "5.8.0")
        # A real cluster asks Let's Encrypt itself.
        self.assertIsNone(website["acme"])
        self.assertTrue(Path(website["package"]).joinpath("build-source.sha256").name)
        site = json.loads((self.work / "files" / "site.json").read_text())
        self.assertEqual(
            site,
            {
                "cluster_name": "labcluster",
                "logo": None,
                "login_address": "cluster.example.org",
                "home_quota_soft_gb": 300,
                "home_quota_hard_gb": 400,
                "scratch_cleanup_days": 14,
            },
        )

    def test_front_build_variables(self) -> None:
        """build: front builds the source shipped in the package; its hash names the release, and matches the
        prebuilt site's, since both come from the same source."""
        self.config["cluster"]["website"]["build"] = "front"
        prepare(self.config, self.hostnames, None, False, [], self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["build"], "front")
        self.assertTrue((Path(website["source"]) / "package-lock.json").is_file())
        built = (Path(website["package"]) / "build-source.sha256").read_text().strip()
        self.assertEqual(website["source_hash"], built)
        self.assertEqual(set(website["node"]["sha256"]), {"x64", "arm64"})

    def test_simulated_cluster_uses_the_test_certificate_server(self) -> None:
        """A simulated cluster cannot reach Let's Encrypt: it asks Pebble, its test server, on the front node."""
        prepare(self.config, self.hostnames, None, True, [], self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["acme"]["server"], "https://127.0.0.1:14000/dir")
        self.assertEqual(website["acme"]["ca_bundle"], "/etc/nanohpc/test-acme/ca.pem")

    def test_logo_name_in_site_data(self) -> None:
        self.config["cluster"]["website"]["logo"] = "/somewhere/Lab Logo.SVG"
        prepare(self.config, self.hostnames, None, False, [], self.work)
        self.assertEqual(json.loads((self.work / "files" / "site.json").read_text())["logo"], "logo.svg")
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["logo"], {"source": "/somewhere/Lab Logo.SVG", "name": "logo.svg"})

    def test_monitor_machines(self) -> None:
        """The status collector checks each machine's role, required services, and mounts."""
        prepare(self.config, self.hostnames, None, True, [], self.work)
        machines = json.loads((self.work / "files" / "monitor-machines.json").read_text())
        self.assertEqual(
            machines["front"],
            {
                "role": "front",
                "units": [
                    "slurmctld.service",
                    "slurmdbd.service",
                    "munge.service",
                    "mariadb.service",
                    "nfs-server.service",
                ],
                "mounts": ["/", "/home"],
            },
        )
        self.assertEqual(
            machines["gpu4"],
            {"role": "compute", "units": ["slurmd.service", "munge.service"], "mounts": ["/", "/home", "/scratch"]},
        )
        # The backup machine does not mount /home.
        self.assertEqual(machines["store"], {"role": "storage", "units": [], "mounts": ["/"]})
        self.assertEqual(sorted(machines), sorted(self.config["machines"]))
        # With /home on a storage machine, that machine runs the NFS server and the front node mounts /home.
        config, errors = load_config(ROOT / "tests" / "sim" / "cluster-home-on-storage.yml", True)
        assert errors == [], errors
        machines = monitor_machines(config)
        storage = [name for name, machine in config["machines"].items() if "home" in machine["roles"]]
        self.assertEqual(
            machines[storage[0]], {"role": "storage", "units": ["nfs-server.service"], "mounts": ["/", "/home"]}
        )
        front = next(name for name, machine in machines.items() if machine["role"] == "front")
        self.assertNotIn("nfs-server.service", machines[front]["units"])
        self.assertEqual(machines[front]["mounts"], ["/", "/home"])

    def test_files_are_written(self) -> None:
        prepare(self.config, self.hostnames, None, True, ["gpu4", "gpu2", "gpu4i"], self.work)
        files = self.work / "files"
        self.assertIn("NodeHostname=host-gpu4", (files / "slurm.conf").read_text())
        self.assertEqual((files / "gres" / "gpu2.conf").read_text(), "Name=gpu Type=rtx6000ada File=/dev/nvidia[0-1]\n")
        self.assertEqual((files / "gres" / "cpu1.conf").read_text(), "")
        self.assertTrue((files / "job_submit.lua").read_text().startswith("-- Generated by nanoHPC"))
        self.assertIn("192.168.104.20 store\n", (files / "hosts").read_text())

    def test_ssh_config_is_passed_to_ansible(self) -> None:
        ssh_config = self.work / "ssh_config"
        prepare(self.config, self.hostnames, ssh_config, True, [], self.work)
        self.assertIn(f"-F {ssh_config}", (self.work / "ansible.cfg").read_text())
        prepare(self.config, self.hostnames, None, False, [], self.work)
        self.assertNotIn("-F ", (self.work / "ansible.cfg").read_text())


if __name__ == "__main__":
    unittest.main()
