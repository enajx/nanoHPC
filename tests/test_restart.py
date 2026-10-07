"""The confirmed compute restart through the public nanoHPC command."""

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

FAKE_SSH = f"""#!{sys.executable}
import json, os, signal, sys
args = sys.argv[1:]
index = 0
login = ""
while args[index].startswith("-"):
    if args[index] == "-l":
        login = args[index + 1]
    index += 2 if args[index] in ("-F", "-o", "-l") else 1
target, command = args[index], args[index + 1]
path = os.environ["RESTART_STATE"]
with open(path) as file:
    state = json.load(file)
state["calls"].append([target, command])
if "/run/nanohpc/maintenance.lock" in command and "python3 -u -c" in command:
    if state["lock"]:
        with open(path, "w") as file:
            json.dump(state, file)
        print("BUSY", flush=True)
        sys.exit(2)
    state["lock"] = True
    state["lock_pid"] = os.getpid()
    with open(path, "w") as file:
        json.dump(state, file)
    print("READY", flush=True)
    sys.stdin.buffer.read()
    with open(path) as file:
        state = json.load(file)
    state["lock"] = False
    state["calls"].append([target, "maintenance holder released"])
    with open(path, "w") as file:
        json.dump(state, file)
    sys.exit(0)
code = 0
output = ""
if "create reservation" in command:
    state["reservation"] = True
elif "delete ReservationName=" in command:
    state["reservation"] = False
elif "scontrol show node" in command:
    output = "NodeName=" + state["machine"] + " State=" + state["node_state"] + " Reason=" + state.get("reason", "none") + "\\n"
elif "ip -4 -j addr show" in command:
    addresses = {{"front": "192.168.104.10", "gpu4": "192.168.104.11",
                  "cpu1": "192.168.104.13", "gpu4i": "192.168.104.14"}}
    address = addresses[target]
    if target == state["machine"]:
        address = state.get("wrong_address", address)
        if state.get("wrong_address_after_reboot") and state["boot_id"] == "new-boot-id":
            address = state["wrong_address_after_reboot"]
    output = json.dumps([{{"addr_info": [{{"family": "inet", "local": address}}]}}]) + "\\n"
elif command.strip() == "id -un":
    output = state.get("operator", "alice") + "\\n"
elif "state=drain" in command:
    if state.get("fail_redrain") and state.get("resumed"):
        code = 1
    else:
        state["node_state"] = "DRAIN"
        if state.get("drop_lock_on") == "drain":
            os.kill(state["lock_pid"], signal.SIGKILL)
elif "state=resume" in command:
    state["node_state"] = "IDLE"
    state["resumed"] = True
    if state.get("drop_lock_on") == "resume":
        os.kill(state["lock_pid"], signal.SIGKILL)
elif "squeue" in command:
    output = "\\n".join(state["running_jobs"]) + ("\\n" if state["running_jobs"] else "")
    state["running_jobs"] = []
elif "python3 -c" in command:
    output = json.dumps(state["facts"]) + "\\n"
elif "cat /proc/sys/kernel/random/boot_id" in command:
    output = state["boot_id"] + "\\n"
elif "systemctl reboot" in command:
    if state.get("reboot_error"):
        code = 255
        sys.stderr.write(state["reboot_error"] + "\\n")
    else:
        state["boot_id"] = "new-boot-id"
elif "uname -r" in command:
    output = "6.8.0-test\\n"
elif "hostname -s" in command and "sbatch" not in command and "srun" not in command:
    output = state.get("hostname", state["machine"]) + "\\n"
elif "sbatch" in command:
    output = "123\\n"
elif "srun" in command:
    if "</dev/null" in command:
        code = 1
    else:
        count = state.get("interactive_gpu_count", 1)
        output = state.get("hostname", state["machine"]) + "\\n" + "".join(
            "GPU " + str(number) + ": Fake GPU\\n" for number in range(count)
        )
elif "nvidia-smi -L" in command:
    if state.get("gpu_states"):
        output = state["gpu_states"].pop(0)
    else:
        output = "".join("GPU " + str(number) + ": Fake GPU\\n" for number in range(4))
elif "findmnt" in command and "/home" in command:
    output = "192.168.104.10:/home\\n"
elif "findmnt" in command and "/scratch" in command:
    output = "/scratch\\n"
elif "systemctl is-active slurmd" in command:
    service = state["slurmd_states"].pop(0) if state["slurmd_states"] else "active"
    output = service + "\\n"
    if service != "active":
        code = 3
elif "sacct" in command:
    if state.get("accounting_states"):
        output = state["accounting_states"].pop(0)
    else:
        output = state["job_state"] + "|" + state["machine"] + "\\n"
elif command.strip() == "true":
    if login == state.get("fail_login"):
        code = 255
else:
    code = 3
    sys.stderr.write("unexpected fake SSH command: " + command[:200] + "\\n")
with open(path, "w") as file:
    json.dump(state, file)
sys.stdout.write(output)
sys.exit(code)
"""


