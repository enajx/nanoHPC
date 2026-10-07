"""Tests for what `nanohpc deploy` prepares before running Ansible (inventory, variables, files)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml

from nanohpc import deploy
from nanohpc.config import load_config
from nanohpc.deploy import (
    DRY_RUN_FAILED,
    NANOHPC_REPOSITORY,
    REAL_RUN_FAILED,
    SLURM,
    Only,
    ansible_cfg,
    check_then_apply,
    install_source,
    monitor_machines,
    nanohpc_wheel,
    needed_machines,
    only_machines,
    prepare,
    refusal,
)
from nanohpc.maintenance_lock import MaintenanceLockBusy, MaintenanceLockError

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
        prepare(self.config, self.hostnames, None, True, ["gpu4", "gpu2", "gpu4i"], False, None, self.work)
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
        prepare(self.config, self.hostnames, None, True, ["gpu2"], False, None, self.work)
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
        # The base role records it in /etc/nanohpc/version, read by nanohpc check.
        self.assertEqual(variables["version"], metadata.version("nanohpc"))

    def test_home_and_scratch_variables(self) -> None:
        prepare(self.config, self.hostnames, None, True, [], False, None, self.work)
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
        self.assertEqual(scratch["job_retention_days"], 7)
        self.assertIn("/home 192.168.104.11(rw", (self.work / "files" / "exports").read_text())
        self.assertIn("node-store", (self.work / "files" / "prometheus.yml").read_text())

    def test_shared_dataset_server_and_clients(self) -> None:
        self.config["machines"]["store"]["roles"].append("shared")
        self.config["machines"]["store"]["shared"] = {"device": "/dev/vdb"}
        prepare(self.config, self.hostnames, None, True, [], False, None, self.work)
        groups = yaml.safe_load((self.work / "inventory.yml").read_text())["all"]["children"]
        self.assertEqual(list(groups["role_shared"]["hosts"]), ["store"])
        variables = json.loads((self.work / "vars.json").read_text())["nanohpc"]
        self.assertEqual(
            variables["shared"],
            {
                "server": "store",
                "server_address": "192.168.104.20",
                "device": "/dev/vdb",
                "clients": ["gpu4", "gpu2", "cpu1", "gpu4i"],
            },
        )
        machines = monitor_machines(self.config)
        self.assertIn("/shared", machines["store"]["mounts"])
        self.assertIn("/shared/datasets", machines["gpu4"]["mounts"])

    def test_partial_node_includes_the_shared_datasets_server(self) -> None:
        """A new compute node needs the shared server's updated export before mounting it."""
        self.config["machines"]["datasets"] = {
            "address": "192.168.104.30",
            "roles": ["shared"],
            "shared": {"device": "/dev/vdb"},
        }
        selected = only_machines(self.config, Only("node", "gpu4"))
        self.assertIn("datasets", selected)
        self.assertEqual(needed_machines(self.config, selected, "gpu4")["datasets"], "the shared datasets server")

    def test_metrics_variables(self) -> None:
        prepare(self.config, self.hostnames, None, True, ["gpu2"], False, None, self.work)
        metrics = json.loads((self.work / "vars.json").read_text())["nanohpc"]["metrics"]
        self.assertEqual(metrics["front_address"], "192.168.104.10")
        self.assertEqual(metrics["gpus"]["gpu2"], {"count": 2, "type": "rtx6000ada", "fake": True})
        self.assertEqual(metrics["gpus"]["gpu4"], {"count": 4, "type": "a6000", "fake": False})
        self.assertNotIn("cpu1", metrics["gpus"])
        self.assertEqual(set(metrics["prometheus"]["sha256"]), {"amd64", "arm64"})
        self.assertEqual(metrics["grafana"]["version"], "13.2.3")
        self.assertEqual(set(metrics["grafana"]["sha256"]), {"amd64", "arm64"})

    def test_website_variables_and_site_data(self) -> None:
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
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
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["build"], "front")
        self.assertTrue((Path(website["source"]) / "package-lock.json").is_file())
        built = (Path(website["package"]) / "build-source.sha256").read_text().strip()
        self.assertEqual(website["source_hash"], built)
        self.assertEqual(set(website["node"]["sha256"]), {"x64", "arm64"})

    def test_simulated_cluster_uses_the_test_certificate_server(self) -> None:
        """A simulated cluster cannot reach Let's Encrypt: it asks Pebble, its test server, on the front node."""
        prepare(self.config, self.hostnames, None, True, [], False, None, self.work)
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["acme"]["server"], "https://127.0.0.1:14000/dir")
        self.assertEqual(website["acme"]["ca_bundle"], "/etc/nanohpc/test-acme/ca.pem")

    def test_logo_name_in_site_data(self) -> None:
        self.config["cluster"]["website"]["logo"] = "/somewhere/Lab Logo.SVG"
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
        self.assertEqual(json.loads((self.work / "files" / "site.json").read_text())["logo"], "logo.svg")
        website = json.loads((self.work / "vars.json").read_text())["nanohpc"]["website"]
        self.assertEqual(website["logo"], {"source": "/somewhere/Lab Logo.SVG", "name": "logo.svg"})

    def test_backup_and_alert_variables(self) -> None:
        self.config["secrets"] = {"slack_webhook": "https://hooks.slack.com/services/T/B/x"}
        self.config["alerts"]["slack"] = True
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
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
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
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
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
        deploy = json.loads((self.work / "vars.json").read_text())["nanohpc"]["auto_deploy"]
        self.assertEqual(deploy["repository"], "git@github.com:lab/cluster-config.git")
        self.assertEqual(deploy["repository_host"], "github.com")
        self.assertEqual(deploy["branch"], "main")
        self.assertEqual(deploy["every_minutes"], 10)
        self.assertEqual(deploy["version"], "0.1.0")
        self.assertIn(deploy["install"]["kind"], ["pypi", "git", "wheel"])

    def test_monitor_machines(self) -> None:
        """The status collector checks each machine's role, required services, and mounts."""
        prepare(self.config, self.hostnames, None, True, [], False, None, self.work)
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
        prepare(self.config, self.hostnames, None, True, ["gpu4", "gpu2", "gpu4i"], False, None, self.work)
        files = self.work / "files"
        self.assertIn("NodeHostname=host-gpu4", (files / "slurm.conf").read_text())
        self.assertEqual((files / "gres" / "gpu2.conf").read_text(), "Name=gpu Type=rtx6000ada File=/dev/nvidia[0-1]\n")
        self.assertEqual((files / "gres" / "cpu1.conf").read_text(), "")
        self.assertTrue((files / "job_submit.lua").read_text().startswith("-- Generated by nanoHPC"))
        self.assertIn("192.168.104.20 store\n", (files / "hosts").read_text())

    def test_ssh_config_is_passed_to_ansible(self) -> None:
        ssh_config = self.work / "ssh_config"
        prepare(self.config, self.hostnames, ssh_config, True, [], False, None, self.work)
        self.assertIn(f"-F {ssh_config}", (self.work / "ansible.cfg").read_text())
        prepare(self.config, self.hostnames, None, False, [], False, None, self.work)
        self.assertNotIn("-F ", (self.work / "ansible.cfg").read_text())


