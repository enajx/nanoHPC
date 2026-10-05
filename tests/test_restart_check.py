"""The restart check through the public CLI, with saved and live machine facts supplied by fake SSH."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nanohpc.restart_check import evaluate
from tests.test_probe import write_command  # pyrefly: ignore[missing-import]

ROOT = Path(__file__).resolve().parents[1]


def facts() -> dict:
    """Return a machine ready for restart with a saved static Netplan address."""
    return {
        "sudo": True,
        "fstab": "UUID=root / ext4 defaults 0 1\nUUID=boot /boot ext4 defaults,nofail 0 2\nUUID=data /scratch ext4 defaults,nofail 0 2\n",
        "boot_mounted": True,
        "grub_defaults": "GRUB_DEFAULT=0\n",
        "grub_cfg": "menuentry 'Ubuntu' {\n linux /vmlinuz-6.8.0-test root=UUID=x\n}\n",
        "grubenv": "",
        "kernels": ["6.8.0-test"],
        "module": {"6.8.0-test": True},
        "routes": [{"dst": "default", "gateway": "10.0.0.1", "dev": "enp0s1", "prefsrc": "10.0.0.2"}],
        "addresses": [{"ifname": "enp0s1", "addr_info": [{"family": "inet", "local": "10.0.0.2", "prefixlen": 24}]}],
        "netplan": "network:\n  ethernets:\n    enp0s1:\n      addresses: [10.0.0.2/24]\n      routes:\n        - to: default\n          via: 10.0.0.1\n",
        "netplan_files": ["/etc/netplan/50-cloud-init.yaml"],
        "volatile_netplan": [],
        "changed_files": [],
        "nm": {},
        "apt": 'Unattended-Upgrade::Automatic-Reboot "false";\n',
    }


class RestartCheckTest(unittest.TestCase):
    """Check the same command an administrator runs, and focused saved/live comparisons."""

    def test_every_machine_and_failure_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            machines = folder / "machines"
            machines.mkdir()
            for name in ("front", "gpu4", "gpu2", "cpu1", "gpu4i", "store"):
                machine = facts()
                if name == "front":
                    machine["fstab"] = machine["fstab"].replace("defaults,nofail 0 2", "defaults 0 2", 1)
                (machines / f"{name}.json").write_text(json.dumps(machine))
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(
                bin_folder,
                "ssh",
                'while [ "${1#-}" != "$1" ]; do case "$1" in -F|-o) shift 2;; *) shift;; esac; done\ncat "$FAKE_MACHINES/$1.json"\n',
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from nanohpc.cli import main; main()",
                    "check",
                    str(ROOT / "examples/cluster.yml"),
                    "--before-restart",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{bin_folder}:{os.environ['PATH']}", "FAKE_MACHINES": str(machines)},
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            for name in ("front", "gpu4", "gpu2", "cpu1", "gpu4i", "store"):
                self.assertIn(name, result.stdout)
            self.assertIn("front: fstab: /boot has no nofail", result.stdout)
            self.assertIn("gpu4", result.stdout)

    def test_unknown_network_fails(self) -> None:
        machine = facts()
        machine["netplan"] = ""
        result = evaluate(machine, False)
        self.assertTrue(result["network"])

    def test_dhcp_saved_profile_passes_without_same_lease_address(self) -> None:
        machine = facts()
        machine["netplan"] = "network:\n  ethernets:\n    enp0s1:\n      dhcp4: true\n"
        machine["addresses"][0]["addr_info"][0]["dynamic"] = True
        machine["routes"][0]["protocol"] = "dhcp"
        self.assertEqual(evaluate(machine, False)["network"], [])

    def test_unmounted_boot_and_last_auto_reboot_value_fail(self) -> None:
        machine = facts()
        machine["boot_mounted"] = False
        machine["apt"] = 'Unattended-Upgrade::Automatic-Reboot "false";\nUnattended-Upgrade::Automatic-Reboot "true";\n'
        result = evaluate(machine, False)
        self.assertIn("/boot is not mounted", result["fstab"])
        self.assertTrue(result["restarts"])

    def test_temporary_netplan_and_mismatched_match_fail(self) -> None:
        machine = facts()
        machine["volatile_netplan"] = ["/run/netplan/override.yaml"]
        self.assertTrue(evaluate(machine, False)["network"])
        machine["volatile_netplan"] = []
        machine["netplan"] = "network:\n  ethernets:\n    enp0s1:\n      match: {name: enp0s2}\n      dhcp4: true\n"
        machine["addresses"][0]["addr_info"][0]["dynamic"] = True
        machine["routes"][0]["protocol"] = "dhcp"
        self.assertTrue(evaluate(machine, False)["network"])

    def test_boot_noauto_and_unknown_restart_value_fail(self) -> None:
        machine = facts()
        machine["fstab"] = machine["fstab"].replace("defaults,nofail", "defaults,noauto", 1)
        machine["apt"] = 'Unattended-Upgrade::Automatic-Reboot "maybe";\n'
        result = evaluate(machine, False)
        self.assertIn("/boot has no nofail", result["fstab"])
        self.assertTrue(result["restarts"])

    def test_persistent_networkmanager_profile_and_unsaved_edit(self) -> None:
        machine = facts()
        machine["netplan"] = None
        machine["netplan_files"] = []
        machine["nm"] = {
            "uuid": "123",
            "files": {
                "/etc/NetworkManager/system-connections/lab.nmconnection": {
                    "connection": {"uuid": "123", "autoconnect": "true"},
                    "ipv4": {"method": "manual", "address1": "10.0.0.2/24,10.0.0.1"},
                }
            },
        }
        self.assertEqual(evaluate(machine, False)["network"], [])
        machine["nm"]["files"]["/etc/NetworkManager/system-connections/lab.nmconnection"]["ipv4"]["address1"] = (
            "10.0.0.9/24,10.0.0.1"
        )
        self.assertTrue(evaluate(machine, False)["network"])

    def test_malformed_netplan_fails_this_machine(self) -> None:
        machine = facts()
        machine["netplan"] = "network: [broken"
        self.assertTrue(evaluate(machine, False)["network"])

    def test_netplan_match_mac_address(self) -> None:
        machine = facts()
        machine["addresses"][0]["address"] = "52:54:00:11:22:33"
        machine["netplan"] = (
            "network:\n  ethernets:\n    uplink:\n      match: {macaddress: '52:54:00:11:22:33'}\n"
            "      set-name: enp0s1\n      addresses: [10.0.0.2/24]\n"
            "      routes: [{to: default, via: 10.0.0.1}]\n"
        )
        self.assertEqual(evaluate(machine, False)["network"], [])


if __name__ == "__main__":
    unittest.main()
