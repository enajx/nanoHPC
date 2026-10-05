"""The confirmed compute update through the public nanoHPC command."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_probe import write_command  # pyrefly: ignore[missing-import]
from tests.test_restart_check import facts  # pyrefly: ignore[missing-import]

ROOT = Path(__file__).resolve().parents[1]

FAKE_PREVIEW_SSH = f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
index = 0
login = ''
while args[index].startswith('-'):
    if args[index] == '-l':
        login = args[index + 1]
    index += 2 if args[index] in ('-F', '-o', '-l') else 1
target, command = args[index], args[index + 1]
with open(os.environ['UPDATE_SSH_LOG'], 'a') as log:
    log.write(json.dumps([target, command]) + '\\n')
state_path = os.environ.get('UPDATE_FAKE_STATE')
state = json.load(open(state_path)) if state_path else {{}}
def save():
    if state_path:
        with open(state_path, 'w') as file:
            json.dump(state, file)
if 'nanohpc-root-hold' in command:
    print('READY', flush=True)
    time.sleep(30)
elif 'nanohpc-ssh-prepare' in command:
    state['undo_armed'] = True
    save()
elif 'nanohpc-ssh-cancel' in command:
    state['undo_armed'] = False
    save()
elif 'nanohpc-ssh-repair-check' in command:
    if state.get('undo_needs_review'):
        sys.stderr.write('SSH undo backup is present; verify access and package state\\n')
        sys.exit(1)
elif 'nanohpc-ssh-listener-check' in command or 'systemctl restart ssh' in command:
    pass
elif 'ip -4 -j addr show' in command:
    print(json.dumps([{{'addr_info': [{{'local': '192.168.104.13' if target == 'cpu1' else '192.168.104.10'}}]}}]))
elif command.strip() == 'id -un':
    print('alice')
elif 'nanohpc-update-snapshot' in command:
    print(json.dumps({{'lists_hash': os.environ.get('UPDATE_FAKE_LISTS_HASH', 'lists1'),
                       'dpkg_hash': 'dpkg1', 'packages': [
        {{'name': 'bash', 'version': os.environ.get('UPDATE_FAKE_BASH_VERSION', '5.2'), 'ubuntu': True, 'security': True}},
        {{'name': 'openssh-server', 'version': '9.6', 'ubuntu': True, 'security': True}},
        {{'name': 'vendor-tool', 'version': '2', 'ubuntu': False, 'security': False}},
        *([{{'name': 'linux-generic', 'version': '2', 'ubuntu': True, 'security': False}}]
          if os.environ.get('UPDATE_FAKE_KERNEL') else [])
    ]}}))
elif 'apt-get update' in command:
    print('Hit: Ubuntu package lists')
elif 'apt-get -s' in command:
    print('Inst bash [5.1] (5.2 Ubuntu:24.04/noble-security [arm64])')
    print('Conf bash (5.2 Ubuntu:24.04/noble-security [arm64])')
    if 'openssh-server=9.6' in command:
        print('Inst openssh-server [9.5] (9.6 Ubuntu:24.04/noble-security [arm64])')
        print('Conf openssh-server (9.6 Ubuntu:24.04/noble-security [arm64])')
    if 'linux-generic=2' in command:
        print('Inst linux-image-2 (2 Ubuntu:24.04/noble-updates [arm64])')
        print('Inst linux-generic [1] (2 Ubuntu:24.04/noble-updates [arm64])')
elif 'mkdir /run/nanohpc-restart.lock' in command or 'rmdir /run/nanohpc-restart.lock' in command:
    if 'rmdir /run/nanohpc-restart.lock' in command and os.environ.get('UPDATE_FAKE_UNLOCK_FAIL'):
        sys.exit(1)
elif 'scontrol show node' in command:
    print('NodeName=cpu1 State=' + state.get('node_state', 'IDLE'))
elif 'state=drain' in command:
    state['node_state'] = 'DRAIN'
    save()
elif 'state=resume' in command:
    state['node_state'] = 'IDLE'
    save()
elif 'squeue' in command:
    pass
elif 'systemctl' in command and ('--failed' in command or '--state=failed' in command):
    if os.environ.get('UPDATE_FAKE_NEW_FAILED_UNIT') and state.get('installed'):
        print('● broken.service loaded failed failed A broken service')
elif 'apt-get install' in command:
    state['installed'] = True
    save()
elif 'sshd -t' in command or 'systemctl is-active slurmd' in command:
    if 'slurmd' in command:
        print('active')
elif 'python3 -c' in command and os.environ.get('UPDATE_FAKE_FACTS'):
    with open(os.environ['UPDATE_FAKE_FACTS']) as file:
        print(file.read())
elif '/run/reboot-required' in command:
    print('required' if state.get('restart_needed') else 'none')
elif command.strip() == 'true':
    if os.environ.get('UPDATE_FAKE_FAIL_LOGIN') == login:
        sys.exit(255)
else:
    sys.stderr.write('unexpected fake SSH command: ' + command[:200] + '\\n')
    sys.exit(3)
"""


