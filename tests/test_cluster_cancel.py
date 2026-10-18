"""Stop running jobs with fake Slurm commands and a local Slack API."""

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from tests.test_user_check_notify import FakeHTTP

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "src/nanohpc/files/cluster-cancel"
ROLE = ROOT / "src/nanohpc/ansible/roles/alerts/tasks/main.yml"

FAKE_SLURM = """#!{python}
import json, sys
jobs_file, log_file = {jobs!r}, {log!r}
with open(jobs_file) as stream:
    jobs = json.load(stream)
args = sys.argv[1:]
if args[:3] == ["show", "job", "-o"]:
    print(" ".join(f"{{key}}={{value}}" for key, value in jobs[args[3]].items()))
    sys.exit(0)
with open(log_file, "a") as stream:
    stream.write(" ".join([sys.argv[0].rsplit("/", 1)[-1]] + args) + "\\n")
if args[0] == "requeue" and jobs[args[1]].get("RequeueCommandFails") == "1":
    sys.exit(1)
if args[0] == "update":
    job, setting = args[1].split("=", 1)[1], args[2].split("=", 1)
    jobs[job][setting[0]] = setting[1]
elif args[0] == "requeue":
    jobs[args[1]]["JobState"] = "PENDING"
else:
    jobs[args[0]]["JobState"] = "CANCELLED"
with open(jobs_file, "w") as stream:
    json.dump(jobs, stream)
"""


def job(job_id: str, user: str, state: str, requeue: str, mail: str | None) -> dict[str, str]:
    """Return the fields printed by `scontrol show job -o`."""
    fields = {"JobId": job_id}
    if "_" in job_id:
        fields = {"JobId": "1084", "ArrayJobId": job_id.split("_")[0], "ArrayTaskId": job_id.split("_")[1]}
    fields |= {
        "JobName": "run",
        "UserId": f"{user}(1009)",
        "JobState": state,
        "Requeue": requeue,
        "BatchFlag": "1",
        "NodeList": "gpu1",
    }
    if mail:
        fields |= {"MailUser": "slack", "MailType": mail}
    return fields


class ClusterCancelTest(unittest.TestCase):
    """Each stopped job gets one private notice with its next action."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="cluster-cancel-"))
        self.addCleanup(shutil.rmtree, self.folder)
        self.http = FakeHTTP([])
        self.addCleanup(self.http.close)
        self.token = self.folder / "bot-token"
        self.token.write_text("xoxb-test-token\n")
        self.jobs = self.folder / "jobs.json"
        self.log = self.folder / "calls.log"
        self.log.write_text("")
        for name in ("scontrol", "scancel"):
            fake = self.folder / name
            fake.write_text(FAKE_SLURM.format(python=sys.executable, jobs=str(self.jobs), log=str(self.log)))
            fake.chmod(0o755)

    def cancel(self, jobs: dict[str, dict[str, str]], *ids: str) -> subprocess.CompletedProcess[str]:
        """Call the public command with fake Slurm and Slack services."""
        self.jobs.write_text(json.dumps(jobs))
        return subprocess.run(
            [
                sys.executable,
                str(COMMAND),
                "--bot-token-file",
                str(self.token),
                "--slack-api",
                self.http.url,
                "--scontrol",
                str(self.folder / "scontrol"),
                "--scancel",
                str(self.folder / "scancel"),
                "--user",
                "eleni:U0ELENI",
                "--user",
                "noah:",
                "--reason",
                "to fix an issue with power",
                *ids,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_requeues_running_jobs_and_restores_end_notice(self) -> None:
        result = self.cancel(
            {
                "1099": job("1099", "eleni", "RUNNING", "1", "END"),
                "1083_0": job("1083_0", "eleni", "RUNNING", "1", None),
            },
            "1099",
            "1083_0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text().splitlines(),
            [
                "scontrol update JobId=1099 MailType=NONE",
                "scontrol requeue 1099",
                "scontrol update JobId=1099 MailType=END",
                "scontrol requeue 1083_0",
            ],
        )
        self.assertEqual([call["channel"] for call in self.http.private_messages], ["U0ELENI", "U0ELENI"])
        self.assertIn("[No action required]", self.http.private_messages[0]["text"])
        self.assertIn("job 1083_0", self.http.private_messages[1]["text"])

    def test_cancels_unrequeueable_job_and_requires_user_action(self) -> None:
        result = self.cancel({"1200": job("1200", "eleni", "RUNNING", "0", "END")}, "1200")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text().splitlines(), ["scontrol update JobId=1200 MailType=NONE", "scancel 1200"]
        )
        self.assertIn("[Action required]", self.http.private_messages[0]["text"])
        self.assertIn("submit it again", self.http.private_messages[0]["text"])

    def test_requeue_command_failure_cancels_and_tells_owner(self) -> None:
        fields = job("1201", "eleni", "RUNNING", "1", "END")
        fields["RequeueCommandFails"] = "1"
        result = self.cancel({"1201": fields}, "1201")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text().splitlines(),
            ["scontrol update JobId=1201 MailType=NONE", "scontrol requeue 1201", "scancel 1201"],
        )
        self.assertIn("[Action required]", self.http.private_messages[0]["text"])

    def test_refuses_all_jobs_if_one_is_not_running(self) -> None:
        result = self.cancel(
            {"1099": job("1099", "eleni", "RUNNING", "1", None), "1300": job("1300", "eleni", "PENDING", "1", None)},
            "1099",
            "1300",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("job 1300 is PENDING, not running", result.stderr)
        self.assertEqual(self.log.read_text(), "")
        self.assertEqual(self.http.private_messages, [])

    def test_missing_slack_id_and_rejected_post_are_errors(self) -> None:
        result = self.cancel({"1400": job("1400", "noah", "RUNNING", "1", None)}, "1400")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no Slack ID for noah", result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ["scontrol requeue 1400"])
        self.http.private_ok = False
        result = self.cancel({"1500": job("1500", "eleni", "RUNNING", "1", None)}, "1500")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not_in_channel", result.stderr)

    def test_installation_and_required_reason(self) -> None:
        tasks = yaml.safe_load(ROLE.read_text())
        program = next(
            task["ansible.builtin.copy"]
            for task in tasks
            if task.get("name") == "Install the cluster-cancel job stopper"
        )
        self.assertEqual(program["src"], "{{ nanohpc.package_files }}/cluster-cancel")
        self.assertEqual(program["dest"], "/usr/local/bin/nanohpc-cluster-cancel")
        self.assertEqual(program["mode"], "0755")
        installed = next(task["ansible.builtin.copy"] for task in tasks if task.get("name") == "Install cluster-cancel")
        self.assertEqual(installed["dest"], "/usr/local/bin/cluster-cancel")
        self.assertEqual(installed["mode"], "0755")
        environment = Environment(undefined=StrictUndefined)
        environment.filters["quote"] = shlex.quote
        wrapper = environment.from_string(installed["content"]).render(
            nanohpc={"users": [{"name": "eleni", "slack_id": "U0ELENI"}, {"name": "noah"}]}
        )
        self.assertIn("--user eleni:U0ELENI", wrapper)
        self.assertIn("--user noah:", wrapper)
        self.assertIn("--bot-token-file /etc/nanohpc/alertmanager/slack-bot-token", wrapper)
        self.assertIn("--scontrol /usr/bin/scontrol --scancel /usr/bin/scancel", wrapper)
        result = subprocess.run(
            [sys.executable, str(COMMAND), "--bot-token-file", "x", "1099"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--reason", result.stderr)


if __name__ == "__main__":
    unittest.main()
