"""Tests for cluster.yml validation, driven through `nanohpc validate` and `check_config`."""

import copy
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from nanohpc.config import check_config, load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cluster.yml"
MINIMAL = ROOT / "examples" / "minimal.yml"


def example() -> dict[str, Any]:
    """Return a fresh copy of the test-cluster example."""
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text()))


def run_validate(path: Path) -> subprocess.CompletedProcess[str]:
    """Run the installed `nanohpc validate` command on one file."""
    command = shutil.which("nanohpc")
    assert command is not None, "the nanohpc command is not installed in this environment"
    return subprocess.run([command, "validate", str(path)], capture_output=True, text=True, check=False)


class ValidateCommandTest(unittest.TestCase):
    """The command the administrator runs."""

    def test_examples_are_valid(self) -> None:
        for path in (EXAMPLE, MINIMAL):
            with self.subTest(path=path.name):
                result = run_validate(path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("valid", result.stdout)

    def test_invalid_file_names_the_field_and_fails(self) -> None:
        config = example()
        del config["machines"]["gpu4"]["cpu"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cluster.yml"
            path.write_text(yaml.safe_dump(config))
            result = run_validate(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("machines.gpu4.cpu is required", result.stderr)

    def test_all_errors_are_reported_together(self) -> None:
        config = example()
        del config["machines"]["gpu4"]["cpu"]
        config["machines"]["gpu2"]["memory_mb"] = 0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cluster.yml"
            path.write_text(yaml.safe_dump(config))
            result = run_validate(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("machines.gpu4.cpu is required", result.stderr)
        self.assertIn("machines.gpu2.memory_mb must be a positive integer", result.stderr)


class HeterogeneousClusterTest(unittest.TestCase):
    """Valid heterogeneous setups: CPU-only nodes, admin-defined partitions, separate storage machines."""

    def test_example_has_no_errors(self) -> None:
        _, errors = check_config(example())
        self.assertEqual(errors, [])

    def test_cpu_only_node_has_no_gpus(self) -> None:
        config, errors = check_config(example())
        self.assertEqual(errors, [])
        self.assertIsNone(config["machines"]["cpu1"]["gpu"])

    def test_home_on_storage_machine(self) -> None:
        raw = example()
        raw["machines"]["front"]["roles"] = ["front"]
        del raw["machines"]["front"]["home"]
        raw["machines"]["store"] = {"address": "192.168.104.20", "roles": ["home"]}
        del raw["backup"]
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["machines"]["store"]["home"], {"device": None})
        self.assertIsNone(config["backup"])

    def test_admin_defined_partitions(self) -> None:
        raw = example()
        raw["partitions"] = {"gpu": {"default": True, "max_time": "2-00:00:00"}, "debug": {"max_time": "00:30:00"}}
        for name in ("gpu4", "gpu2", "gpu4i"):
            raw["machines"][name]["partitions"] = ["gpu"]
        raw["machines"]["cpu1"]["partitions"] = ["debug"]
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["partitions"]["debug"]["max_gpus_per_user"], "unlimited")

    def test_backup_to_outside_ssh_server(self) -> None:
        raw = example()
        del raw["machines"]["store"]
        raw["backup"]["to"] = "lab@storage.university.example:/backups/cluster"
        _, errors = check_config(raw)
        self.assertEqual(errors, [])

    def test_partition_job_types(self) -> None:
        config, errors = check_config(example())
        self.assertEqual(errors, [])
        self.assertEqual(config["partitions"]["main"]["jobs"], "batch")
        self.assertEqual(config["partitions"]["interactive"]["jobs"], "interactive")
        errors = mutate(lambda raw: raw["partitions"]["main"].update(jobs="gpu"))
        self.assertIn("partitions.main.jobs must be batch, interactive, or any", errors)
        errors = mutate(lambda raw: raw["partitions"].update(normal={"max_time": "01:00:00"}))
        self.assertIn("partitions.normal: the name normal is reserved by Slurm", errors)

    def test_minimal_gets_defaults(self) -> None:
        config, errors = check_config(yaml.safe_load(MINIMAL.read_text()))
        self.assertEqual(errors, [])
        self.assertEqual(config["policy"]["fairshare_weight"], 10000)
        self.assertEqual(config["policy"]["max_gpus_per_user"], "unlimited")
        self.assertEqual(config["home"], {"quota_soft_gb": 300, "quota_hard_gb": 400, "quota_grace": "7days"})
        self.assertEqual(config["scratch"], {"cleanup_days": 14, "job_retention_days": 7})
        self.assertIsNone(config["backup"])
        self.assertEqual(config["alerts"], {"slack": False})
        self.assertEqual(
            config["auto_deploy"],
            {"enabled": False, "repository": None, "branch": "main", "every_minutes": 10, "webhook": False},
        )
        self.assertEqual(config["cluster"]["website"]["login_address"], "cluster.mylab.example.org")
        website = config["cluster"]["website"]
        self.assertEqual(website["path"], "/cluster/")
        self.assertEqual(website["allow"], [])
        self.assertIsNone(website["forwarded_by"])
        self.assertEqual(website["build"], "package")
        self.assertEqual(config["machines"]["gpu1"]["aliases"], [])
        self.assertEqual(config["partitions"]["main"]["jobs"], "any")

    def test_job_retention_days_must_be_positive(self) -> None:
        """A bad cleanup cutoff is rejected before any deploy can run."""
        raw = example()
        raw["scratch"]["job_retention_days"] = 0
        _, errors = check_config(raw)
        self.assertIn("scratch.job_retention_days must be a positive integer", errors)


def mutate(change: Callable[[dict[str, Any]], None]) -> list[str]:
    """Apply one change to the example and return the validation errors."""
    raw = example()
    change(raw)
    return check_config(raw)[1]


class WebsiteFilesTest(unittest.TestCase):
    """Certificate, key, and logo paths are on the administrator's machine, relative to cluster.yml."""

    def write(self, folder: Path, website: dict[str, Any]) -> Path:
        raw = yaml.safe_load(EXAMPLE.read_text())
        raw["cluster"]["website"].update(website)
        path = folder / "cluster.yml"
        path.write_text(yaml.safe_dump(raw))
        return path

    def test_relative_paths_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "tls").mkdir()
            self.make_certificate(folder / "tls", "cert", "cluster.example.org", "90")
            (folder / "tls" / "cert.key").rename(folder / "tls" / "key.pem")
            (folder / "logo.png").write_text("x")
            website = {
                "https": "own",
                "certificate": "tls/cert.pem",
                "certificate_key": "tls/key.pem",
                "logo": "logo.png",
            }
            config, errors = load_config(self.write(folder, website), True, False)
            self.assertEqual(errors, [])
            resolved = config["cluster"]["website"]
            self.assertEqual(resolved["certificate"], str(folder.resolve() / "tls/cert.pem"))
            self.assertEqual(resolved["certificate_key"], str(folder.resolve() / "tls/key.pem"))
            self.assertEqual(resolved["logo"], str(folder.resolve() / "logo.png"))

    def make_certificate(self, folder: Path, name: str, hostname: str, days: str) -> None:
        """Write a self-signed certificate and key for `hostname` as <name>.pem and <name>.key."""
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", days, "-subj", f"/CN={hostname}",
             "-addext", f"subjectAltName=DNS:{hostname}", "-keyout", f"{name}.key", "-out", f"{name}.pem"],
            cwd=folder, capture_output=True, check=True,
        )  # fmt: skip

    def test_own_certificate_is_checked(self) -> None:
        """Before touching any machine: the certificate matches its key, names the hostname, and is not expired."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.make_certificate(folder, "good", "cluster.example.org", "90")
            self.make_certificate(folder, "other", "other.example.org", "90")
            good = {"https": "own", "certificate": "good.pem", "certificate_key": "good.key"}
            self.assertEqual(load_config(self.write(folder, good), True, False)[1], [])
            mismatched = {"https": "own", "certificate": "good.pem", "certificate_key": "other.key"}
            self.assertIn(
                "cluster.website.certificate_key does not match cluster.website.certificate",
                load_config(self.write(folder, mismatched), True, False)[1],
            )
            wrong_name = {"https": "own", "certificate": "other.pem", "certificate_key": "other.key"}
            self.assertIn(
                "cluster.website.certificate is not for cluster.example.org (it names other.example.org)",
                load_config(self.write(folder, wrong_name), True, False)[1],
            )
            (folder / "broken.pem").write_text("not a certificate")
            broken = {"https": "own", "certificate": "broken.pem", "certificate_key": "good.key"}
            self.assertIn(
                "cluster.website.certificate is not a PEM certificate",
                load_config(self.write(folder, broken), True, False)[1],
            )

    def test_automatic_deploy_keeps_the_front_nodes_certificate(self) -> None:
        """On the front node, the administrator's certificate files are not there: an automatic deploy keeps the
        certificate and key the last manual deploy installed."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            website = {
                "https": "own",
                "certificate": "/admin/laptop/cert.pem",
                "certificate_key": "/admin/laptop/key.pem",
            }
            config, errors = load_config(self.write(folder, website), True, True)
            self.assertEqual(errors, [])
            self.assertIsNone(config["cluster"]["website"]["certificate"])
            self.assertIsNone(config["cluster"]["website"]["certificate_key"])

    def test_missing_files_and_logo_types_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "logo.bmp").write_text("x")
            website = {"https": "own", "certificate": "cert.pem", "certificate_key": "/no/key.pem", "logo": "logo.bmp"}
            _, errors = load_config(self.write(folder, website), True, False)
            self.assertIn(f"cluster.website.certificate: {folder.resolve() / 'cert.pem'} not found", errors)
            self.assertIn("cluster.website.certificate_key: /no/key.pem not found", errors)
            self.assertIn("cluster.website.logo must be a .png, .svg, .jpg, or .webp file", errors)