class ComputeUpdateCommandTest(unittest.TestCase):
    """Invalid targets stop before SSH or APT can change a machine."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        write_command(bin_folder, "ssh", 'echo contacted >> "$UPDATE_SSH_LOG"\nexit 1\n')
        self.log = self.folder / "ssh.log"
        self.environment = {
            **os.environ,
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "UPDATE_SSH_LOG": str(self.log),
        }

    def update(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the administrator's update command with fake SSH available on PATH."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def test_front_node_is_refused_before_ssh(self) -> None:
        result = self.update("front", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compute", result.stderr.lower())
        self.assertFalse(self.log.exists())

    def test_real_run_requires_matching_machine_confirmation(self) -> None:
        result = self.update("cpu1", "--confirm", "gpu4")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirm", result.stderr.lower())
        self.assertFalse(self.log.exists())


class ComputeUpdateDryRunTest(unittest.TestCase):
    """The dry run refreshes lists, previews only the allowed updates, and does not drain."""

    def test_dry_run_records_the_exact_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            log = folder / "ssh.log"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from nanohpc.cli import main; main()",
                    "update",
                    str(ROOT / "examples/cluster.yml"),
                    "cpu1",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_folder}:{os.environ['PATH']}",
                    "UPDATE_SSH_LOG": str(log),
                    "XDG_STATE_HOME": str(folder / "state"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("bash", result.stdout)
            self.assertNotIn("openssh-server", result.stdout)
            self.assertNotIn("vendor-tool", result.stdout)
            calls = [json.loads(line)[1] for line in log.read_text().splitlines()]
            self.assertTrue(any("apt-get update" in call for call in calls))
            self.assertTrue(any("apt-get -s" in call for call in calls))
            self.assertFalse(any("state=drain" in call for call in calls))
            previews = list((folder / "state" / "nanohpc" / "updates").glob("*.json"))
            self.assertEqual(len(previews), 1)
            preview = json.loads(previews[0].read_text())
            self.assertEqual(preview["machine"], "cpu1")
            self.assertEqual(preview["include"], [])
            self.assertEqual(preview["lists_hash"], "lists1")
            self.assertEqual(preview["packages"], [["bash", "5.2"]])

    def test_real_run_refuses_changed_lists_and_added_care_group_before_drain(self) -> None:
        """The approved package scope and saved APT state must still match at apply time."""
        for changed in ("lists", "include"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                bin_folder = folder / "bin"
                bin_folder.mkdir()
                write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
                log = folder / "ssh.log"
                environment = {
                    **os.environ,
                    "PATH": f"{bin_folder}:{os.environ['PATH']}",
                    "UPDATE_SSH_LOG": str(log),
                    "XDG_STATE_HOME": str(folder / "state"),
                }
                command = [
                    sys.executable,
                    "-c",
                    "from nanohpc.cli import main; main()",
                    "update",
                    str(ROOT / "examples/cluster.yml"),
                    "cpu1",
                ]
                preview = subprocess.run(
                    [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
                )
                self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
                log.write_text("")
                if changed == "lists":
                    environment["UPDATE_FAKE_LISTS_HASH"] = "lists2"
                arguments = ["--confirm", "cpu1", *(["--include", "ssh"] if changed == "include" else [])]
                result = subprocess.run(
                    [*command, *arguments], capture_output=True, text=True, env=environment, check=False
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("dry run", result.stderr.lower())
                calls = [json.loads(line)[1] for line in log.read_text().splitlines()]
                self.assertFalse(any("state=drain" in call or "apt-get install" in call for call in calls))

    def test_included_kernel_records_and_installs_exact_new_kernel_dependency(self) -> None:
        """A named kernel group may add the exact Ubuntu image from the reviewed simulation."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            state = folder / "machine.json"
            state.write_text(json.dumps({"node_state": "IDLE"}))
            machine_facts = folder / "facts.json"
            machine_facts.write_text(json.dumps(facts()))
            log = folder / "ssh.log"
            environment = {
                **os.environ,
                "PATH": f"{bin_folder}:{os.environ['PATH']}",
                "UPDATE_SSH_LOG": str(log),
                "UPDATE_FAKE_STATE": str(state),
                "UPDATE_FAKE_FACTS": str(machine_facts),
                "UPDATE_FAKE_KERNEL": "1",
                "XDG_STATE_HOME": str(folder / "state"),
            }
            command = [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "cpu1",
                "--include",
                "kernel",
            ]
            dry_run = subprocess.run(
                [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            preview = json.loads(next((folder / "state" / "nanohpc" / "updates").glob("*.json")).read_text())
            self.assertIn(["linux-image-2", "2"], preview["resolved"])
            log.write_text("")
            applied = subprocess.run(
                [*command, "--confirm", "cpu1"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            calls = [json.loads(line)[1] for line in log.read_text().splitlines()]
            install = next(call for call in calls if "apt-get install" in call)
            self.assertIn("linux-image-2=2", install)
            self.assertIn("--no-remove", install)
            self.assertNotIn("--only-upgrade", install)

    def test_pending_ssh_undo_blocks_another_dry_run(self) -> None:
        """An earlier automatic SSH recovery needs administrator review first."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            state = folder / "machine.json"
            state.write_text(json.dumps({"undo_needs_review": True}))
            log = folder / "ssh.log"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from nanohpc.cli import main; main()",
                    "update",
                    str(ROOT / "examples/cluster.yml"),
                    "cpu1",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_folder}:{os.environ['PATH']}",
                    "UPDATE_SSH_LOG": str(log),
                    "UPDATE_FAKE_STATE": str(state),
                    "XDG_STATE_HOME": str(folder / "state"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SSH undo backup", result.stderr)
            self.assertFalse(any("apt-get update" in line for line in log.read_text().splitlines()))


class ComputeUpdateApplyTest(unittest.TestCase):
    """Apply one approved plan after draining, and return the node only when safe."""

    def test_clean_update_resumes_but_restart_needed_stays_drained(self) -> None:
        for restart_needed in (False, True):
            with self.subTest(restart_needed=restart_needed), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                bin_folder = folder / "bin"
                bin_folder.mkdir()
                write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
                log = folder / "ssh.log"
                state = folder / "machine.json"
                state.write_text(json.dumps({"node_state": "IDLE", "restart_needed": restart_needed}))
                machine_facts = folder / "facts.json"
                machine_facts.write_text(json.dumps(facts()))
                environment = {
                    **os.environ,
                    "PATH": f"{bin_folder}:{os.environ['PATH']}",
                    "UPDATE_SSH_LOG": str(log),
                    "UPDATE_FAKE_STATE": str(state),
                    "UPDATE_FAKE_FACTS": str(machine_facts),
                    "XDG_STATE_HOME": str(folder / "state"),
                }
                command = [
                    sys.executable,
                    "-c",
                    "from nanohpc.cli import main; main()",
                    "update",
                    str(ROOT / "examples/cluster.yml"),
                    "cpu1",
                ]
                preview = subprocess.run(
                    [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
                )
                self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
                log.write_text("")
                result = subprocess.run(
                    [*command, "--confirm", "cpu1"], capture_output=True, text=True, env=environment, check=False
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                after = json.loads(state.read_text())
                self.assertTrue(after["installed"])
                self.assertEqual(after["node_state"], "DRAIN" if restart_needed else "IDLE")
                calls = [json.loads(line)[1] for line in log.read_text().splitlines()]
                drained = next(index for index, call in enumerate(calls) if "state=drain" in call)
                installed = next(index for index, call in enumerate(calls) if "apt-get install" in call)
                self.assertLess(drained, installed)
                if not restart_needed:
                    unlocked = next(
                        index for index, call in enumerate(calls) if "rmdir /run/nanohpc-restart.lock" in call
                    )
                    resumed = next(index for index, call in enumerate(calls) if "state=resume" in call)
                    self.assertLess(unlocked, resumed)
                self.assertFalse(any("systemctl reboot" in call for call in calls))

    def test_new_failed_systemd_unit_keeps_node_drained(self) -> None:
        """A newly failed unit must stop the update even when systemctl prefixes it with a symbol."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            state = folder / "machine.json"
            state.write_text(json.dumps({"node_state": "IDLE"}))
            machine_facts = folder / "facts.json"
            machine_facts.write_text(json.dumps(facts()))
            environment = {
                **os.environ,
                "PATH": f"{bin_folder}:{os.environ['PATH']}",
                "UPDATE_SSH_LOG": str(folder / "ssh.log"),
                "UPDATE_FAKE_STATE": str(state),
                "UPDATE_FAKE_FACTS": str(machine_facts),
                "UPDATE_FAKE_NEW_FAILED_UNIT": "1",
                "XDG_STATE_HOME": str(folder / "state"),
            }
            command = [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "cpu1",
            ]
            dry_run = subprocess.run(
                [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            result = subprocess.run(
                [*command, "--confirm", "cpu1"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("broken.service", result.stderr)
            self.assertEqual(json.loads(state.read_text())["node_state"], "DRAIN")

    def test_lock_cleanup_failure_keeps_node_drained(self) -> None:
        """The CLI must not report a failed update while leaving the node resumed."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            state = folder / "machine.json"
            state.write_text(json.dumps({"node_state": "IDLE"}))
            machine_facts = folder / "facts.json"
            machine_facts.write_text(json.dumps(facts()))
            environment = {
                **os.environ,
                "PATH": f"{bin_folder}:{os.environ['PATH']}",
                "UPDATE_SSH_LOG": str(folder / "ssh.log"),
                "UPDATE_FAKE_STATE": str(state),
                "UPDATE_FAKE_FACTS": str(machine_facts),
                "UPDATE_FAKE_UNLOCK_FAIL": "1",
                "XDG_STATE_HOME": str(folder / "state"),
            }
            command = [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "cpu1",
            ]
            dry_run = subprocess.run(
                [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            result = subprocess.run(
                [*command, "--confirm", "cpu1"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lock", result.stderr)
            self.assertEqual(json.loads(state.read_text())["node_state"], "DRAIN")

    def test_ssh_group_holds_two_root_sessions_and_arms_undo_before_install(self) -> None:
        """An SSH upgrade keeps access while its timer protects a failed fresh login."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            log = folder / "ssh.log"
            state = folder / "machine.json"
            state.write_text(json.dumps({"node_state": "IDLE"}))
            machine_facts = folder / "facts.json"
            machine_facts.write_text(json.dumps(facts()))
            environment = {
                **os.environ,
                "PATH": f"{bin_folder}:{os.environ['PATH']}",
                "UPDATE_SSH_LOG": str(log),
                "UPDATE_FAKE_STATE": str(state),
                "UPDATE_FAKE_FACTS": str(machine_facts),
                "XDG_STATE_HOME": str(folder / "state"),
            }
            command = [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "cpu1",
                "--include",
                "ssh",
            ]
            preview = subprocess.run(
                [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
            log.write_text("")
            result = subprocess.run(
                [*command, "--confirm", "cpu1"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(json.loads(state.read_text())["undo_armed"])
            calls = [json.loads(line)[1] for line in log.read_text().splitlines()]
            holds = [index for index, call in enumerate(calls) if "nanohpc-root-hold" in call]
            prepared = next(index for index, call in enumerate(calls) if "nanohpc-ssh-prepare" in call)
            installed = next(index for index, call in enumerate(calls) if "apt-get install" in call)
            canceled = next(index for index, call in enumerate(calls) if "nanohpc-ssh-cancel" in call)
            self.assertEqual(len(holds), 2)
            self.assertTrue(all(index < prepared < installed < canceled for index in holds))

    def test_failed_fresh_root_login_leaves_ssh_undo_armed_and_node_drained(self) -> None:
        """A login failure must leave the automatic SSH recovery ready to fire."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bin_folder = folder / "bin"
            bin_folder.mkdir()
            write_command(bin_folder, "ssh", FAKE_PREVIEW_SSH)
            log = folder / "ssh.log"
            state = folder / "machine.json"
            state.write_text(json.dumps({"node_state": "IDLE"}))
            machine_facts = folder / "facts.json"
            machine_facts.write_text(json.dumps(facts()))
            environment = {
                **os.environ,
                "PATH": f"{bin_folder}:{os.environ['PATH']}",
                "UPDATE_SSH_LOG": str(log),
                "UPDATE_FAKE_STATE": str(state),
                "UPDATE_FAKE_FACTS": str(machine_facts),
                "UPDATE_FAKE_FAIL_LOGIN": "root",
                "XDG_STATE_HOME": str(folder / "state"),
            }
            command = [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "update",
                str(ROOT / "examples/cluster.yml"),
                "cpu1",
                "--include",
                "ssh",
            ]
            preview = subprocess.run(
                [*command, "--dry-run"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
            log.write_text("")
            result = subprocess.run(
                [*command, "--confirm", "cpu1"], capture_output=True, text=True, env=environment, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            after = json.loads(state.read_text())
            self.assertTrue(after["undo_armed"])
            self.assertEqual(after["node_state"], "DRAIN")
            calls = [json.loads(line)[1] for line in log.read_text().splitlines()]
            self.assertFalse(any("nanohpc-ssh-cancel" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
