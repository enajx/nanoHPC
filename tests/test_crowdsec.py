"""CrowdSec (opt-in, both modes): its cluster.yml section, what the roles get, the numbers script, the blocking
behind an undo, and the Security page's dashboard. Never contacts a machine: the numbers script runs through its
command line with a FAKE cscli and a FAKE iptables (they print fixed JSON and counters)."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jinja2
import yaml

from nanohpc import clusterfile
from nanohpc.config import check_config, load_config
from nanohpc.deploy import prepare, prepare_monitor, site_data

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cluster.yml"
MONITOR_EXAMPLE = ROOT / "examples" / "monitor.yml"
ANSIBLE = ROOT / "src/nanohpc/ansible"
ROLE = ANSIBLE / "roles/crowdsec"
FILES = ROOT / "src/nanohpc/files"
NOW = datetime.now(UTC)


def example() -> dict[str, Any]:
    """The Slurm-mode example as a mapping."""
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text()))


def monitor_example() -> dict[str, Any]:
    """The monitor-mode example as a mapping."""
    return copy.deepcopy(yaml.safe_load(MONITOR_EXAMPLE.read_text()))


class CrowdsecConfigTest(unittest.TestCase):
    """Off unless cluster.yml turns it on, in both modes, with a list of networks that are never blocked."""

    def test_off_by_default_in_both_modes(self) -> None:
        for raw in (example(), monitor_example()):
            raw.pop("crowdsec", None)
            config, errors = check_config(raw)
            self.assertEqual(errors, [])
            self.assertEqual(config["crowdsec"], {"enabled": False, "never_block": []})

    def test_on_with_never_block_networks_in_both_modes(self) -> None:
        for raw in (example(), monitor_example()):
            raw["crowdsec"] = {"enabled": True, "never_block": ["192.0.2.0/24", "198.51.100.7", "2001:db8::/32"]}
            config, errors = check_config(raw)
            self.assertEqual(errors, [])
            self.assertEqual(config["crowdsec"]["never_block"], ["192.0.2.0/24", "198.51.100.7", "2001:db8::/32"])

    def test_bad_values_are_named(self) -> None:
        for value, field in (
            ({"enabled": "yes"}, "crowdsec.enabled"),
            ({"never_block": "10.0.0.0/8"}, "crowdsec.never_block"),
            ({"never_block": ["not a network"]}, "crowdsec.never_block[0]"),
            ({"never_block": ["10.0.0.0/33"]}, "crowdsec.never_block[0]"),
            ({"blocking": False}, "crowdsec.blocking"),
        ):
            for raw in (example(), monitor_example()):
                with self.subTest(value=value, mode=raw["cluster"].get("mode", "slurm")):
                    raw["crowdsec"] = value
                    _, errors = check_config(raw)
                    self.assertTrue(any(error.startswith(field) for error in errors), errors)

    def test_the_examples_show_the_section_and_editing_keeps_it(self) -> None:
        self.assertEqual(yaml.safe_load(EXAMPLE.read_text())["crowdsec"], {"enabled": False, "never_block": []})
        self.assertIn("crowdsec:", MONITOR_EXAMPLE.read_text())
        file = clusterfile.load(EXAMPLE)
        file.set_value(["scratch", "cleanup_days"], 10)
        self.assertIn("crowdsec:", file.as_text())


class CrowdsecVariablesTest(unittest.TestCase):
    """What the roles and the website get from cluster.yml."""

    def slurm_variables(self, crowdsec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = example()
        raw["crowdsec"] = crowdsec
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "cluster.yml").write_text(yaml.safe_dump(raw))
            config, errors = load_config(work / "cluster.yml", True, False)
            self.assertEqual(errors, [])
            prepare(
                config, {name: name for name in config["machines"]}, Path("/x/ssh_config"), True, [], False, None, work
            )
            site = json.loads((work / "files" / "site.json").read_text())
            return json.loads((work / "vars.json").read_text())["nanohpc"]["crowdsec"], site

    def test_slurm_mode_never_blocks_the_machines_and_the_listed_networks(self) -> None:
        variables, site = self.slurm_variables({"enabled": True, "never_block": ["192.0.2.0/24"]})
        addresses = sorted({machine["address"] for machine in example()["machines"].values()})
        self.assertTrue(variables["enabled"])
        self.assertEqual(variables["never_block"], [*addresses, "192.0.2.0/24"])
        # Fresh logins after blocking is on: root (nanoHPC sets up root login in Slurm mode) and the deploy's account.
        self.assertTrue(variables["root_login"])
        self.assertEqual(variables["ssh_config"], "/x/ssh_config")
        self.assertTrue(site["security"])

    def test_off_reaches_the_roles_and_hides_the_page(self) -> None:
        variables, site = self.slurm_variables({"enabled": False})
        self.assertFalse(variables["enabled"])
        self.assertFalse(site["security"])

    def test_monitor_mode(self) -> None:
        raw = monitor_example()
        raw["crowdsec"] = {"enabled": True, "never_block": ["203.0.113.0/24"]}
        config, errors = check_config(raw)
        self.assertEqual(errors, [])
        config["secrets"] = {}
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            prepare_monitor(config, None, False, {}, {}, work, None)
            variables = json.loads((work / "vars.json").read_text())["nanohpc"]["crowdsec"]
            site = json.loads((work / "files" / "site.json").read_text())
        self.assertEqual(variables["never_block"], ["192.168.1.10", "192.168.1.11", "192.168.1.12", "203.0.113.0/24"])
        # Monitor mode does not set up root logins, so only the deploy's own account is checked.
        self.assertFalse(variables["root_login"])
        self.assertIsNone(variables["ssh_config"])
        self.assertTrue(site["security"])
        self.assertEqual(site_data(config)["security"], True)


class NeverBlockRuleTest(unittest.TestCase):
    """The machines and the listed networks never count toward a block; failed logins by cluster users count like
    any other (no rule for user names)."""

    def test_the_rule_lists_addresses_and_networks(self) -> None:
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
        template = environment.from_string((ROLE / "templates/never-block.yaml.j2").read_text())
        rule = yaml.safe_load(
            template.render(nanohpc={"crowdsec": {"never_block": ["192.168.1.10", "192.0.2.0/24", "2001:db8::/32"]}})
        )
        self.assertEqual(rule["whitelist"]["ip"], ["192.168.1.10"])
        self.assertEqual(rule["whitelist"]["cidr"], ["192.0.2.0/24", "2001:db8::/32"])
        self.assertNotIn("expression", rule["whitelist"])


def alert(number: int, scenario: str, address: str, country: str, network: str, age: timedelta, banned: bool) -> dict:
    """An alert as `cscli alerts list -o json` prints it (the fields the script reads)."""
    return {
        "id": number,
        "decisions": [{"type": "ban", "origin": "crowdsec"}] if banned else [],
        "scenario": scenario,
        "scenario_hash": "f00d",
        "created_at": (NOW - age).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"scope": "Ip", "value": address, "ip": address, "cn": country, "as_name": network},
    }


class NumbersTest(unittest.TestCase):
    """cluster-security-metrics turns CrowdSec's alerts into numbers for the machine's exporter."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="crowdsec-"))
        self.addCleanup(shutil.rmtree, self.directory)
        self.alerts = self.directory / "alerts.json"
        self.decisions = self.directory / "decisions.json"
        self.fake = self.directory / "cscli"
        self.fake.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"print(open({str(self.alerts)!r} if sys.argv[1] == 'alerts' else {str(self.decisions)!r}).read())\n"
        )
        self.fake.chmod(0o755)
        # FAKE iptables: CrowdSec's chain with two rules that dropped 120 and 30 packets.
        self.iptables = self.directory / "iptables"
        self.iptables.write_text(
            "#!/bin/sh\necho 'Chain CROWDSEC_CHAIN (1 references)'\n"
            "echo '    pkts      bytes target prot opt in out source destination'\n"
            "echo '     120     7200 DROP all -- * * 0.0.0.0/0 0.0.0.0/0 match-set crowdsec-blacklists-0 src'\n"
            "echo '      30     1800 DROP all -- * * 0.0.0.0/0 0.0.0.0/0 match-set crowdsec-blacklists-1 src'\n"
        )
        self.iptables.chmod(0o755)
        # CrowdSec's own metrics, as a file (FAKE): 8 failed logins reached its ssh-bf rule.
        metrics = self.directory / "metrics.txt"
        metrics.write_text(
            'cs_bucket_poured_total{name="crowdsecurity/ssh-bf",source="/var/log/auth.log",type="file"} 8\n'
            'cs_bucket_poured_total{name="crowdsecurity/ssh-slow-bf",source="/var/log/auth.log",type="file"} 8\n'
        )
        self.metrics_url = metrics.as_uri()

    def run_script(self) -> str:
        output = self.directory / "security.prom"
        subprocess.run(
            [
                sys.executable,
                str(FILES / "cluster-security-metrics"),
                "--cscli",
                str(self.fake),
                "--iptables",
                str(self.iptables),
                "--metrics",
                self.metrics_url,
                "--state",
                str(self.directory / "state.json"),
                "--output",
                str(output),
            ],
            check=True,
        )
        return output.read_text()

    def test_each_attack_is_counted_once_by_kind_country_network_and_address(self) -> None:
        self.alerts.write_text(
            json.dumps(
                [
                    alert(1, "crowdsecurity/ssh-bf", "203.0.113.5", "CN", 'Big "Cloud"', timedelta(hours=1), True),
                    alert(2, "crowdsecurity/ssh-bf", "203.0.113.5", "CN", 'Big "Cloud"', timedelta(days=2), True),
                    alert(3, "crowdsecurity/ssh-slow-bf", "198.51.100.9", "NL", "Hoster", timedelta(days=10), True),
                    # A detection that only reports: an attack, not a block.
                    alert(
                        6, "crowdsecurity/ssh-time-based-bf", "198.51.100.20", "BG", "Gwy", timedelta(hours=2), False
                    ),
                    # A manual block, and the blocking test's block (its reason has a slash): not attacks.
                    {
                        "id": 4,
                        "scenario": "manual 'ban' from 'localhost'",
                        "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": {"scope": "Ip", "value": "192.0.2.1"},
                    },
                    {
                        "id": 5,
                        "scenario": "blocking test (nanoHPC/crowdsec)",
                        "scenario_hash": "",
                        "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": {"scope": "Ip", "value": "192.0.2.2"},
                    },
                ]
            )
        )
        self.decisions.write_text(json.dumps([{"decisions": [{"type": "ban"}, {"type": "ban"}]}]))
        text = self.run_script()
        self.assertIn('cluster_security_attacks_total{kind="ssh-bf",country="CN"} 2\n', text)
        self.assertIn('cluster_security_attacks_total{kind="ssh-slow-bf",country="NL"} 1\n', text)
        self.assertNotIn("192.0.2.1", text)
        self.assertNotIn("192.0.2.2", text)
        # The last 7 days only: the 10-day-old attack is in the total, not on the map.
        self.assertIn('cluster_security_attacks_7d{country="CN"} 2\n', text)
        self.assertIn('cluster_security_attacks_7d{country="BG"} 1\n', text)
        self.assertNotIn('cluster_security_attacks_7d{country="NL"}', text)
        self.assertIn('cluster_security_networks_7d{network="Big \\"Cloud\\"",country="CN"} 2\n', text)
        self.assertIn(
            'cluster_security_addresses_7d{address="203.0.113.5",country="CN",network="Big \\"Cloud\\""} 2\n', text
        )
        self.assertIn("cluster_security_blocked 2\n", text)
        self.assertIn("cluster_security_attacks_24h 2\n", text)
        self.assertIn("cluster_security_blocks_total 3\n", text)
        self.assertIn("cluster_security_blocks_24h 1\n", text)
        self.assertIn("cluster_security_blocks_7d 2\n", text)
        self.assertIn("cluster_security_dropped_packets_total 150\n", text)
        self.assertIn("cluster_security_refused_logins_total 8\n", text)
        self.assertIn("cluster_security_updated_seconds ", text)
        # A second run with the same alerts counts nothing again.
        again = self.run_script()
        self.assertIn('cluster_security_attacks_total{kind="ssh-bf",country="CN"} 2\n', again)
        self.assertIn("cluster_security_blocks_total 3\n", again)

    def test_totals_stay_after_crowdsec_drops_old_alerts(self) -> None:
        self.alerts.write_text(
            json.dumps([alert(7, "crowdsecurity/ssh-bf", "203.0.113.5", "CN", "X", timedelta(hours=1), True)])
        )
        self.decisions.write_text("null")
        self.run_script()
        self.alerts.write_text("null")
        text = self.run_script()
        self.assertIn('cluster_security_attacks_total{kind="ssh-bf",country="CN"} 1\n', text)
        self.assertIn("cluster_security_blocked 0\n", text)

    def test_no_crowdsec_chain_is_zero_dropped(self) -> None:
        self.iptables.write_text("#!/bin/sh\necho 'iptables: No chain/target/match by that name.' >&2\nexit 1\n")
        self.alerts.write_text("null")
        self.decisions.write_text("null")
        self.assertIn("cluster_security_dropped_packets_total 0\n", self.run_script())