class DryRunTest(unittest.TestCase):
    """Every deploy runs the playbook in check mode first. Machines whose dry run failed are left out of the real
    run, unless the front node or the home machine failed: then nothing is deployed."""

    def setUp(self) -> None:
        self.machines = ["front", "gpu4", "cpu1"]
        self.needed = {"front": "the front node and the home machine"}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.calls: list[list[str]] = []
        self.variables: list[dict[str, Any]] = []

    def fake_runner(self, results: list[tuple[int, dict[str, Any] | None]]) -> Callable[[list[str], Path], int]:
        """A stand-in for ansible-playbook (mocked: no Ansible runs): each call keeps the variables file it was
        given, writes the next record, and returns its exit code. A record of None writes nothing."""

        def run(arguments: list[str], record: Path) -> int:
            code, written = results[len(self.calls)]
            self.calls.append(arguments)
            self.variables.append(json.loads(Path(arguments[arguments.index("-e") + 1][1:]).read_text()))
            if written is not None:
                record.write_text(json.dumps(written))
            return code

        return run

    def run_flow(self, results: list[tuple[int, dict[str, Any] | None]], dry_run_only: bool) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = check_then_apply(self.fake_runner(results), self.machines, self.needed, self.folder, dry_run_only)
        return code, output.getvalue()

    def test_dry_run_passes_then_real_run(self) -> None:
        record = {
            "changed": {"front": ["base : Record roles", "accounts : Create users"]},
            "failed": {},
            "facts": {"front": {"nanohpc_dry_run_auto_deploy_state": "abc"}},
        }
        code, output = self.run_flow([(0, record), (0, {"changed": {}, "failed": {}, "facts": {}})], False)
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in self.calls], ["--check", "-e"])
        self.assertEqual(self.variables[0], {"nanohpc_left_out": [], "nanohpc_dry_run": {}})
        # The real run gets what the dry run read (for example the last automatic deploy, to compare).
        self.assertEqual(
            self.variables[1],
            {"nanohpc_left_out": [], "nanohpc_dry_run": {"front": {"nanohpc_dry_run_auto_deploy_state": "abc"}}},
        )
        self.assertIn("Dry run: front would change 2 things: base : Record roles; accounts : Create users\n", output)
        self.assertIn("Dry run: gpu4 has nothing to change\n", output)
        self.assertIn("Dry run: cpu1 has nothing to change\n", output)
        self.assertIn("Dry run passed: applying the changes.", output)

    def test_machine_that_fails_its_dry_run_is_left_out(self) -> None:
        """The other machines are deployed; the deploy still fails, with its own exit code, and says why."""
        record = {
            "changed": {"front": ["base : Record roles"]},
            "failed": {"gpu4": "preflight : Check the scratch disk: gpu4: /dev/vdb has no filesystem."},
            "facts": {},
        }
        real = {"changed": {}, "failed": {"gpu4": "Leave out: gpu4 is left out"}, "facts": {}}
        code, output = self.run_flow([(2, record), (2, real)], False)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertEqual(self.variables[1]["nanohpc_left_out"], ["gpu4"])
        self.assertIn("Dry run: front would change 1 thing: base : Record roles\n", output)
        self.assertIn(
            "Dry run: gpu4 failed: preflight : Check the scratch disk: gpu4: /dev/vdb has no filesystem.", output
        )
        self.assertIn(
            "Dry run failed on gpu4: left out of this deploy, unchanged. Applying the changes on the others.", output
        )
        self.assertIn(
            "Left out of this deploy (their dry run failed; nothing was changed on them):\n"
            "  gpu4: preflight : Check the scratch disk: gpu4: /dev/vdb has no filesystem.",
            output,
        )

    def test_front_or_home_machine_failing_stops_everything(self) -> None:
        record = {"changed": {}, "failed": {"front": "preflight : Require Ubuntu: no", "cpu1": "x: y"}, "facts": {}}
        code, output = self.run_flow([(2, record)], False)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(
            "Dry run failed on front (the front node and the home machine), which the rest of this deploy depends on: "
            "nothing was deployed, and nothing was changed.",
            output,
        )

    def test_real_run_failure(self) -> None:
        """A failure after the dry run passed: the machines may be partly changed, with another exit code."""
        passed = {"changed": {}, "failed": {}, "facts": {}}
        real = {
            "changed": {"cpu1": ["slurm : Start"]},
            "failed": {"cpu1": "slurm_compute : Run slurmd: failed"},
            "facts": {},
        }
        code, output = self.run_flow([(0, passed), (2, real)], False)
        self.assertEqual(code, REAL_RUN_FAILED)
        self.assertIn(
            "The deploy failed on these machines, which may be partly changed. Fix the problem and deploy again:\n"
            "  cpu1: slurm_compute : Run slurmd: failed",
            output,
        )
        self.calls, self.variables = [], []
        code, output = self.run_flow([(0, passed), (4, None)], False)
        self.assertEqual(code, REAL_RUN_FAILED)
        self.assertIn("left no record", output)

    def test_left_out_machines_listed_again_when_the_real_run_fails(self) -> None:
        record = {"changed": {}, "failed": {"gpu4": "preflight : Check the disk: no filesystem"}, "facts": {}}
        real = {
            "changed": {},
            "failed": {"gpu4": "Leave out", "cpu1": "slurm_compute : Run slurmd: failed"},
            "facts": {},
        }
        code, output = self.run_flow([(2, record), (2, real)], False)
        self.assertEqual(code, REAL_RUN_FAILED)
        self.assertIn("  cpu1: slurm_compute : Run slurmd: failed", output)
        self.assertTrue(
            output.rstrip().endswith(
                "Left out of this deploy (their dry run failed; nothing was changed on them):\n"
                "  gpu4: preflight : Check the disk: no filesystem"
            ),
            output,
        )

    def test_automatic_deploy_in_between(self) -> None:
        """An automatic deploy ran between the dry run and the real run: the real run stops at the pause, before
        changing anything, and says to deploy again (not "may be partly changed")."""
        passed = {"changed": {}, "failed": {}, "facts": {}}
        message = (
            "auto_deploy : Stop if an automatic deploy ran during the dry run: An automatic deploy ran during this "
            "deploy's dry run, so the dry run is out of date. Nothing was changed; run nanohpc deploy again."
        )
        real = {"changed": {}, "failed": {"front": message}, "facts": {}}
        code, output = self.run_flow([(0, passed), (2, real)], False)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertIn("An automatic deploy ran during this deploy's dry run", output)
        self.assertNotIn("may be partly changed", output)

    def test_dry_run_only(self) -> None:
        code, output = self.run_flow([(0, {"changed": {"cpu1": ["scratch : Mount"]}, "failed": {}, "facts": {}})], True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Dry run: cpu1 would change 1 thing: scratch : Mount", output)
        self.assertIn("Dry run passed. Nothing was changed (--dry-run).", output)
        self.calls = []
        record = {"changed": {}, "failed": {"cpu1": "a: b"}, "facts": {}}
        code, output = self.run_flow([(2, record)], True)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Dry run failed on cpu1. Nothing was changed (--dry-run).", output)

    def test_unclear_dry_run_never_applies(self) -> None:
        """An Ansible error with no failed machine, or no record at all, stops before any change."""
        code, output = self.run_flow([(4, {"changed": {}, "failed": {}, "facts": {}})], False)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(
            "Dry run failed (ansible-playbook exit code 4): nothing was deployed, and nothing was changed.", output
        )
        self.calls = []
        code, output = self.run_flow([(0, None)], False)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("left no record", output)

    def test_record_from_real_ansible(self) -> None:
        """The record comes from nanoHPC's callback in a real ansible-playbook run on this machine, in check mode."""
        target = self.folder / "file"
        playbook = self.folder / "play.yml"
        playbook.write_text(
            textwrap.dedent(f"""\
                - hosts: all
                  gather_facts: false
                  tasks:
                    - name: Write a file
                      ansible.builtin.copy:
                        dest: {target}
                        content: hello
                    - name: Read nothing
                      ansible.builtin.debug:
                        msg: hi
                    - name: Run a command that changes something
                      ansible.builtin.command: touch {target}
                    - name: Run commands that change something
                      ansible.builtin.command: touch {target}
                      loop: [1, 2]
                    - name: Run a command that may change something
                      ansible.builtin.command: echo changed
                      register: maybe
                      changed_when: "'changed' in maybe.stdout"
                    - name: Run a command that only reads
                      ansible.builtin.command: echo read
                      changed_when: false
                    - name: Skip a command
                      ansible.builtin.command: touch {target}
                      when: false
                    - name: Keep something for the real run
                      ansible.builtin.set_fact:
                        nanohpc_dry_run_example: kept
                        other_fact: not kept
                    - name: Stop here
                      ansible.builtin.assert:
                        that: false
                        fail_msg: the check failed
                      when: stop | bool
            """)
        )
        (self.folder / "ansible.cfg").write_text(ansible_cfg(None))
        playbook_command = str(Path(sys.executable).parent / "ansible-playbook")

        def run(arguments: list[str], record: Path) -> int:
            command = [playbook_command, "-i", "local,", "-c", "local", str(playbook), *arguments]
            environment = {
                **os.environ,
                "ANSIBLE_CONFIG": str(self.folder / "ansible.cfg"),
                "NANOHPC_RECORD": str(record),
                "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
            }
            self.calls.append(arguments)
            result = subprocess.run(
                [*command, "-e", f"stop={self.stop}"], env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            return result.returncode

        self.stop = False
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = check_then_apply(run, ["local"], {"local": "this machine"}, self.folder, True)
        self.assertEqual(code, 0, output.getvalue())
        # Commands skipped in check mode count as changes, unless they are marked as never changing anything.
        self.assertIn(
            "Dry run: local would change 4 things: Write a file; Run a command that changes something;"
            " Run commands that change something; Run a command that may change something\n",
            output.getvalue(),
        )
        self.assertFalse(target.exists())
        record = json.loads((self.folder / "dry-run.json").read_text())
        self.assertEqual(record["facts"], {"local": {"nanohpc_dry_run_example": "kept"}})
        self.stop = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = check_then_apply(run, ["local"], {"local": "this machine"}, self.folder, False)
        self.assertEqual(code, DRY_RUN_FAILED)
        self.assertEqual(len(self.calls), 2, "no real run")
        self.assertIn("Dry run: local failed: Stop here: the check failed", output.getvalue())
        self.assertFalse(target.exists())


class DeployMaintenanceTest(unittest.TestCase):
    """A deploy needs the coordinator lock before it prepares or applies changes."""

    def test_manual_deploy_refuses_busy_lock_before_prepare(self) -> None:
        config = {"cluster": {"name": "lab"}, "machines": {"front": {}}, "auto_deploy": {"enabled": False}}
        found = deploy.Probe({"front": "front"}, [], [], {"front": None})
        with (
            patch.object(deploy, "refusal", return_value=None),
            patch.object(deploy, "probe", return_value=found),
            patch.object(deploy, "front_machine", return_value=("front", {})),
            patch.object(deploy, "prepare") as prepare_mock,
            patch("nanohpc.maintenance_lock.acquire", side_effect=MaintenanceLockBusy("busy")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = deploy.deploy(config, None, False, [], False, True, None)
        self.assertEqual(result, 1)
        prepare_mock.assert_not_called()

    def test_automatic_deploy_requires_inherited_lock_before_probe(self) -> None:
        config = {"cluster": {"name": "lab"}, "machines": {"front": {}}, "auto_deploy": {"enabled": True}}
        with (
            patch.object(deploy, "refusal", return_value=None),
            patch.object(deploy, "probe") as probe_mock,
            patch("nanohpc.maintenance_lock.validate_inherited", side_effect=MaintenanceLockError("invalid")),
            patch.dict(os.environ, {"NANOHPC_MAINTENANCE_FD": "5"}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = deploy.deploy(config, None, False, [], True, True, None)
        self.assertEqual(result, 1)
        probe_mock.assert_not_called()

    def test_manual_deploy_rejects_facts_changed_while_waiting_for_lock(self) -> None:
        config = {"cluster": {"name": "lab"}, "machines": {"front": {}}, "auto_deploy": {"enabled": False}}
        before = deploy.Probe({"front": "front"}, [], [], {"front": None})
        after = deploy.Probe({"front": "front"}, [], [], {"front": ["front"]})
        holder = MagicMock()
        with (
            patch.object(deploy, "refusal", return_value=None),
            patch.object(deploy, "probe", side_effect=[before, after]),
            patch.object(deploy, "front_machine", return_value=("front", {})),
            patch.object(deploy, "prepare") as prepare_mock,
            patch("nanohpc.maintenance_lock.acquire", return_value=holder),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = deploy.deploy(config, None, False, [], False, True, None)
        self.assertEqual(result, 1)
        prepare_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