class AutoDeployTest(unittest.TestCase):
    """Automatic deploys: the configuration repository, its branch, how often, the webhook, and the pinned version."""

    def test_defaults(self) -> None:
        config, errors = check_config(example())
        self.assertEqual(errors, [])
        self.assertEqual(
            config["auto_deploy"],
            {"enabled": False, "repository": None, "branch": "main", "every_minutes": 10, "webhook": False},
        )
        self.assertIsNone(config["nanohpc_version"])

    def test_enabled(self) -> None:
        raw = example()
        raw["nanohpc_version"] = "0.1.0"
        raw["auto_deploy"] = {
            "enabled": True,
            "repository": "git@github.com:lab/cluster-config.git",
            "branch": "stable",
            "every_minutes": 5,
        }
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["auto_deploy"]["branch"], "stable")
        self.assertEqual(config["nanohpc_version"], "0.1.0")

    def test_invalid(self) -> None:
        def enabled(raw: dict[str, Any]) -> None:
            raw["auto_deploy"] = {"enabled": True, "repository": "git@github.com:lab/c.git"}
            raw["nanohpc_version"] = "0.1.0"

        cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
            (lambda raw: raw["auto_deploy"].update(enabled=True, repository="git@github.com:lab/c.git"),
             "nanohpc_version is required when auto_deploy.enabled is true"),
            (lambda raw: (enabled(raw), raw["auto_deploy"].update(repository="https://github.com/lab/c.git")),
             "auto_deploy.repository must be a Git repository over SSH"),
            (lambda raw: (enabled(raw), raw["auto_deploy"].update(branch="main; rm")), "auto_deploy.branch must be a branch name"),
            (lambda raw: (enabled(raw), raw["auto_deploy"].update(repository="ssh://git@host.example.org:2222/lab/c.git")),
             "auto_deploy.repository must be a Git repository over SSH"),
            (lambda raw: (enabled(raw), raw["auto_deploy"].update(every_minutes=0)), "auto_deploy.every_minutes must be"),
            (lambda raw: (enabled(raw), raw.update(nanohpc_version="latest")), "nanohpc_version must be a version"),
            (lambda raw: raw["auto_deploy"].update(webhook=True), "auto_deploy.webhook needs auto_deploy.enabled"),
        ]  # fmt: skip
        for change, expected in cases:
            with self.subTest(expected):
                errors = mutate(change)
                self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")

    def test_webhook_secret_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            raw = example()
            raw["nanohpc_version"] = "0.1.0"
            raw["auto_deploy"] = {"enabled": True, "repository": "git@github.com:lab/c.git", "webhook": True}
            path = folder / "cluster.yml"
            path.write_text(yaml.safe_dump(raw))
            _, errors = load_config(path, True, False)
            self.assertIn(
                f"auto_deploy.webhook is true but {folder.resolve() / '.env'} has no NANOHPC_DEPLOY_WEBHOOK_SECRET",
                errors,
            )
            (folder / ".env").write_text("NANOHPC_DEPLOY_WEBHOOK_SECRET=short\n")
            _, errors = load_config(path, True, False)
            self.assertIn(
                "NANOHPC_DEPLOY_WEBHOOK_SECRET in .env must be at least 32 characters (for example openssl rand -hex 32)",
                errors,
            )
            secret = "s" * 40
            (folder / ".env").write_text(f"NANOHPC_DEPLOY_WEBHOOK_SECRET={secret}\n")
            config, errors = load_config(path, True, False)
            self.assertEqual(errors, [])
            self.assertEqual(config["secrets"]["deploy_webhook"], secret)