def tasks(path: Path) -> list[dict[str, Any]]:
    """The tasks of a role's task file."""
    return yaml.safe_load(path.read_text())


class DetectionRoleTest(unittest.TestCase):
    """Only the SSH log and the SSH detections; pinned, checksummed packages; nothing sent to CrowdSec."""

    def test_only_the_ssh_log_and_the_ssh_detections(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertEqual(
            defaults["crowdsec_collections"],
            ["crowdsecurity/linux", "crowdsecurity/sshd", "crowdsecurity/whitelist-good-actors"],
        )
        named = {task["name"]: task for task in tasks(ROLE / "tasks/main.yml")}
        source = yaml.safe_load(named["Read only the SSH log"]["ansible.builtin.copy"]["content"])
        self.assertEqual(source["filenames"], ["/var/log/auth.log"])
        self.assertIn("difference(crowdsec_collections)", named["Remove every other detection"]["loop"])
        self.assertEqual(
            named["Find the log sources the package's setup made"]["ansible.builtin.find"]["patterns"], "setup.*.yaml"
        )

    def test_pinned_checksummed_packages_for_both_cpu_types(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        for name in ("crowdsec_sha256", "crowdsec_bouncer_sha256"):
            self.assertEqual(set(defaults[name]), {"amd64", "arm64"})
            self.assertTrue(all(len(value) == 64 for value in defaults[name].values()))
        text = (ROLE / "tasks/main.yml").read_text() + (ROLE / "tasks/blocking.yml").read_text()
        self.assertEqual(text.count("checksum:"), 2)

    def test_nothing_is_sent_to_the_central_service(self) -> None:
        named = {task["name"]: task for task in tasks(ROLE / "tasks/main.yml")}
        questions = named["Never register with CrowdSec's central service when the package installs"]["loop"]
        self.assertIn({"question": "crowdsec/capi", "value": "false"}, questions)
        self.assertIn("Turn the online client off", named)
        self.assertIn("Require the online client off", named)
        self.assertNotIn("capi register", (ROLE / "tasks/main.yml").read_text())

    def test_a_dry_run_on_a_new_machine_downloads_nothing(self) -> None:
        for path in (ROLE / "tasks/main.yml", ROLE / "tasks/blocking.yml"):
            for task in tasks(path):
                for inner in [task, *task.get("block", [])]:
                    if "ansible.builtin.get_url" in inner:
                        self.assertIn("crowdsec_new_in_dry_run", str(inner.get("when")), (path, inner["name"]))


class BlockingTest(unittest.TestCase):
    """Blocking behind a 15-minute undo, cancelled only after a test block works, a never-block address is refused,
    and fresh logins work; one machine at a time."""

    def test_the_undo_is_set_before_and_cancelled_after_every_check(self) -> None:
        block = tasks(ROLE / "tasks/blocking.yml")[-1]
        names = [task["name"] for task in block["block"]]
        undo = names.index("Set the automatic undo")
        cancel = names.index("Cancel the automatic undo")
        for step in (
            "Install the firewall bouncer (it registers with the local CrowdSec and starts)",
            "Require the test address in the firewall's block list",
            "Require the test address gone from the block list",
            "Require a never-block address to be refused",
            "Check a fresh login as the deploy's account",
            "Check a fresh root login",
        ):
            self.assertLess(undo, names.index(step), step)
            self.assertLess(names.index(step), cancel, step)
        self.assertIn("--on-active=15min", str(block["block"][undo]))
        self.assertIn("nanohpc-crowdsec-undo", str(block["block"][undo]))
        self.assertEqual(block["rescue"][0]["ansible.builtin.command"], "systemctl start nanohpc-crowdsec-undo.service")
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertEqual(
            defaults["crowdsec_test_address"], "203.0.113.77", "the test block uses a documentation address"
        )
        self.assertIn("crowdsec_test_address", str(block["block"]))
        undo_script = (FILES / "nanohpc-crowdsec-undo").read_text()
        self.assertIn("systemctl stop crowdsec-firewall-bouncer", undo_script)
        self.assertIn("iptables -D INPUT -j CROWDSEC_CHAIN", undo_script)

    def test_an_earlier_undo_stops_the_run(self) -> None:
        block = tasks(ROLE / "tasks/blocking.yml")[-1]
        names = [task["name"] for task in block["block"]]
        self.assertLess(names.index("Refuse while an earlier undo is still set"), names.index("Set the automatic undo"))

    def test_both_modes_turn_blocking_on_one_machine_at_a_time(self) -> None:
        for playbook in ("site.yml", "monitor.yml", "partial.yml"):
            plays = yaml.safe_load((ANSIBLE / playbook).read_text())
            blocking = [play for play in plays if "blocking" in json.dumps(play.get("tasks", []))]
            self.assertEqual(len(blocking), 1, playbook)
            self.assertEqual(blocking[0]["serial"], 1, playbook)
            detection = [play for play in plays if "'main' if nanohpc.crowdsec.enabled" in json.dumps(play)]
            self.assertEqual(len(detection), 1, playbook)
            # --only node: the new machine, then the others learn its address (never blocked).
            self.assertEqual(detection[0]["hosts"], "only_node" if playbook == "partial.yml" else "all", playbook)


class SecurityDashboardTest(unittest.TestCase):
    """The Security page's Grafana dashboard: world map from Grafana's own files, the numbers, and a Machine menu."""

    def dashboard(self) -> dict[str, Any]:
        return json.loads((FILES / "grafana/security.json").read_text())

    def test_the_map_numbers_and_machine_menu(self) -> None:
        dashboard = self.dashboard()
        self.assertEqual(dashboard["uid"], "nanohpc-security")
        self.assertEqual(dashboard["title"], "[% nanohpc.cluster_name %]: Security")
        geomap = next(panel for panel in dashboard["panels"] if panel["type"] == "geomap")
        self.assertEqual(geomap["options"]["basemap"]["config"]["src"], "public/maps/countries.geojson")
        self.assertEqual(geomap["options"]["layers"][0]["location"]["gazetteer"], "public/gazetteer/countries.json")
        self.assertEqual(geomap["gridPos"]["w"], 12)
        self.assertNotIn("http", json.dumps(dashboard))
        machine = dashboard["templating"]["list"][0]
        self.assertEqual(
            (machine["name"], machine["includeAll"], machine["multi"], machine["current"]["value"]),
            ("machine", True, False, "$__all"),
        )
        targets = [target for panel in dashboard["panels"] for target in panel.get("targets", [])]
        self.assertTrue(all('machine=~"$machine"' in target["expr"] for target in targets))
        expressions = " ".join(target["expr"] for target in targets)
        for metric in (
            "cluster_security_refused_logins_total",
            "cluster_security_blocks_24h",
            "cluster_security_blocks_7d",
            "cluster_security_blocks_total",
            "cluster_security_blocked",
            "cluster_security_dropped_packets_total",
            "cluster_security_attacks_7d",
            "cluster_security_networks_7d",
            "cluster_security_addresses_7d",
        ):
            self.assertIn(metric, expressions)
        titles = [panel["title"] for panel in dashboard["panels"]]
        for title in ("SSH refused", "CrowdSec blocks", "Top countries, last 7 days"):
            self.assertIn(title, titles)
        self.assertLessEqual(max(panel["gridPos"]["y"] + panel["gridPos"]["h"] for panel in dashboard["panels"]), 25)

    def test_installed_and_public_in_both_modes(self) -> None:
        grafana = (ANSIBLE / "roles/grafana/tasks/main.yml").read_text()
        self.assertIn("'security.json'", grafana)
        self.assertIn("(3 if nanohpc.mode | default('slurm') == 'monitor' else 7)", grafana)
        nginx = (ANSIBLE / "roles/website/templates/website.conf.j2").read_text()
        self.assertEqual(nginx.count("|nanohpc-security"), 2)


if __name__ == "__main__":
    unittest.main()
