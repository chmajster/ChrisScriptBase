"""Exercise the real playbook against simulated Kubernetes and HTTP endpoints."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PLAYBOOK = Path(__file__).resolve().parents[1] / "update_awx.yaml"
ANSIBLE = shutil.which("ansible-playbook")

KUBECTL = r'''
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
args = sys.argv[2:]
state = json.loads(path.read_text())
event = {"args": args}
output = ""
if args[:3] == ["apply", "-f", "-"]:
    event["manifest"] = json.load(sys.stdin)
    output = "awxbackup.awx.ansible.com/backup created"
elif args[:2] == ["apply", "-k"]:
    state["applied"] = True
    output = "deployment.apps/awx-operator-controller-manager configured"
elif args[:2] == ["get", "awx"]:
    output = json.dumps({"spec": {"service_type": "NodePort", "nodeport_port": 30080,
                                  "projects_persistence": True}})
elif args[:2] == ["get", "awxbackup"]:
    status = {"backupDirectory": "/backups/complete", "backupClaim": "awx-backup-claim"}
    output = json.dumps({"status": status if state["backup_ready"] else {}})
elif args[:2] == ["get", "deployment"]:
    name = args[2]
    if name == "awx-operator-controller-manager":
        version = state["target"] if state["applied"] else state["installed"]
        container = {"name": "awx-manager", "image": "quay.io/ansible/awx-operator:" + version}
    else:
        version = state["target_awx"] if state["applied"] and state["images_ready"] else "24.5.0"
        container = {"name": name, "image": "quay.io/ansible/awx:" + version}
    output = json.dumps({"spec": {"template": {"spec": {"containers": [container]}}}})
elif args[0] == "exec":
    output = state["target_awx"] if state["applied"] or state["installed"] == state["target"] else "24.5.0"
elif args[0] in ("rollout", "annotate"):
    output = "completed"
elif args[:2] == ["get", "pods,statefulsets,deployments,svc,pvc,awxbackups"]:
    output = "Simulated Kubernetes resource diagnostics"
else:
    raise SystemExit("Unexpected kubectl invocation: " + repr(args))
state["events"].append(event)
path.write_text(json.dumps(state))
print(output)
'''


@unittest.skipUnless(ANSIBLE and os.name == "posix", "Requires Linux and ansible-playbook")
class UpdateAwxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update-awx-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.state_path.write_text(json.dumps({
            "installed": "2.18.0", "target": "2.19.1", "target_awx": "24.6.1",
            "applied": False, "backup_ready": True, "images_ready": True, "events": [],
        }))
        self.kubectl = self.root / "kubectl.py"
        self.kubectl.write_text(KUBECTL)
        state_path = self.state_path

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state = json.loads(state_path.read_text())
                if self.path.startswith("/releases/"):
                    tag = state["target"] if self.path.endswith("/latest") else self.path.rsplit("/", 1)[-1]
                    response = {"tag_name": tag, "prerelease": False, "draft": False}
                elif self.path == "/api/v2/ping/":
                    current = state["target_awx"] if state["installed"] == state["target"] else "24.5.0"
                    response = {"version": state["target_awx"] if state["applied"] else current}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.workdir = self.root / "awx"

    def state(self, **updates):
        state = json.loads(self.state_path.read_text())
        if updates:
            state.update(updates)
            self.state_path.write_text(json.dumps(state))
        return state

    def run_playbook(self, check=False, **overrides):
        url = f"http://127.0.0.1:{self.server.server_port}"
        variables = {
            "ansible_become": False, "ansible_python_interpreter": sys.executable,
            "awx_kubectl": [sys.executable, str(self.kubectl), str(self.state_path)],
            "awx_operator_releases_url": url + "/releases", "awx_api_url": url,
            "awx_workdir": str(self.workdir), "awx_wait_retries": 1, "awx_wait_delay": 0,
        }
        variables.update(overrides)
        varfile = self.root / "variables.json"
        varfile.write_text(json.dumps(variables))
        command = [ANSIBLE, "-i", "localhost,", str(PLAYBOOK), "-e", "@" + str(varfile)]
        if check:
            command.append("--check")
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                              timeout=120, env={**os.environ, "ANSIBLE_NOCOLOR": "1"})

    def test_up_to_date_installation_does_not_create_backup_or_apply(self):
        self.state(installed="2.19.1")
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.workdir.exists())
        self.assertFalse(any(e["args"][0] == "apply" for e in self.state()["events"]))

    def test_check_mode_only_runs_preflight(self):
        result = self.run_playbook(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.workdir.exists())
        self.assertTrue(all(e["args"][0] in ("get", "exec") for e in self.state()["events"]))

    def test_backup_timeout_blocks_operator_changes(self):
        self.state(backup_ready=False)
        result = self.run_playbook()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.state()["applied"])
        self.assertEqual(len(list(self.workdir.rglob("diagnostics.txt"))), 1)

    def test_stale_deployment_images_prevent_success_report(self):
        self.state(images_ready=False)
        result = self.run_playbook()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.state()["applied"])
        self.assertFalse(list(self.workdir.rglob("upgrade-report.txt")))

    def test_downgrade_is_rejected_before_backup(self):
        result = self.run_playbook(awx_operator_version="2.17.0")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.workdir.exists())
        self.assertIn("Downgrades are rejected", result.stdout)

    def test_upgrade_waits_for_backup_and_preserves_existing_spec(self):
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.state()["events"]
        backup_index = next(i for i, e in enumerate(events) if "manifest" in e)
        apply_index = next(i for i, e in enumerate(events) if e["args"][:2] == ["apply", "-k"])
        backup_status_index = next(i for i, e in enumerate(events) if e["args"][:2] == ["get", "awxbackup"])
        self.assertLess(backup_index, backup_status_index)
        self.assertLess(backup_status_index, apply_index)
        self.assertEqual(events[backup_index]["manifest"]["kind"], "AWXBackup")
        self.assertFalse(any(e["args"][0] == "delete" for e in events))
        self.assertEqual(sum("manifest" in e for e in events), 1)
        snapshot = json.loads(next(self.workdir.rglob("awx-before.json")).read_text())
        self.assertTrue(snapshot["spec"]["projects_persistence"])
        report = next(self.workdir.rglob("upgrade-report.txt"))
        self.assertIn("AWX: 24.5.0 -> 24.6.1", report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