class RestartCommandTest(unittest.TestCase):
    """Invalid restart requests stop before they contact any machine."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        write_command(bin_folder, "ssh", 'echo contacted >> "$RESTART_SSH_LOG"\nexit 1\n')
        self.log = self.folder / "ssh.log"
        self.environment = {
            **os.environ,
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "RESTART_SSH_LOG": str(self.log),
        }

    def run_restart(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the installed command with the six-machine example and fake SSH on PATH."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "restart",
                str(ROOT / "examples/cluster.yml"),
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def test_front_node_is_refused_before_ssh(self) -> None:
        result = self.run_restart("front", "--confirm", "front")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compute", result.stderr.lower())
        self.assertFalse(self.log.exists())

    def test_missing_confirmation_is_refused_before_ssh(self) -> None:
        result = self.run_restart("gpu4")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirm", result.stderr.lower())
        self.assertFalse(self.log.exists())

    def test_two_machine_names_are_refused_before_ssh(self) -> None:
        result = self.run_restart("gpu4", "cpu1", "--confirm", "gpu4")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())


class RestartFlowTest(unittest.TestCase):
    """A fake SSH machine checks the CLI's visible state changes without starting a VM."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        bin_folder = self.folder / "bin"
        bin_folder.mkdir()
        write_command(bin_folder, "ssh", FAKE_SSH)
        self.state_path = self.folder / "state.json"
        self.state = {
            "calls": [],
            "lock": False,
            "node_state": "IDLE",
            "running_jobs": [],
            "facts": facts(),
            "boot_id": "old-boot-id",
            "slurmd_states": [],
            "job_state": "COMPLETED",
            "machine": "gpu4",
            "reservation": False,
        }
        self.state_path.write_text(json.dumps(self.state))
        self.environment = {
            **os.environ,
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "RESTART_STATE": str(self.state_path),
        }

    def restart(self, machine: str) -> subprocess.CompletedProcess[str]:
        """Run a confirmed restart through the installed CLI."""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "restart",
                str(ROOT / "examples/cluster.yml"),
                machine,
                "--confirm",
                machine,
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def test_failed_precheck_leaves_node_drained_without_reboot(self) -> None:
        self.state["facts"]["fstab"] += "UUID=missing /unsafe ext4 defaults 0 2\n"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/unsafe has no nofail", result.stderr)
        self.assertEqual(state["node_state"], "DRAIN")
        self.assertEqual(state["boot_id"], "old-boot-id")
        self.assertFalse(state["lock"])

    def test_update_drained_node_can_complete_required_restart(self) -> None:
        """A successful update can hand its drained node to the existing restart command."""
        self.state["node_state"] = "DRAIN"
        self.state["reason"] = "nanohpc-update-restart-required"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["boot_id"], "new-boot-id")
        self.assertEqual(state["node_state"], "IDLE")

    def test_success_reboots_then_tests_before_finishing(self) -> None:
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["boot_id"], "new-boot-id")
        self.assertEqual(state["node_state"], "IDLE")
        calls = [command for _, command in state["calls"]]
        self.assertLess(
            next(i for i, command in enumerate(calls) if "state=drain" in command),
            next(i for i, command in enumerate(calls) if "systemctl reboot" in command),
        )
        self.assertLess(
            next(i for i, command in enumerate(calls) if "systemctl reboot" in command),
            next(i for i, command in enumerate(calls) if "sbatch" in command),
        )
        created = next(i for i, command in enumerate(calls) if "create reservation" in command)
        resumed = next(i for i, command in enumerate(calls) if "state=resume" in command)
        smoked = next(i for i, command in enumerate(calls) if "sbatch" in command)
        released = next(i for i, command in enumerate(calls) if "delete ReservationName=" in command)
        unlocked = next(i for i, command in enumerate(calls) if "maintenance holder released" in command)
        self.assertLess(created, resumed)
        self.assertLess(resumed, smoked)
        self.assertLess(smoked, released)
        self.assertLess(released, unlocked)
        self.assertIn("--reservation=nanohpc_restart_gpu4", calls[smoked])
        self.assertFalse(state["reservation"])
        self.assertFalse(state["lock"])
        self.assertFalse(any("/run/nanohpc-restart.lock" in command for command in calls))

    def test_lost_maintenance_lock_stops_before_reboot_or_resume(self) -> None:
        self.state["drop_lock_on"] = "drain"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        calls = [command for _, command in state["calls"]]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("maintenance lock holder closed", result.stderr)
        self.assertEqual(state["node_state"], "DRAIN")
        self.assertFalse(any("systemctl reboot" in command for command in calls))
        self.assertFalse(any("state=resume" in command for command in calls))

    def test_lost_lock_after_temporary_resume_redrains_without_removing_reservation(self) -> None:
        self.state["drop_lock_on"] = "resume"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        calls = [command for _, command in state["calls"]]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("maintenance lock holder closed", result.stderr)
        self.assertEqual(state["node_state"], "DRAIN")
        self.assertTrue(state["reservation"])
        self.assertFalse(any("delete ReservationName=" in command for command in calls))

    def test_failed_emergency_redrain_keeps_reservation(self) -> None:
        self.state["drop_lock_on"] = "resume"
        self.state["fail_redrain"] = True
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        calls = [command for _, command in state["calls"]]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("emergency redrain failed", result.stderr)
        self.assertEqual(state["node_state"], "IDLE")
        self.assertTrue(state["reservation"])
        self.assertFalse(any("delete ReservationName=" in command for command in calls))

    def test_cpu_only_node_runs_a_cpu_slurm_job(self) -> None:
        self.state["machine"] = "cpu1"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("cpu1")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        smoke = next(command for _, command in state["calls"] if "sbatch" in command)
        self.assertIn("hostname -s", smoke)
        self.assertNotIn("--gpus", smoke)

    def test_running_job_finishes_before_precheck_and_reboot(self) -> None:
        self.state["running_jobs"] = ["42"]
        self.state_path.write_text(json.dumps(self.state))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc import restart; restart.POLL_JOBS_SECONDS=0; from nanohpc.cli import main; main()",
                "restart",
                str(ROOT / "examples/cluster.yml"),
                "gpu4",
                "--confirm",
                "gpu4",
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [command for _, command in json.loads(self.state_path.read_text())["calls"]]
        self.assertLess(
            next(i for i, command in enumerate(calls) if "squeue" in command),
            next(i for i, command in enumerate(calls) if "python3 -c" in command),
        )
        self.assertIn("waiting for running jobs: 42", result.stdout)

    def test_job_wait_includes_every_allocated_job_state(self) -> None:
        result = self.restart("gpu4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [command for _, command in json.loads(self.state_path.read_text())["calls"]]
        query = next(command for command in calls if "squeue" in command)
        self.assertNotIn(" -t ", query)

    def test_failed_smoke_job_leaves_node_drained(self) -> None:
        self.state["job_state"] = "FAILED"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["boot_id"], "new-boot-id")
        self.assertEqual(state["node_state"], "DRAIN")
        self.assertFalse(state["reservation"])
        self.assertIn("test job 123 finished as FAILED", result.stderr)

    def test_failed_redrain_keeps_reservation(self) -> None:
        self.state["job_state"] = "FAILED"
        self.state["fail_redrain"] = True
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["node_state"], "IDLE")
        self.assertTrue(state["reservation"], state["calls"])
        self.assertIn("could not leave gpu4 drained", result.stderr)

    def test_slurmd_can_become_active_after_boot(self) -> None:
        self.state["slurmd_states"] = ["inactive", "active"]
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_gpu_driver_can_become_ready_after_boot(self) -> None:
        self.state["gpu_states"] = ["", "".join(f"GPU {number}: Fake GPU\n" for number in range(4))]
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_test_job_accounting_can_arrive_after_completion(self) -> None:
        self.state["accounting_states"] = ["", "COMPLETED|gpu4\n"]
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_accounting_can_show_running_briefly_after_job_exits(self) -> None:
        self.state["accounting_states"] = ["RUNNING|gpu4\n", "COMPLETED|gpu4\n"]
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_lock_refuses_a_second_restart(self) -> None:
        self.state["lock"] = True
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["node_state"], "IDLE")
        self.assertEqual(state["boot_id"], "old-boot-id")

    def test_existing_drain_is_left_alone(self) -> None:
        self.state["node_state"] = "DRAIN"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state["boot_id"], "old-boot-id")
        self.assertEqual(state["node_state"], "DRAIN")
        self.assertFalse(any("systemctl reboot" in command for _, command in state["calls"]))

    def test_wrong_ssh_target_is_refused_before_drain(self) -> None:
        self.state["wrong_address"] = "192.168.104.12"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 1)
        self.assertIn("configured address", result.stderr)
        self.assertEqual(state["node_state"], "IDLE")
        self.assertFalse(any("systemctl reboot" in command for _, command in state["calls"]))

    def test_ssh_target_must_still_be_correct_after_reboot(self) -> None:
        self.state["wrong_address_after_reboot"] = "192.168.104.12"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 1)
        self.assertIn("configured address", result.stderr)
        self.assertEqual(state["node_state"], "DRAIN")
        self.assertFalse(any("state=resume" in command for _, command in state["calls"]))

    def test_reboot_auth_failure_is_reported_without_waiting(self) -> None:
        self.state["reboot_error"] = "Permission denied (publickey)."
        self.state_path.write_text(json.dumps(self.state))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc import restart; restart.WAIT_BOOT_SECONDS=0; from nanohpc.cli import main; main()",
                "restart",
                str(ROOT / "examples/cluster.yml"),
                "gpu4",
                "--confirm",
                "gpu4",
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("reboot request failed: Permission denied", result.stderr)

    def test_interactive_only_node_gets_a_slurm_test_job(self) -> None:
        self.state["machine"] = "gpu4i"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4i")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["node_state"], "IDLE")
        self.assertTrue(any("srun" in command and "interactive" in command for _, command in state["calls"]))

    def test_interactive_job_can_see_unrestricted_gpu_listing(self) -> None:
        self.state["machine"] = "gpu4i"
        self.state["interactive_gpu_count"] = 4
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4i")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_interactive_node_can_have_a_different_hostname(self) -> None:
        self.state["machine"] = "gpu4i"
        self.state["hostname"] = "worker-forty-four"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4i")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invoking_administrator_gets_a_fresh_login(self) -> None:
        config = self.folder / "cluster.yml"
        config.write_text(
            (ROOT / "examples/cluster.yml").read_text().replace("admins: [alice]", "admins: [alice, bob]")
        )
        self.state["operator"] = "bob"
        self.state["fail_login"] = "alice"
        self.state_path.write_text(json.dumps(self.state))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nanohpc.cli import main; main()",
                "restart",
                str(config),
                "gpu4",
                "--confirm",
                "gpu4",
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["node_state"], "IDLE")
        calls = [command for _, command in state["calls"]]
        self.assertTrue(any("runuser -u bob" in command for command in calls))
        self.assertFalse(any("-l alice" in command for command in calls))

    def test_non_administrator_is_refused_before_drain(self) -> None:
        self.state["operator"] = "bob"
        self.state_path.write_text(json.dumps(self.state))
        result = self.restart("gpu4")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an administrator", result.stderr)
        self.assertEqual(state["node_state"], "IDLE")


if __name__ == "__main__":
    unittest.main()
