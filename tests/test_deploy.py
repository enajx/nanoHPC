"""Tests for what `nanohpc deploy` prepares before running Ansible (inventory, variables, files)."""

import json
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

import yaml

from nanohpc.config import load_config
from nanohpc.deploy import (
    NANOHPC_REPOSITORY,
    SLURM,
    install_source,
    monitor_machines,
    nanohpc_wheel,
    prepare,
    refusal,
)

ROOT = Path(__file__).resolve().parents[1]


class PrepareTest(unittest.TestCase):
    """The work folder holds everything Ansible needs, generated from cluster.yml."""

    def setUp(self) -> None:
        config, errors = load_config(ROOT / "examples" / "cluster.yml", True, False)
        assert errors == [], errors
        self.config = config
        self.hostnames = {name: f"host-{name}" for name in config["machines"]}
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inventory_groups_follow_roles(self) -> None:
        prepare(self.config, self.hostnames, None, True, ["gpu4", "gpu2", "gpu4i"], False, self.work)
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
        prepare(self.config, self.hostnames, None, True, ["gpu2"], False, self.work)
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
        prepare(self.config, self.hostnames, None, True, [], False, self.work)
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
        prepare(self.config, self.hostnames, None, True, ["gpu2"], False, self.work)
        metrics = json.loads((self.work / "vars.json").read_text())["nanohpc"]["metrics"]
        self.assertEqual(metrics["front_address"], "192.168.104.10")
        self.assertEqual(metrics["gpus"]["gpu2"], {"count": 2, "type": "rtx6000ada", "fake": True})
        self.assertEqual(metrics["gpus"]["gpu4"], {"count": 4, "type": "a6000", "fake": False})
        self.assertNotIn("cpu1", metrics["gpus"])
        self.assertEqual(set(metrics["prometheus"]["sha256"]), {"amd64", "arm64"})
        self.assertEqual(metrics["grafana"]["version"], "13.2.3")
        self.assertEqual(set(metrics["grafana"]["sha256"]), {"amd64", "arm64"})

    def test_website_variables_and_site_data(self) -> None:
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
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
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["build"], "front")
        self.assertTrue((Path(website["source"]) / "package-lock.json").is_file())
        built = (Path(website["package"]) / "build-source.sha256").read_text().strip()
        self.assertEqual(website["source_hash"], built)
        self.assertEqual(set(website["node"]["sha256"]), {"x64", "arm64"})

    def test_simulated_cluster_uses_the_test_certificate_server(self) -> None:
        """A simulated cluster cannot reach Let's Encrypt: it asks Pebble, its test server, on the front node."""
        prepare(self.config, self.hostnames, None, True, [], False, self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["acme"]["server"], "https://127.0.0.1:14000/dir")
        self.assertEqual(website["acme"]["ca_bundle"], "/etc/nanohpc/test-acme/ca.pem")

    def test_logo_name_in_site_data(self) -> None:
        self.config["cluster"]["website"]["logo"] = "/somewhere/Lab Logo.SVG"
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
        self.assertEqual(json.loads((self.work / "files" / "site.json").read_text())["logo"], "logo.svg")
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["logo"], {"source": "/somewhere/Lab Logo.SVG", "name": "logo.svg"})

    def test_backup_and_alert_variables(self) -> None:
        self.config["secrets"] = {"slack_webhook": "https://hooks.slack.com/services/T/B/x"}
        self.config["alerts"]["slack"] = True
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
        variables = json.loads((self.work / "vars.json").read_text())["nanohpc"]
        self.assertEqual(
            variables["backup"],
            {
                "kind": "machine",
                "machine": "store",
                "address": "192.168.104.20",
                "destination": "nanohpc-backup@192.168.104.20:/srv/nanohpc-backup/home/",
                "folder": "/srv/nanohpc-backup/home",
                "time": "03:00",
                "exclude": [".cache/", ".venv/", "__pycache__/"],
            },
        )
        self.assertEqual(variables["alerts"], {"slack": True})
        self.assertEqual(set(variables["metrics"]["alertmanager"]["sha256"]), {"amd64", "arm64"})
        # The webhook is a secret: only in a file only this user can read, never in vars.json.
        self.assertNotIn("hooks.slack.com", (self.work / "vars.json").read_text())
        secrets = self.work / "secrets.json"
        self.assertEqual(
            json.loads(secrets.read_text()),
            {"nanohpc_secrets": {"slack_webhook": "https://hooks.slack.com/services/T/B/x"}},
        )
        self.assertEqual(oct(secrets.stat().st_mode & 0o777), oct(0o600))

    def test_backup_to_an_outside_server(self) -> None:
        self.config["backup"]["to"] = "lab@backup.example.org:/srv/cluster"
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
        backup = json.loads((self.work / "vars.json").read_text())["nanohpc"]["backup"]
        self.assertEqual(backup["kind"], "outside")
        self.assertEqual(backup["destination"], "lab@backup.example.org:/srv/cluster/")
        self.assertEqual(backup["address"], "backup.example.org")

    def test_install_source_follows_how_nanohpc_was_installed(self) -> None:
        """The front node installs the pinned version the way the administrator installed nanoHPC."""
        self.assertEqual(install_source(None), {"kind": "pypi", "url": None})
        git = '{"url": "https://github.com/enajx/nanoHPC", "vcs_info": {"vcs": "git", "commit_id": "abc"}}'
        self.assertEqual(install_source(git), {"kind": "git", "url": "https://github.com/enajx/nanoHPC"})
        local = '{"url": "file:///home/admin/nanoHPC", "dir_info": {"editable": true}}'
        self.assertEqual(install_source(local), {"kind": "wheel", "url": "file:///home/admin/nanoHPC"})
        # Anything else: the nanoHPC repository on GitHub, at the version's tag.
        archive = '{"url": "file:///tmp/nanohpc-0.1.0-py3-none-any.whl", "archive_info": {}}'
        self.assertEqual(install_source(archive), {"kind": "git", "url": NANOHPC_REPOSITORY})

    def test_wheel_for_a_local_checkout(self) -> None:
        """From a local checkout, the deploy builds the wheel the front node installs; only of its own version."""
        version = metadata.version("nanohpc")
        source = {"kind": "wheel", "url": ROOT.as_uri()}
        self.assertEqual(nanohpc_wheel(source, version, self.work), None)
        wheels = list((self.work / "files" / "nanohpc-wheel").glob("*.whl"))
        self.assertEqual([wheel.name for wheel in wheels], [f"nanohpc-{version}-py3-none-any.whl"])
        error = nanohpc_wheel(source, "9.9.9", self.work)
        self.assertEqual(
            error,
            f"cluster.yml pins nanoHPC 9.9.9, but this nanoHPC (a local checkout) is {version}: "
            "set nanohpc_version to it, or deploy from nanoHPC 9.9.9",
        )
        self.assertIsNone(nanohpc_wheel({"kind": "pypi", "url": None}, "9.9.9", self.work))

    def test_refusals_before_any_change(self) -> None:
        """With automatic deploys on, every deploy uses the pinned nanoHPC version; an automatic deploy cannot
        turn automatic deploys off (that would remove the root login it runs on, halfway)."""
        self.config["nanohpc_version"] = "0.1.0"
        self.config["auto_deploy"].update(enabled=True, repository="git@github.com:lab/c.git")
        self.assertIsNone(refusal(self.config, False, "0.1.0"))
        self.assertEqual(
            refusal(self.config, False, "0.2.0"),
            "cluster.yml pins nanoHPC 0.1.0 (nanohpc_version), but this is nanoHPC 0.2.0: "
            "deploy with nanoHPC 0.1.0, or change nanohpc_version",
        )
        self.config["auto_deploy"]["enabled"] = False
        self.assertIsNone(refusal(self.config, False, "0.2.0"))
        self.assertEqual(
            refusal(self.config, True, "0.1.0"),
            "this commit turns automatic deploys off: turn them off with nanohpc deploy from the administrator's machine",
        )

    def test_auto_deploy_variables(self) -> None:
        self.config["nanohpc_version"] = "0.1.0"
        self.config["auto_deploy"].update(enabled=True, repository="git@github.com:lab/cluster-config.git")
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
        deploy = json.loads((self.work / "vars.json").read_text())["nanohpc"]["auto_deploy"]
        self.assertEqual(deploy["repository"], "git@github.com:lab/cluster-config.git")
        self.assertEqual(deploy["repository_host"], "github.com")
        self.assertEqual(deploy["branch"], "main")
        self.assertEqual(deploy["every_minutes"], 10)
        self.assertEqual(deploy["version"], "0.1.0")
        self.assertIn(deploy["install"]["kind"], ["pypi", "git", "wheel"])

    def test_monitor_machines(self) -> None:
        """The status collector checks each machine's role, required services, and mounts."""
        prepare(self.config, self.hostnames, None, True, [], False, self.work)
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
        config, errors = load_config(ROOT / "tests" / "sim" / "cluster-home-on-storage.yml", True, False)
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
        prepare(self.config, self.hostnames, None, True, ["gpu4", "gpu2", "gpu4i"], False, self.work)
        files = self.work / "files"
        self.assertIn("NodeHostname=host-gpu4", (files / "slurm.conf").read_text())
        self.assertEqual((files / "gres" / "gpu2.conf").read_text(), "Name=gpu Type=rtx6000ada File=/dev/nvidia[0-1]\n")
        self.assertEqual((files / "gres" / "cpu1.conf").read_text(), "")
        self.assertTrue((files / "job_submit.lua").read_text().startswith("-- Generated by nanoHPC"))
        self.assertIn("192.168.104.20 store\n", (files / "hosts").read_text())

    def test_ssh_config_is_passed_to_ansible(self) -> None:
        ssh_config = self.work / "ssh_config"
        prepare(self.config, self.hostnames, ssh_config, True, [], False, self.work)
        self.assertIn(f"-F {ssh_config}", (self.work / "ansible.cfg").read_text())
        prepare(self.config, self.hostnames, None, False, [], False, self.work)
        self.assertNotIn("-F ", (self.work / "ansible.cfg").read_text())


if __name__ == "__main__":
    unittest.main()
