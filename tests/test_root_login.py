"""Key-only root login for administrators: the root key file and nanoHPC's sshd settings, rendered from their
templates as the deploy does. When this machine has an sshd, it also reads the effective settings with
`sshd -T`, and the accounts role's checks of them run in real ansible-playbook with that sshd. The preflight check
of root's own key files, and the condition that says where it runs, run in real ansible-playbook on this
machine."""

import base64
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

ROLES = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles"
TEMPLATES = ROLES / "accounts/templates"
FILTERS = ROLES / "preflight/filter_plugins/root_keys.py"
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
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


# Settings another file in sshd_config.d could hold: keys from a command and from a certificate authority for
# everyone, and password login (also for root) from some addresses.
OTHER_SSHD_SETTINGS = (
    "AuthorizedKeysCommand /usr/local/bin/keys %u\n"
    "AuthorizedKeysCommandUser nobody\n"
    "TrustedUserCAKeys /etc/ssh/user_ca.pub\n"
    "Match Address 198.51.100.0/24\n"
    "    PasswordAuthentication yes\n"
    "    KbdInteractiveAuthentication yes\n"
    "    PermitRootLogin yes\n"
)


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

    def test_root_match_block_is_last_and_sets_only_roots_login(self) -> None:
        """sshd applies every line after a Match line to that Match only, so the block must close the file. It
        holds root's key file, turns off the other ways to log in as root (keys from a command or a certificate
        authority, password), and repeats the key-only settings, which a later Match block could otherwise change
        for root."""
        lines = [line for line in sshd_settings(["alice"]).splitlines() if line.strip() and not line.startswith("#")]
        matches = [index for index, line in enumerate(lines) if line.lower().startswith("match")]
        self.assertEqual(len(matches), 1)
        self.assertEqual(lines[matches[0]], "Match User root")
        self.assertEqual(
            [line.strip() for line in lines[matches[0] + 1 :]],
            [
                "AuthorizedKeysFile /etc/ssh/authorized_keys/root",
                "AuthorizedKeysCommand none",
                "TrustedUserCAKeys none",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PermitRootLogin prohibit-password",
            ],
        )

    @unittest.skipUnless(Path(SSHD).exists(), "needs an OpenSSH sshd")
    def test_effective_settings_with_sshd(self) -> None:
        """sshd -T on a main config that includes the file first, like Ubuntu's, then later files that turn
        password login on, also for root from some addresses (a Match Address block), and add keys from a command
        and a certificate authority: root logs in only with a key from its file, other users read keys from both
        places."""
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "sshd_config.d").mkdir()
            (folder / "sshd_config.d" / "10-nanohpc.conf").write_text(sshd_settings(["alice"]))
            (folder / "sshd_config.d" / "50-cloud-init.conf").write_text("PasswordAuthentication yes\n")
            (folder / "sshd_config.d" / "60-other.conf").write_text(OTHER_SSHD_SETTINGS)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(folder / "host_key")], check=True)
            # Subsystem is refused inside a Match block, so this also shows the Match ends with the file.
            (folder / "sshd_config").write_text(
                f"Include {folder}/sshd_config.d/*.conf\nHostKey {folder}/host_key\nSubsystem sftp internal-sftp\n"
            )

            def effective(user: str) -> list[str]:
                result = subprocess.run(
                    [SSHD, "-T", "-f", str(folder / "sshd_config"), "-C", f"user={user},host=laptop,addr=198.51.100.7"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return result.stdout.splitlines()

            root = effective("root")
            self.assertIn("authorizedkeysfile /etc/ssh/authorized_keys/root", root)
            self.assertIn("authorizedkeyscommand none", root)
            self.assertIn("trustedusercakeys none", root)
            self.assertIn("passwordauthentication no", root)
            self.assertIn("kbdinteractiveauthentication no", root)
            self.assertTrue({"permitrootlogin prohibit-password", "permitrootlogin without-password"} & set(root))
            self.assertIn("allowusers root", root)
            self.assertIn("authorizedkeysfile .ssh/authorized_keys /etc/ssh/authorized_keys/%u", effective("alice"))


def run_playbook(
    folder: Path, inventory: dict[str, Any], play: dict[str, Any], arguments: list[str], environment: dict[str, str]
) -> dict[str, Any]:
    """Run one play with real ansible-playbook on this machine, with nanoHPC's ansible.cfg, and return what
    nanoHPC's record callback wrote: {"changed": ..., "failed": {host: "task: message"}, "facts": ...}."""
    (folder / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (folder / "play.yml").write_text(yaml.safe_dump([play]))
    (folder / "ansible.cfg").write_text(ansible_cfg(None))
    record = folder / "record.json"
    record.unlink(missing_ok=True)
    result = subprocess.run(
        [str(Path(sys.executable).parent / "ansible-playbook"), "-i", str(folder / "inventory.yml"),
         str(folder / "play.yml"), *arguments],
        env={**os.environ, "ANSIBLE_CONFIG": str(folder / "ansible.cfg"), "NANOHPC_RECORD": str(record),
             **environment},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert record.exists(), result.stdout + result.stderr
    return {**json.loads(record.read_text()), "output": result.stdout + result.stderr}


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
            record = run_playbook(folder, inventory, play, ["--check"], {})
            failed = record["failed"]
            self.assertEqual(sorted(failed), ["outsiders"], record["output"])
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


def sshd_check_tasks() -> list[dict[str, Any]]:
    """Return the accounts role's tasks that read and check the effective SSH settings, from the block that
    changes them, with sshd reading the main config in the host variable `sshd_config_file` (not
    /etc/ssh/sshd_config)."""
    tasks = yaml.safe_load((ROLES / "accounts/tasks/main.yml").read_text())
    block = next(task for task in tasks if task.get("name", "").startswith("Change SSH access"))["block"]
    checks = [task for task in block if "ansible.builtin.command" in task or "ansible.builtin.assert" in task]
    for task in checks:
        if "ansible.builtin.command" in task:
            command = task["ansible.builtin.command"]
            assert command.startswith("/usr/sbin/sshd -T "), command
            task["ansible.builtin.command"] = command.replace(
                "/usr/sbin/sshd -T", f"{SSHD} -T -f {{{{ sshd_config_file }}}}"
            )
    return checks


@unittest.skipUnless(Path(SSHD).exists(), "needs an OpenSSH sshd")
class SshdChecksTest(unittest.TestCase):
    """The accounts role's checks of the effective SSH settings (`sshd -T` for the login account and for root),
    run by real ansible-playbook on this machine with this machine's sshd, on a config like Ubuntu's: nanoHPC's
    file included first, then other files. Each host here is one such config. The deploy stops (and puts the old
    settings back) when another file still lets root in some other way, refuses root, or changes the key files of
    the users; with automatic deploys (login as root) the users' settings are checked for the first
    administrator."""

    def test_checks_of_the_effective_settings(self) -> None:
        other_for_root = OTHER_SSHD_SETTINGS.replace(
            "Match Address 198.51.100.0/24", "Match Address 198.51.100.0/24 User root"
        )
        other_files = {
            # Keys from a command and a certificate authority, and password login for root from this machine's
            # address: nanoHPC's Match User root block turns all of these off for root.
            "safe": {"60-other.conf": other_for_root},
            "safe_as_root": {"60-other.conf": other_for_root},
            # A file that sorts before nanoHPC's wins: root's keys from a command or a certificate authority.
            "command_keys": {
                "05-first.conf": "Match User root\n    AuthorizedKeysCommand /usr/local/bin/keys %u\n"
                "    AuthorizedKeysCommandUser nobody\n"
            },
            "ca_keys": {"05-first.conf": "Match User root\n    TrustedUserCAKeys /etc/ssh/user_ca.pub\n"},
            # Root refused: login as root would fail although the deploy passed.
            "deny_user": {"70-deny.conf": "DenyUsers bob ro*\n"},
            "deny_user_at_host": {"70-deny.conf": "DenyUsers root@10.0.0.*\n"},
            "deny_group": {"70-deny.conf": "DenyGroups wheel root\n"},
            # Login as root (automatic deploys), and alice, the first administrator, reads keys only from her home.
            "users_keys_as_root": {"70-alice.conf": "Match User alice\n    AuthorizedKeysFile .ssh/authorized_keys\n"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(folder / "host_key")], check=True)
            hosts: dict[str, Any] = {}
            for host, files in other_files.items():
                (folder / host / "sshd_config.d").mkdir(parents=True)
                (folder / host / "sshd_config.d" / "10-nanohpc.conf").write_text(sshd_settings(["alice", "ubuntu"]))
                for name, text in files.items():
                    (folder / host / "sshd_config.d" / name).write_text(text)
                (folder / host / "sshd_config").write_text(
                    f"Include {folder}/{host}/sshd_config.d/*.conf\nHostKey {folder}/host_key\n"
                )
                hosts[host] = {
                    "sshd_config_file": str(folder / host / "sshd_config"),
                    "nanohpc_login_user": "root" if host.endswith("_as_root") else "ubuntu",
                }
            inventory = {
                "all": {
                    "vars": {
                        "ansible_connection": "local",
                        "ansible_python_interpreter": sys.executable,
                        "ssh_access": {"changed": False},
                        "nanohpc": {
                            "admins": ["alice", "bob"],
                            "users": USERS,
                            "machines": {host: {"address": "198.51.100.7"} for host in other_files},
                        },
                    },
                    "hosts": hosts,
                }
            }
            play = {"hosts": "all", "gather_facts": True, "gather_subset": ["min"], "tasks": sshd_check_tasks()}
            record = run_playbook(
                folder, inventory, play, [], {"ANSIBLE_FILTER_PLUGINS": str(ROLES / "accounts/filter_plugins")}
            )
            failed = record["failed"]
            self.assertEqual(
                sorted(failed),
                ["ca_keys", "command_keys", "deny_group", "deny_user", "deny_user_at_host", "users_keys_as_root"],
                record["output"],
            )
            for host in ("ca_keys", "command_keys", "deny_group", "deny_user", "deny_user_at_host"):
                self.assertIn("another SSH setting overrides nanoHPC's root login", failed[host], host)
            for host, line in (
                ("deny_user", "denyusers ro*"),
                ("deny_user_at_host", "denyusers root@10.0.0.*"),
                ("deny_group", "denygroups root"),
            ):
                self.assertIn(f"Root is refused by: {line}.", failed[host], host)
            self.assertIn("the login account 'alice' not allowed", failed["users_keys_as_root"])


class CloudInitRefusalTest(unittest.TestCase):
    """Only cloud-init's own refusal key (its forced command is exactly cloud-init's: it says to log in as another
    user, waits, and exits with 142) is left out of the dry run's stop on root's keys. A look-alike key that still gives a shell counts."""

    def test_look_alike_keys_count(self) -> None:
        filters = root_key_filters()

        def key(number: int) -> str:
            return "ssh-ed25519 " + base64.b64encode(f"test key number {number:03}".encode()).decode()

        cloud_init = (
            "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,"
            'command="echo \'Please login as the user \\"ubuntu\\" rather than the user \\"root\\".\';echo;sleep 10;'
            'exit 142"'
        )
        lines = [
            f"{cloud_init} {key(1)} cloud-init",
            f'command="echo \'Please login as the user \\"ubuntu\\"\';exec bash -l;exit 142" {key(2)} exec',
            f'command="echo \'Please login as the user\';sh -c \\"$SSH_ORIGINAL_COMMAND\\";exit 142" {key(3)} original',
            f'command="echo \'Please login as the user \\"ubuntu\\"\';bash" {key(4)} shell',
            f"command=\"echo 'Please login as the user';bash;exit 142 \" {key(5)} space",
            f"command=\"echo 'Please login as the user';bash;exit 142\" {key(6)} bash",
        ]
        content = base64.b64encode("\n".join(lines).encode()).decode()
        found = filters["root_keys_to_lose"]([{"source": "/root/.ssh/authorized_keys", "content": content}], [])
        self.assertEqual([entry.split()[-1] for entry in found], ["exec", "original", "shell", "space", "bash"])


class RootKeysStopWhereTest(unittest.TestCase):
    """The dry run's stop on root's keys runs only where the deploy changes sshd's settings (the accounts role):
    a full deploy, `--only users`, and the new machine of `--only node`; not `--only policy` or
    `--only partitions`. The preflight runs in every part (tag `always` in partial.yml), so its block of root-key
    tasks carries the condition. Here real ansible-playbook evaluates that condition with each part's --tags."""

    def test_condition_under_each_part(self) -> None:
        tasks = yaml.safe_load((ROLES / "preflight/tasks/main.yml").read_text())
        blocks = [task for task in tasks if "block" in task and "root_keys.yml" in json.dumps(task["block"])]
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertIn("user=root", json.dumps(block["block"]))
        partial = yaml.safe_load((ROLES.parent / "partial.yml").read_text())
        self.assertEqual(partial[0]["roles"], ["preflight"])
        self.assertEqual(partial[0]["tags"], ["always"])
        play = {
            "hosts": "all",
            "gather_facts": False,
            "tags": ["always"],
            "tasks": [
                {
                    "name": "Root key stop",
                    "ansible.builtin.set_fact": {"nanohpc_dry_run_root_keys": True},
                    "when": block["when"],
                }
            ],
        }
        inventory = {
            "all": {
                "vars": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable},
                "hosts": {"new": None, "other": None},
                "children": {"only_node": {"hosts": {"new": None}}},
            }
        }
        expected = {
            "": ["new", "other"],
            "users": ["new", "other"],
            "node": ["new"],
            "policy": [],
            "partitions": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            for tag, machines in expected.items():
                record = run_playbook(Path(temporary), inventory, play, ["--tags", tag] if tag else [], {})
                self.assertEqual(sorted(record["facts"]), machines, (tag, record["output"]))


if __name__ == "__main__":
    unittest.main()
