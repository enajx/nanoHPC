"""Key-only root login for administrators: the root key file and nanoHPC's sshd settings, rendered from their
templates as the deploy does. When this machine has an sshd, it also reads the effective settings with
`sshd -T`, as the deploy's own check does on every machine. The preflight check of root's own key files runs in
real ansible-playbook on this machine."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import jinja2
import yaml

from nanohpc.deploy import ansible_cfg

TEMPLATES = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/accounts/templates"
FILTERS = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/preflight/filter_plugins/root_keys.py"
USERS = [
    {
        "name": "alice",
        "uid": 2000,
        "ssh_keys": ["ssh-ed25519 AAAAalice1 alice@laptop", "ssh-ed25519 AAAAalice2 alice@desk"],
    },
    {"name": "bob", "uid": 2001, "ssh_keys": ["ssh-ed25519 AAAAbob bob@laptop"]},
    {"name": "carol", "uid": 2002, "ssh_keys": ["ssh-ed25519 AAAAcarol carol@laptop"]},
]


def render(template: str, values: dict[str, Any]) -> str:
    """Render a template of the accounts role with Ansible's template settings (trim_blocks)."""
    environment = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    return environment.from_string((TEMPLATES / template).read_text()).render(**values)


def root_keys(admins: list[str], auto_deploy_key: str | None) -> str:
    """Render /etc/ssh/authorized_keys/root for the example users."""
    return render(
        "root_authorized_keys.j2",
        {"users": USERS, "admins": admins, "auto_deploy_key": auto_deploy_key, "front_address": "10.0.0.10"},
    )


def sshd_settings(allowed: list[str]) -> str:
    """Render /etc/ssh/sshd_config.d/10-nanohpc.conf."""
    return render("sshd_nanohpc.conf.j2", {"ssh_allowed": allowed})


def key_lines(text: str) -> list[str]:
    """Return the key lines of an authorized_keys file (not comments or blank lines)."""
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


class RootKeysTest(unittest.TestCase):
    """Each administrator's keys from cluster.yml unlock root; the front node's key only from its address."""

    def test_administrators_keys_only(self) -> None:
        lines = key_lines(root_keys(["alice", "carol"], None))
        self.assertEqual(
            lines,
            [
                "ssh-ed25519 AAAAalice1 alice@laptop",
                "ssh-ed25519 AAAAalice2 alice@desk",
                "ssh-ed25519 AAAAcarol carol@laptop",
            ],
        )

    def test_removed_administrator_loses_root(self) -> None:
        lines = key_lines(root_keys(["carol"], None))
        self.assertEqual(lines, ["ssh-ed25519 AAAAcarol carol@laptop"])

    def test_automatic_deploy_key_keeps_its_front_only_limit(self) -> None:
        lines = key_lines(root_keys(["alice"], "ssh-ed25519 AAAAfront nanohpc-auto-deploy@front"))
        self.assertEqual(
            lines,
            [
                "ssh-ed25519 AAAAalice1 alice@laptop",
                "ssh-ed25519 AAAAalice2 alice@desk",
                'from="10.0.0.10",no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAAfront nanohpc-auto-deploy@front',
            ],
        )


