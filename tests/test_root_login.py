"""Key-only root login for administrators: the root key file and nanoHPC's sshd settings, rendered from their
templates as the deploy does. When this machine has an sshd, it also reads the effective settings with
`sshd -T`, as the deploy's own check does on every machine."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import jinja2

TEMPLATES = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/accounts/templates"
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


if __name__ == "__main__":
    unittest.main()
