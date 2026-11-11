"""Login limits: every user's login processes together get a CPU and memory cap on front and login nodes."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import jinja2
import yaml

from nanohpc import clusterfile
from nanohpc.config import check_config, load_config
from nanohpc.deploy import prepare

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cluster.yml"
ANSIBLE = ROOT / "src/nanohpc/ansible"
ROLE = ANSIBLE / "roles/login_limits"


def example() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text()))


class LoginLimitsConfigTest(unittest.TestCase):
    def test_defaults_when_the_section_or_a_key_is_missing(self) -> None:
        raw = example()
        raw.pop("login_limits", None)
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["login_limits"], {"cpu_cores": 4, "memory_gb": 8})
        raw["login_limits"] = {"memory_gb": 16}
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(config["login_limits"], {"cpu_cores": 4, "memory_gb": 16})

    def test_values_must_be_positive_integers_and_keys_known(self) -> None:
        for value, field in (
            ({"cpu_cores": 0}, "login_limits.cpu_cores"),
            ({"memory_gb": -1}, "login_limits.memory_gb"),
            ({"memory_gb": "8"}, "login_limits.memory_gb"),
            ({"cpu_cores": True}, "login_limits.cpu_cores"),
            ({"cpu": 2}, "login_limits.cpu"),
        ):
            with self.subTest(value=value):
                raw = example()
                raw["login_limits"] = value
                _, errors = check_config(raw)
                self.assertTrue(any(error.startswith(field) for error in errors), errors)

    def test_monitor_mode_refuses_the_section(self) -> None:
        raw = yaml.safe_load((ROOT / "examples" / "monitor.yml").read_text())
        raw["login_limits"] = {"cpu_cores": 2}
        _, errors = check_config(raw)
        self.assertTrue(any("login_limits" in error for error in errors), errors)

    def test_the_example_shows_the_section(self) -> None:
        self.assertIn("login_limits", yaml.safe_load(EXAMPLE.read_text()))

    def test_editing_the_file_keeps_the_section(self) -> None:
        file = clusterfile.load(EXAMPLE)
        file.set_value(["scratch", "cleanup_days"], 10)
        self.assertIn("login_limits:", file.as_text())


class LoginLimitsDeployTest(unittest.TestCase):
    def variables(self, limits: dict[str, int]) -> dict[str, Any]:
        raw = example()
        raw["login_limits"] = limits
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "cluster.yml").write_text(yaml.safe_dump(raw))
            config, errors = load_config(work / "cluster.yml", True, False)
            self.assertEqual(errors, [])
            hostnames = {name: name for name in config["machines"]}
            prepare(config, hostnames, None, True, [], False, None, work)
            return json.loads((work / "vars.json").read_text())["nanohpc"]["login_limits"]

    def test_variables_reach_ansible(self) -> None:
        variables = self.variables({"cpu_cores": 2, "memory_gb": 16})
        self.assertEqual(variables["cpu_quota_percent"], 200)
        self.assertEqual(variables["memory_high_mb"], 12288)
        self.assertEqual(variables["memory_max_gb"], 16)
        uids = {user["name"]: user["uid"] for user in example()["users"]}
        admins = example()["cluster"]["admins"]
        self.assertEqual(variables["exempt_uids"], [0, *[uids[name] for name in admins]])

    def render(self, template: str, **values: Any) -> str:
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        return environment.from_string((ROLE / "templates" / template).read_text()).render(**values)

    def test_every_user_slice_gets_the_cap(self) -> None:
        text = self.render(
            "limits.conf.j2",
            nanohpc={"login_limits": {"cpu_quota_percent": 400, "memory_high_mb": 6144, "memory_max_gb": 8}},
        )
        self.assertIn("[Slice]\n", text)
        self.assertIn("CPUQuota=400%\n", text)
        self.assertIn("MemoryHigh=6144M\n", text)
        self.assertIn("MemoryMax=8G\n", text)

    def test_exemption_clears_the_limits(self) -> None:
        text = self.render("exempt.conf.j2")
        lines = text.splitlines()
        for line in ("[Slice]", "CPUQuota=", "MemoryHigh=infinity", "MemoryMax=infinity"):
            self.assertIn(line, lines)

    def tasks(self) -> str:
        return (ROLE / "tasks/main.yml").read_text()

    def test_role_writes_both_drop_ins_checks_them_and_removes_old_exemptions(self) -> None:
        tasks = self.tasks()
        for text in (
            "/etc/systemd/system/user-.slice.d/50-nanohpc-login-limits.conf",
            "user-{{ item }}.slice.d/50-nanohpc-login-limits.conf",
            "daemon_reload: true",
            "systemctl show",
            "ansible.builtin.assert",
            "state: absent",
        ):
            self.assertIn(text, tasks)

    def removal_task(self) -> dict[str, Any]:
        tasks = yaml.safe_load(self.tasks())
        return next(task for task in tasks if task["name"].startswith("Remove exemptions"))

    def test_removal_selects_only_files_of_people_who_are_no_longer_administrators(self) -> None:
        """Run the removal task's loop expression with Ansible on sample file lists."""
        base = "/etc/systemd/system"
        name = "50-nanohpc-login-limits.conf"
        found = [
            f"{base}/user-.slice.d/{name}",
            f"{base}/user-0.slice.d/{name}",
            f"{base}/user-1001.slice.d/{name}",
            f"{base}/user-100.slice.d/{name}",  # not an administrator: 100 is not 1001
            f"{base}/user-10010.slice.d/{name}",  # not an administrator: 1001 is not 10010
            f"{base}/user-1002.slice.d/{name}",  # an old administrator
        ]
        play = [
            {
                "hosts": "localhost",
                "gather_facts": False,
                "vars": {
                    "nanohpc": {"login_limits": {"exempt_uids": [0, 1001]}},
                    "login_limits_files": {"files": [{"path": path} for path in found]},
                },
                "tasks": [
                    {
                        "ansible.builtin.copy": {
                            "dest": "{{ playbook_dir }}/selected.json",
                            "content": self.removal_task()["loop"],
                        }
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "play.yml").write_text(yaml.safe_dump(play))
            ansible = Path(sys.executable).parent / "ansible-playbook"
            result = subprocess.run([str(ansible), "play.yml"], cwd=folder, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            selected = [item["path"] for item in json.loads((Path(folder) / "selected.json").read_text())]
        self.assertEqual(selected, found[3:])

    def test_the_limits_are_reloaded_and_checked_on_every_real_run(self) -> None:
        """An interrupted run must not leave the next one failing: the reload does not depend on a change."""
        tasks = yaml.safe_load(self.tasks())
        reload = next(task for task in tasks if "ansible.builtin.systemd_service" in task)
        self.assertNotIn("changed", str(reload.get("when", "")))
        self.assertEqual(reload["when"], "not ansible_check_mode")

    def test_the_check_reads_every_exempt_slice_and_compares_the_configured_values(self) -> None:
        text = self.tasks()
        self.assertIn("exempt_uids", text.split("Read the limits in force")[1])
        self.assertIn("1073741824", text)
        self.assertIn("MemoryMax=", text)

    def test_plays_run_it_on_front_and_login_nodes_for_full_and_users_deploys(self) -> None:
        site = yaml.safe_load((ANSIBLE / "site.yml").read_text())
        partial = yaml.safe_load((ANSIBLE / "partial.yml").read_text())
        full = [play for play in site if "login_limits" in play.get("roles", [])]
        users = [
            play for play in partial if "login_limits" in play.get("roles", []) and "users" in play.get("tags", [])
        ]
        for plays in (full, users):
            self.assertEqual(len(plays), 1)
            self.assertEqual(plays[0]["hosts"], "role_front:role_login")


if __name__ == "__main__":
    unittest.main()