class SshdSettingsTest(unittest.TestCase):
    """Root may log in from anywhere with a key, and only with the keys in /etc/ssh/authorized_keys/root."""

    def test_key_only_root_login_for_everyone_allowed(self) -> None:
        lines = sshd_settings(["alice", "ubuntu"]).splitlines()
        self.assertIn("PasswordAuthentication no", lines)
        self.assertIn("KbdInteractiveAuthentication no", lines)
        self.assertIn("PermitRootLogin prohibit-password", lines)
        self.assertIn("AllowUsers alice ubuntu root", lines)
        self.assertIn("AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/authorized_keys/%u", lines)

    def test_root_match_block_is_last_and_sets_only_the_key_file(self) -> None:
        """sshd applies every line after a Match line to that Match only, so the block must close the file and
        hold nothing but root's key file."""
        lines = [line for line in sshd_settings(["alice"]).splitlines() if line.strip() and not line.startswith("#")]
        matches = [index for index, line in enumerate(lines) if line.lower().startswith("match")]
        self.assertEqual(len(matches), 1)
        self.assertEqual(lines[matches[0]], "Match User root")
        self.assertEqual(
            [line.strip() for line in lines[matches[0] + 1 :]], ["AuthorizedKeysFile /etc/ssh/authorized_keys/root"]
        )

    @unittest.skipUnless(shutil.which("sshd") or Path("/usr/sbin/sshd").exists(), "needs an OpenSSH sshd")
    def test_effective_settings_with_sshd(self) -> None:
        """sshd -T on a main config that includes the file first, like Ubuntu's, then a later file that turns
        password login on: root reads keys only from its file, other users from both places."""
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "sshd_config.d").mkdir()
            (folder / "sshd_config.d" / "10-nanohpc.conf").write_text(sshd_settings(["alice"]))
            (folder / "sshd_config.d" / "50-cloud-init.conf").write_text("PasswordAuthentication yes\n")
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(folder / "host_key")], check=True)
            # Subsystem is refused inside a Match block, so this also shows the Match ends with the file.
            (folder / "sshd_config").write_text(
                f"Include {folder}/sshd_config.d/*.conf\nHostKey {folder}/host_key\nSubsystem sftp internal-sftp\n"
            )

            def effective(user: str) -> list[str]:
                result = subprocess.run(
                    [sshd, "-T", "-f", str(folder / "sshd_config"), "-C", f"user={user},host=laptop,addr=203.0.113.5"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return result.stdout.splitlines()

            root = effective("root")
            self.assertIn("authorizedkeysfile /etc/ssh/authorized_keys/root", root)
            self.assertIn("passwordauthentication no", root)
            self.assertTrue({"permitrootlogin prohibit-password", "permitrootlogin without-password"} & set(root))
            self.assertIn("allowusers root", root)
            self.assertIn("authorizedkeysfile .ssh/authorized_keys /etc/ssh/authorized_keys/%u", effective("alice"))


def public_key(folder: Path, name: str, comment: str) -> tuple[str, str]:
    """Make an ed25519 key pair in `folder` and return its public key line and its SHA256 fingerprint, as
    ssh-keygen prints them."""
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(folder / name)], check=True)
    listing = subprocess.run(
        ["ssh-keygen", "-l", "-f", str(folder / f"{name}.pub")], capture_output=True, text=True, check=True
    )
    return (folder / f"{name}.pub").read_text().strip(), listing.stdout.split()[1]


