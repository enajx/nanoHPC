"""Check the deployed user-process scanner's service and playbook wiring."""

import shlex
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "src/nanohpc/ansible"
ROLE = ANSIBLE / "roles/user_check"


class UserCheckDeployTest(unittest.TestCase):
    """The scanner runs on Slurm nodes and publishes through node_exporter's textfile collector."""

    def render_service(self, machine: str) -> str:
        """Render the service as Ansible would for a front or compute machine."""
        template = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
            (ROLE / "templates/nanohpc-user-check.service.j2").read_text()
        )
        return template.render(
            inventory_hostname=machine,
            groups={"role_front": ["front"], "role_compute": ["gpu-1"]},
            nanohpc={
                "admins": ["admin1", "admin2"],
                "user_check": {
                    "allowed_tunnels": [
                        {"user": "alice", "program": "ngrok", "machines": ["gpu-1"]},
                        {"user": "alice", "program": "code tunnel", "machines": ["gpu-1"]},
                        {"user": "bob", "program": "cloudflared", "machines": ["front"]},
                    ],
                },
            },
        )

    def test_full_and_one_node_deploys_install_on_every_machine_after_metrics(self) -> None:
        """The metrics folder must exist before the scanner first writes to it."""
        site = yaml.safe_load((ANSIBLE / "site.yml").read_text())
        partial = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        for plays, target in ((site, "all"), (partial, "only_node")):
            selected = plays if target == "all" else [play for play in plays if "node" in play.get("tags", [])]
            roles = [(play["hosts"], play.get("roles", [])) for play in selected]
            metrics_index = next(i for i, (_, names) in enumerate(roles) if "machine_metrics" in names)
            check_index = next(i for i, (_, names) in enumerate(roles) if "user_check" in names)
            self.assertLess(metrics_index, check_index)
            self.assertEqual(roles[check_index][0], target)

    def test_service_uses_per_machine_allow_entries_and_admins(self) -> None:
        """Only a host's approved tunnels reach its command line."""
        for machine, role, allowed, excluded in (
            ("front", "front", "bob:cloudflared", "alice:ngrok"),
            ("gpu-1", "compute", "alice:ngrok", "bob:cloudflared"),
            ("storage", "other", "", "alice:ngrok"),
        ):
            service = self.render_service(machine)
            command = next(
                line.removeprefix("ExecStart=") for line in service.splitlines() if line.startswith("ExecStart=")
            )
            args = shlex.split(command)
            self.assertEqual(args[0], "/usr/local/bin/cluster-user-check")
            for part in (
                "--root /",
                f"--machine {machine}",
                f"--role {role}",
                "--admin admin1",
                "--admin admin2",
                "--disk /",
                "--disk-percent 90",
                "--keep-minutes 15",
                "--output /var/lib/nanohpc/metrics-textfile/user-check.prom",
            ):
                self.assertIn(part, command)
            if allowed:
                self.assertIn(allowed, args)
                self.assertIn("--allow", args)
            if machine == "gpu-1":
                self.assertIn("alice:code tunnel", args)
            self.assertNotIn(excluded, command)
            self.assertIn("StateDirectory=nanohpc-user-check", service)
            self.assertIn("ReadWritePaths=/var/lib/nanohpc/metrics-textfile", service)

    def test_role_runs_check_once_before_starting_minute_timer(self) -> None:
        """A broken first run fails deployment instead of waiting for the timer."""
        tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        names = [task["name"] for task in tasks]
        self.assertLess(names.index("Run the user check once"), names.index("Start the user check timer"))
        timer = (ROLE / "files/nanohpc-user-check.timer").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)

    def test_users_partial_deploy_refreshes_admin_and_tunnel_rules(self) -> None:
        """Changing admins through --only users must update the running checker command."""
        plays = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        users = [play for play in plays if "users" in play.get("tags", [])]
        accounts = next(index for index, play in enumerate(users) if "accounts" in play.get("roles", []))
        checker = next(index for index, play in enumerate(users) if "user_check" in play.get("roles", []))
        self.assertLess(accounts, checker)
        self.assertEqual(users[checker]["hosts"], "all")


if __name__ == "__main__":
    unittest.main()