class SecretsTest(unittest.TestCase):
    """Secrets are in a .env file next to cluster.yml (never committed), read only when they are needed."""

    def write(self, folder: Path, slack: bool, env: str | None) -> Path:
        raw = yaml.safe_load(EXAMPLE.read_text())
        raw["alerts"]["slack"] = slack
        path = folder / "cluster.yml"
        path.write_text(yaml.safe_dump(raw))
        if env is not None:
            (folder / ".env").write_text(env)
        return path

    def test_slack_webhook_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            env = "# alerts\nOTHER=1\nNANOHPC_SLACK_WEBHOOK='https://hooks.slack.com/services/T/B/x'\n"
            config, errors = load_config(self.write(folder, True, env), True, False)
            self.assertEqual(errors, [])
            self.assertEqual(
                config["secrets"], {"slack_webhook": "https://hooks.slack.com/services/T/B/x", "deploy_webhook": None}
            )

    def test_missing_webhook_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _, errors = load_config(self.write(folder, True, None), True, False)
            self.assertIn(f"alerts.slack is true but {folder.resolve() / '.env'} has no NANOHPC_SLACK_WEBHOOK", errors)
            _, errors = load_config(self.write(folder, True, "NANOHPC_SLACK_WEBHOOK=not a url\n"), True, False)
            self.assertIn("NANOHPC_SLACK_WEBHOOK in .env must be an http(s) URL", errors)

    def test_secrets_are_read_before_their_feature_is_on(self) -> None:
        """Secrets in .env reach the front node even when their feature is still off, so a later commit can turn
        Slack alerts or the webhook on in an automatic deploy."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            secret = "s" * 40
            env = f"NANOHPC_SLACK_WEBHOOK=https://hooks.slack.com/services/T/B/x\nNANOHPC_DEPLOY_WEBHOOK_SECRET={secret}\n"
            config, errors = load_config(self.write(folder, False, env), True, False)
            self.assertEqual(errors, [])
            self.assertEqual(
                config["secrets"],
                {"slack_webhook": "https://hooks.slack.com/services/T/B/x", "deploy_webhook": secret},
            )

    def test_no_secrets_needed_without_slack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config, errors = load_config(self.write(Path(directory), False, None), True, False)
            self.assertEqual(errors, [])
            self.assertEqual(config["secrets"], {"slack_webhook": None, "deploy_webhook": None})


class InvalidConfigTest(unittest.TestCase):
    """Each wrong configuration is rejected with a message naming the field."""

    def assert_rejected(self, change: Callable[[dict[str, Any]], None], expected: str) -> None:
        errors = mutate(change)
        self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")

    def test_cases(self) -> None:
        machines = lambda raw: raw["machines"]
        cases: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
            ("not a mapping", lambda raw: raw.update(machines=[]), "machines must be a mapping"),
            ("unknown top-level key", lambda raw: raw.update(polcy={}), "polcy is not a known field"),
            (
                "unknown machine key",
                lambda raw: machines(raw)["gpu4"].update(memroy_mb=1),
                "machines.gpu4.memroy_mb is not a known field",
            ),
            (
                "missing cpu part",
                lambda raw: machines(raw)["gpu4"]["cpu"].pop("sockets"),
                "machines.gpu4.cpu.sockets is required",
            ),
            (
                "zero memory",
                lambda raw: machines(raw)["gpu4"].update(memory_mb=0),
                "machines.gpu4.memory_mb must be a positive integer",
            ),
            (
                "boolean count",
                lambda raw: machines(raw)["gpu4"]["gpu"].update(count=True),
                "machines.gpu4.gpu.count must be a positive integer",
            ),
            (
                "missing gpu type",
                lambda raw: machines(raw)["gpu4"]["gpu"].pop("type"),
                "machines.gpu4.gpu.type is required",
            ),
            (
                "bad gpu type",
                lambda raw: machines(raw)["gpu4"]["gpu"].update(type="RTX 4090"),
                "machines.gpu4.gpu.type must be",
            ),
            (
                "bad machine name",
                lambda raw: machines(raw).update({"9node": machines(raw).pop("gpu2")}),
                "machines.9node: the name must",
            ),
            (
                "not ipv4",
                lambda raw: machines(raw)["gpu4"].update(address="gpu4.lab"),
                "machines.gpu4.address must be an IPv4 address",
            ),
            (
                "duplicate address",
                lambda raw: machines(raw)["gpu2"].update(address="192.168.104.11"),
                "machines.gpu2.address 192.168.104.11 is already used by machines.gpu4",
            ),
            (
                "duplicate alias",
                lambda raw: machines(raw)["gpu2"].update(aliases=["gpu4"]),
                "machines.gpu2.aliases: gpu4 is already used by machines.gpu4",
            ),
            (
                "unknown role",
                lambda raw: machines(raw)["gpu4"].update(roles=["worker"]),
                "machines.gpu4.roles: unknown role worker",
            ),
            (
                "no roles",
                lambda raw: machines(raw)["gpu4"].update(roles=[]),
                "machines.gpu4.roles must be a non-empty list",
            ),
            (
                "two front",
                lambda raw: machines(raw)["store"].update(roles=["front"]),
                "exactly one machine must have the front role, found 2",
            ),
            (
                "no home",
                lambda raw: machines(raw)["front"].update(roles=["front"]),
                "exactly one machine must have the home role, found 0",
            ),
            (
                "no compute",
                lambda raw: [machines(raw).pop(n) for n in ("gpu4", "gpu2", "cpu1", "gpu4i")],
                "at least one machine must have the compute role",
            ),
            (
                "front runs jobs",
                lambda raw: machines(raw)["front"].update(roles=["front", "home", "compute"]),
                "machines.front: the front role cannot be combined with compute",
            ),
            (
                "backup on home",
                lambda raw: machines(raw)["front"].update(roles=["front", "home", "backup"]),
                "machines.front: the backup role cannot be on the home machine",
            ),
            (
                "compute field on storage",
                lambda raw: machines(raw)["store"].update(memory_mb=4096),
                "machines.store.memory_mb is only for compute machines",
            ),
            (
                "home field without role",
                lambda raw: machines(raw)["gpu4"].update(home={"device": "/dev/vdb"}),
                "machines.gpu4.home is only for the home machine",
            ),
            (
                "home device not a device",
                lambda raw: machines(raw)["front"]["home"].update(device="/data"),
                "machines.front.home.device must be a device path under /dev/",
            ),
            (
                "backup path relative",
                lambda raw: machines(raw)["store"]["backup"].update(path="backup"),
                "machines.store.backup.path must be an absolute path",
            ),
            (
                "scratch both kinds",
                lambda raw: machines(raw)["gpu4"]["scratch"].update(image_gb=10),
                "machines.gpu4.scratch must have exactly one of device or image_gb",
            ),
            ("scratch missing", lambda raw: machines(raw)["gpu4"].pop("scratch"), "machines.gpu4.scratch is required"),
            (
                "scratch device",
                lambda raw: machines(raw)["gpu4"].update(scratch={"device": "/scratch"}),
                "machines.gpu4.scratch.device must be a device path under /dev/",
            ),
            (
                "unknown partition",
                lambda raw: machines(raw)["gpu4"].update(partitions=["batch"]),
                "machines.gpu4.partitions: unknown partition batch",
            ),
            (
                "duplicate partition",
                lambda raw: machines(raw)["gpu4"].update(partitions=["main", "main"]),
                "machines.gpu4.partitions has duplicates",
            ),
            (
                "empty partition",
                lambda raw: raw["partitions"].update(debug={"max_time": "01:00:00"}),
                "partitions.debug has no compute machines",
            ),
            (
                "two defaults",
                lambda raw: raw["partitions"]["interactive"].update(default=True),
                "exactly one partition must have default: true, found 2",
            ),
            (
                "bad max_time",
                lambda raw: raw["partitions"]["main"].update(max_time="24h"),
                "partitions.main.max_time must be a Slurm time",
            ),
            (
                "bad partition name",
                lambda raw: raw["partitions"].update({"Main Queue": {"max_time": "01:00:00"}}),
                "partitions.Main Queue: the name must",
            ),
            (
                "bad gpu limit",
                lambda raw: raw["partitions"]["interactive"].update(max_gpus_per_user=0),
                "partitions.interactive.max_gpus_per_user must be a positive integer or unlimited",
            ),
            (
                "admin not user",
                lambda raw: raw["cluster"].update(admins=["carol"]),
                "cluster.admins: carol is not in users",
            ),
            ("bad cluster name", lambda raw: raw["cluster"].update(name="Lab Cluster"), "cluster.name must"),
            (
                "own https without cert",
                lambda raw: raw["cluster"]["website"].update(https="own"),
                "cluster.website.certificate is required when https is own",
            ),
            (
                "plain http",
                lambda raw: raw["cluster"]["website"].update(https="none"),
                "cluster.website.https must be letsencrypt or own",
            ),
            (
                "duplicate uid",
                lambda raw: raw["users"][1].update(uid=2000),
                "users[1].uid 2000 is already used by alice",
            ),
            ("duplicate user", lambda raw: raw["users"][1].update(name="alice"), "users[1].name alice is already used"),
            ("system uid", lambda raw: raw["users"][0].update(uid=999), "users[0].uid must be at least 1000"),
            (
                "no ssh keys",
                lambda raw: raw["users"][0].update(ssh_keys=[]),
                "users[0].ssh_keys must be a non-empty list",
            ),
            ("bad user name", lambda raw: raw["users"][0].update(name="Alice"), "users[0].name must"),
            (
                "bad policy value",
                lambda raw: raw["policy"].update(default_cpus_per_gpu="four"),
                "policy.default_cpus_per_gpu must be a positive integer",
            ),
            (
                "bad half life",
                lambda raw: raw["policy"].update(fairshare_half_life="7 days"),
                "policy.fairshare_half_life must be a Slurm time",
            ),
            (
                "quota order",
                lambda raw: raw["home"].update(quota_soft_gb=500),
                "home.quota_soft_gb must not be larger than home.quota_hard_gb",
            ),
            (
                "backup unknown machine",
                lambda raw: raw["backup"].update(to="nas"),
                "backup.to must be a machine with the backup role or user@host:/path",
            ),
            (
                "backup to non-backup machine",
                lambda raw: raw["backup"].update(to="gpu4"),
                "backup.to must be a machine with the backup role or user@host:/path",
            ),
            (
                "backup machine unused",
                lambda raw: raw.pop("backup"),
                "machines.store has the backup role but backup.to does not use it",
            ),
            ("bad backup time", lambda raw: raw["backup"].update(time="3am"), "backup.time must be HH:MM"),
            (
                "auto deploy without repo",
                lambda raw: raw["auto_deploy"].update(enabled=True),
                "auto_deploy.repository is required when auto_deploy.enabled is true",
            ),
            ("slack not bool", lambda raw: raw["alerts"].update(slack="yes"), "alerts.slack must be true or false"),
        ]
        for label, change, expected in cases:
            with self.subTest(label):
                self.assert_rejected(change, expected)


class RobustInputTest(unittest.TestCase):
    """Odd files and odd values give a message naming the problem, never a crash or a silent pass."""

    def run_text(self, text: str) -> subprocess.CompletedProcess[str]:
        """Write `text` to a temporary cluster.yml and validate it with the command."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cluster.yml"
            path.write_text(text)
            return run_validate(path)

    def test_missing_file(self) -> None:
        result = run_validate(ROOT / "no-such-cluster.yml")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no-such-cluster.yml: file not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_file_names_the_sections(self) -> None:
        for text in ("", "null\n", "- a\n"):
            with self.subTest(text=text):
                result = self.run_text(text)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "the file must be a mapping with the sections cluster, machines, users, partitions", result.stderr
                )

    def test_duplicate_keys_are_rejected(self) -> None:
        text = EXAMPLE.read_text().replace(
            "  main:\n    default: true", "  main:\n    max_time: '01:00:00'\n  main:\n    default: true"
        )
        result = self.run_text(text)
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate key main", result.stderr)

    def test_wrong_types_do_not_crash(self) -> None:
        cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
            (lambda raw: raw["cluster"].update(admins=[["alice"]]), "cluster.admins[0] must be a user name"),
            (lambda raw: raw["users"][0].update(name=["a"]), "users[0].name must"),
            (
                lambda raw: raw["machines"]["gpu4"].update(partitions=[["main"]]),
                "machines.gpu4.partitions[0] must be a partition name",
            ),
            (
                lambda raw: raw["machines"]["gpu4"].update(aliases=[5]),
                "machines.gpu4.aliases must be a list of strings",
            ),
        ]
        for change, expected in cases:
            with self.subTest(expected):
                errors = mutate(change)
                self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")

    def test_unsafe_values_are_rejected(self) -> None:
        machines = lambda raw: raw["machines"]
        cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
            (
                lambda raw: raw["users"][0].update(ssh_keys=["garbage"]),
                "users[0].ssh_keys[0] must be one public key line",
            ),
            (
                lambda raw: raw["users"][0].update(ssh_keys=["ssh-ed25519 AAAA a\nssh-rsa BBBB b"]),
                "users[0].ssh_keys[0] must be one public key line",
            ),
            (lambda raw: raw["users"][0].update(name="root"), "users[0].name root is a system account"),
            (lambda raw: raw["users"][0].update(uid=65534), "users[0].uid must be at most 60000"),
            (
                lambda raw: machines(raw).update(GPU4=machines(raw).pop("gpu2")),
                "machines.GPU4.aliases: GPU4 is already used by machines.gpu4",
            ),
            (
                lambda raw: machines(raw).update(localhost=machines(raw).pop("gpu2")),
                "machines.localhost.aliases: localhost is reserved",
            ),
            (
                lambda raw: machines(raw)["gpu4"].update(address="127.0.0.1"),
                "machines.gpu4.address must be the machine's network address",
            ),
            (
                lambda raw: machines(raw)["gpu4"].update(address="0.0.0.0"),
                "machines.gpu4.address must be the machine's network address",
            ),
            (
                lambda raw: machines(raw)["store"]["backup"].update(path="/"),
                "machines.store.backup.path must be an absolute path",
            ),
            (
                lambda raw: machines(raw)["store"]["backup"].update(path="/srv/../etc"),
                "machines.store.backup.path must be an absolute path",
            ),
            (
                lambda raw: raw["partitions"]["main"].update(max_time="99:99:99"),
                "partitions.main.max_time must be a Slurm time",
            ),
            (lambda raw: raw["partitions"]["main"].update(max_time=86400), 'in quotes, like "24:00:00"'),
            (
                lambda raw: raw["cluster"]["website"].update(https="own", certificate="", certificate_key="k.pem"),
                "cluster.website.certificate is required when https is own",
            ),
            (
                lambda raw: raw["cluster"]["website"].update(certificate="/c.pem"),
                "cluster.website.certificate is only used when https is own",
            ),
            (lambda raw: raw["cluster"]["website"].update(logo=5), "cluster.website.logo must be a file path"),
            (
                lambda raw: raw["cluster"]["website"].update(login_address=5),
                "cluster.website.login_address must be a host name",
            ),
            (
                lambda raw: raw["backup"].update(to="lab@backup.example.org:/"),
                "backup.to must be a machine with the backup role or user@host:/path",
            ),
            (
                lambda raw: raw["backup"].update(to="lab@backup.example.org:/srv/../etc"),
                "backup.to must be a machine with the backup role or user@host:/path",
            ),
            (lambda raw: raw["backup"].update(exclude=["$HOME/x"]), "backup.exclude[0] must be a plain rsync pattern"),
            (lambda raw: raw["backup"].update(exclude=["50%"]), "backup.exclude[0] must be a plain rsync pattern"),
            (
                lambda raw: raw["users"].append(
                    {"name": "nanohpc-backup", "uid": 2050, "ssh_keys": raw["users"][0]["ssh_keys"]}
                ),
                "users[2].name nanohpc-backup is reserved for nanoHPC",
            ),
            (lambda raw: raw["cluster"]["website"].update(path="cluster"), "cluster.website.path must be a URL path"),
            (lambda raw: raw["cluster"]["website"].update(path="/a b/"), "cluster.website.path must be a URL path"),
            (lambda raw: raw["cluster"]["website"].update(path="/x/../"), "cluster.website.path must be a URL path"),
            (lambda raw: raw["cluster"]["website"].update(allow="10.0.0.0/8"), "cluster.website.allow must be a list"),
            (
                lambda raw: raw["cluster"]["website"].update(allow=["10.0.0.0/33"]),
                "cluster.website.allow[0] must be a network",
            ),
            (
                lambda raw: raw["cluster"]["website"].update(forwarded_by="lab server"),
                "cluster.website.forwarded_by must be an IP address",
            ),
            (
                lambda raw: raw["cluster"]["website"].update(build="docker"),
                "cluster.website.build must be package or front",
            ),
        ]
        for change, expected in cases:
            with self.subTest(expected):
                errors = mutate(change)
                self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")

    def test_valid_variants_are_accepted(self) -> None:
        cases: list[Callable[[dict[str, Any]], None]] = [
            lambda raw: raw["machines"]["gpu4"]["gpu"].update(type="a100-80gb"),
            lambda raw: raw["machines"]["front"].update(home=None),
            lambda raw: raw["cluster"]["website"].update(
                https="own", certificate="/etc/ssl/c.pem", certificate_key="/etc/ssl/k.pem"
            ),
            lambda raw: raw["partitions"]["main"].update(max_time="7-12:00:00"),
            lambda raw: raw["cluster"]["website"].update(path="/"),
            lambda raw: raw["cluster"]["website"].update(path="/lab/cluster"),
            lambda raw: raw["cluster"]["website"].update(
                allow=["10.0.0.0/8", "192.168.1.7", "2001:db8::/32"], forwarded_by="203.0.113.5", build="front"
            ),
        ]
        for index, change in enumerate(cases):
            with self.subTest(index):
                self.assertEqual(mutate(change), [])

    def test_website_hostname_and_login_address(self) -> None:
        """The hostname is used lowercase (as certificates store it); a null login address means the hostname."""
        raw = example()
        raw["cluster"]["website"].update(hostname="Cluster.Example.ORG", login_address=None)
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["cluster"]["website"]["hostname"], "cluster.example.org")
        self.assertEqual(config["cluster"]["website"]["login_address"], "cluster.example.org")
        raw = example()
        raw["cluster"]["website"].update(hostname="my_cluster.example.org")
        _, errors = check_config(raw)
        self.assertIn("cluster.website.hostname must be a DNS name (letters, digits, - and .)", errors[0])

    def test_storage_roles_do_not_run_jobs(self) -> None:
        def home_on_compute(raw: dict[str, Any]) -> None:
            raw["machines"]["front"]["roles"] = ["front"]
            del raw["machines"]["front"]["home"]
            raw["machines"]["gpu4"]["roles"] = ["compute", "home"]

        def backup_on_compute(raw: dict[str, Any]) -> None:
            del raw["machines"]["store"]
            raw["machines"]["gpu4"]["roles"] = ["compute", "backup"]
            raw["machines"]["gpu4"]["backup"] = {"path": "/srv/backup"}
            raw["backup"]["to"] = "gpu4"

        for change, expected in [
            (home_on_compute, "machines.gpu4: the home role cannot be combined with compute"),
            (backup_on_compute, "machines.gpu4: the backup role cannot be combined with compute"),
        ]:
            with self.subTest(expected):
                errors = mutate(change)
                self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")

    def test_limits_no_machine_can_meet_are_rejected(self) -> None:
        # The example has 10 GPUs: gpu4 (4, main+interactive), gpu2 (2, main), gpu4i (4, interactive).
        # Compute machines have 4096 MB; GPU machines have 8 CPUs.
        cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
            (
                lambda raw: raw["partitions"]["interactive"].update(max_gpus_per_user=9),
                "partitions.interactive.max_gpus_per_user 9 is more than the 8 GPUs in the partition",
            ),
            (
                lambda raw: raw["policy"].update(max_gpus_per_user=11),
                "policy.max_gpus_per_user 11 is more than the 10 GPUs in the cluster",
            ),
            (
                lambda raw: raw["policy"].update(default_memory_mb_per_cpu=5000),
                "policy.default_memory_mb_per_cpu 5000 is more than the memory of any compute machine (4096)",
            ),
            (
                lambda raw: raw["policy"].update(default_cpus_per_gpu=9),
                "policy.default_cpus_per_gpu 9 is more than the CPUs of any GPU machine (8)",
            ),
        ]
        for change, expected in cases:
            with self.subTest(expected):
                errors = mutate(change)
                self.assertTrue(any(expected in error for error in errors), f"expected {expected!r} in {errors}")
        limits_at_capacity = mutate(
            lambda raw: (
                raw["partitions"]["interactive"].update(max_gpus_per_user=8),
                raw["policy"].update(max_gpus_per_user=10, default_memory_mb_per_cpu=4096, default_cpus_per_gpu=8),
            )
        )
        self.assertEqual(limits_at_capacity, [])

    def test_bad_roles_give_one_error(self) -> None:
        errors = mutate(lambda raw: raw["machines"]["gpu4"].update(roles="compute"))
        self.assertIn("machines.gpu4.roles must be a non-empty list", errors)
        self.assertFalse(any("is only for compute machines" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