def root_key_filters() -> dict[str, Any]:
    """Load the preflight role's filters (filter_plugins/root_keys.py) as Ansible does."""
    spec = importlib.util.spec_from_file_location("root_keys", FILTERS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FilterModule().filters()


class RootKeysPreflightTest(unittest.TestCase):
    """The preflight check of root's own key files (roles/preflight/tasks/root_keys.yml), run by real
    ansible-playbook on this machine in check mode, as the dry run does. It reads the files that sshd reads for
    root today (from `sshd -T`, here naming files in a temporary folder), apart from nanoHPC's own file. A key in
    them that is not an administrator's key in cluster.yml stops that machine, naming each such key, unless it
    gives no root access today (cloud-init's "Please login as the user" forced command)."""

    def test_keys_that_would_stop_working_stop_the_machine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            alice_key, alice_fingerprint = public_key(folder, "alice", "alice@laptop")
            bob_key, bob_fingerprint = public_key(folder, "bob", "bob@laptop")
            backup_key, backup_fingerprint = public_key(folder, "backup", "")
            ubuntu_key, ubuntu_fingerprint = public_key(folder, "ubuntu", "ubuntu@laptop")
            alice_type, alice_data = alice_key.split()[:2]
            backup_type, backup_data = backup_key.split()[:2]
            # cloud-init's disable_root_opts (DISABLE_USER_OPTS in cloudinit/ssh_util.py), filled in for ubuntu.
            cloud_init = (
                (
                    "no-port-forwarding,no-agent-forwarding,"
                    'no-X11-forwarding,command="echo \'Please login as the user \\"$USER\\"'
                    ' rather than the user \\"$DISABLE_USER\\".\';echo;sleep 10;'
                    'exit 142"'
                )
                .replace("$USER", "ubuntu")
                .replace("$DISABLE_USER", "root")
            )
            files = {
                # Settings active: sshd reads only nanoHPC's file, so nothing is read.
                "active": {"authorized_keys": f"{bob_key}\n"},
                # An administrator's key with options and another comment, comment lines, and cloud-init's key
                # that only says to log in as ubuntu. authorized_keys2 is not read: sshd does not list it.
                "admins": {
                    "authorized_keys": (
                        f'# keys\n\nfrom="10.0.0.1",command="echo hi there" {alice_type} {alice_data} other comment\n'
                        f"{cloud_init} {ubuntu_key}\n"
                    ),
                    "authorized_keys2": f"{bob_key}\n",
                },
                # A user who is not an administrator, an outside backup's rsync key (a forced command that works
                # today), and a line that is no key.
                "outsiders": {
                    "authorized_keys": f"{alice_key}\n{bob_key}\n",
                    "authorized_keys2": f'no-pty,command="rsync --server x" {backup_type} {backup_data}\nnot a key\n',
                },
            }
            settings = {
                "active": "authorizedkeysfile /etc/ssh/authorized_keys/root",
                "admins": f"authorizedkeysfile {folder}/admins/authorized_keys {folder}/admins/missing"
                " /etc/ssh/authorized_keys/%u",
                "outsiders": f"authorizedkeysfile {folder}/outsiders/authorized_keys {folder}/outsiders/authorized_keys2",
            }
            hosts: dict[str, Any] = {}
            for host, contents in files.items():
                (folder / host).mkdir()
                for name, text in contents.items():
                    (folder / host / name).write_text(text)
                hosts[host] = {"sshd_root_settings": ["permitrootlogin yes", settings[host]]}
            inventory = {
                "all": {
                    "vars": {
                        "ansible_connection": "local",
                        "ansible_python_interpreter": sys.executable,
                        "nanohpc": {
                            "admins": ["alice"],
                            "users": [
                                {"name": "alice", "uid": 2000, "ssh_keys": [alice_key]},
                                {"name": "bob", "uid": 2001, "ssh_keys": [bob_key]},
                            ],
                        },
                    },
                    "hosts": hosts,
                }
            }
            (folder / "inventory.yml").write_text(yaml.safe_dump(inventory))
            play = {
                "hosts": "all",
                "gather_facts": False,
                "tasks": [
                    {
                        "name": "Check root's own keys",
                        "ansible.builtin.include_role": {"name": "preflight", "tasks_from": "root_keys"},
                    }
                ],
            }
            (folder / "play.yml").write_text(yaml.safe_dump([play]))
            (folder / "ansible.cfg").write_text(ansible_cfg(None))
            result = subprocess.run(
                [str(Path(sys.executable).parent / "ansible-playbook"), "-i", str(folder / "inventory.yml"),
                 str(folder / "play.yml"), "--check"],
                env={**os.environ, "ANSIBLE_CONFIG": str(folder / "ansible.cfg"),
                     "NANOHPC_RECORD": str(folder / "record.json")},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
            )  # fmt: skip
            output = result.stdout + result.stderr
            self.assertTrue((folder / "record.json").exists(), output)
            failed = json.loads((folder / "record.json").read_text())["failed"]
            self.assertEqual(sorted(failed), ["outsiders"], output)
            message = failed["outsiders"]
            self.assertIn(f"{folder}/outsiders/authorized_keys: ssh-ed25519 {bob_fingerprint} bob@laptop", message)
            self.assertIn(
                f"{folder}/outsiders/authorized_keys2: ssh-ed25519 {backup_fingerprint} (no comment)", message
            )
            self.assertIn(f"{folder}/outsiders/authorized_keys2 line 2: not a key line", message)
            self.assertNotIn(alice_fingerprint, message)
            self.assertNotIn(ubuntu_fingerprint, message)
            self.assertIn("These keys will stop working for root after this deploy", message)
            self.assertIn("Delete them from that file, or add the key to an administrator in cluster.yml.", message)
            self.assertIn("Nothing was changed on this machine.", message)

    def test_files_sshd_reads_for_root(self) -> None:
        """The paths in sshd -T's authorizedkeysfile for root, as sshd expands them (root's home is /root),
        without nanoHPC's own file."""
        filters = root_key_filters()
        self.assertEqual(
            filters["root_key_files"](
                ["permitrootlogin yes", "authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2"]
            ),
            ["/root/.ssh/authorized_keys", "/root/.ssh/authorized_keys2"],
        )
        self.assertEqual(
            filters["root_key_files"](
                ["authorizedkeysfile %h/.ssh/authorized_keys /etc/ssh/authorized_keys/%u /keys/%%/%u"]
            ),
            ["/root/.ssh/authorized_keys", "/keys/%/root"],
        )
        self.assertEqual(filters["root_key_files"](["authorizedkeysfile /etc/ssh/authorized_keys/root"]), [])


if __name__ == "__main__":
    unittest.main()
