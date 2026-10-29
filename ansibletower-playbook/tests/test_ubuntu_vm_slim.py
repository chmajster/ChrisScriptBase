"""Run the playbook and real cleanup script with guarded system-command fixtures.

The download task is replaced by a local copy. All argument validation, command
rendering, check-mode handling and result reporting come from the real playbook.
"""

import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "ansibletower-playbook/ubuntu-vm-slim.yml"
SCRIPT = ROOT / "linux/ubuntu-vm-slim.sh"
ANSIBLE = shutil.which("ansible-playbook")
FIXTURES = runpy.run_path(str(ROOT / "linux/tests/test_ubuntu_vm_slim.py"))["FIXTURES"]


@unittest.skipUnless(ANSIBLE and os.name == "posix" and os.geteuid() == 0,
                     "Requires Ansible and root on Ubuntu")
class VmSlimPlaybookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-slim-playbook-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.log"
        self.fixture = self.root / "fixtures.bash"
        self.fixture.write_text(FIXTURES)
        self.script_destination = self.root / "downloaded script.sh"

        # Refuse to source the real script unless every mutating command is mocked.
        guard = self.root / "guard.sh"
        guard.write_text(
            '#!/usr/bin/env bash\nset -e\n'
            'for stub in apt-get apt-mark systemctl mkdir cp rm journalctl shutdown; do\n'
            '  [[ "$(type -t "$stub")" == function ]] || exit 99\n'
            'done\n'
            'vm_slim_log "script $*"\n'
            f'source {shlex.quote(str(SCRIPT))} "$@"\n'
        )
        playbook = yaml.safe_load(PLAYBOOK.read_text())
        for task in playbook[0]["tasks"]:
            if "ansible.builtin.get_url" in task:
                original = task.pop("ansible.builtin.get_url")
                task["ansible.builtin.copy"] = {
                    "src": str(guard), "dest": original["dest"],
                    "owner": original["owner"], "group": original["group"],
                    "mode": original["mode"],
                }
        self.playbook = self.root / "playbook.yml"
        self.playbook.write_text(yaml.safe_dump(playbook, sort_keys=False))

    def run_playbook(self, check=False, fail_cleanup=False, **overrides):
        variables = {
            "ansible_become": False, "ansible_python_interpreter": sys.executable,
            "ubuntu_vm_slim_path": str(self.script_destination),
        }
        variables.update(overrides)
        varfile = self.root / "variables.json"
        varfile.write_text(json.dumps(variables))
        command = [ANSIBLE, "-i", "localhost,", "-c", "local", str(self.playbook),
                   "-e", "@" + str(varfile)]
        if check:
            command.append("--check")
        environment = {
            **os.environ, "BASH_ENV": str(self.fixture), "ANSIBLE_NOCOLOR": "1",
            "VM_SLIM_TEST_DIR": str(self.root), "VM_SLIM_TEST_LOG": str(self.log),
            "VM_SLIM_FAIL_CLEANUP": "1" if fail_cleanup else "0",
            "VM_SLIM_FAIL_FINAL": "0", "VM_SLIM_FAIL_SHUTDOWN": "0",
        }
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                              timeout=90, env=environment)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def power_calls(self):
        return [line for line in self.calls() if line.startswith("shutdown ")]

    def test_default_apply_omits_post_action_and_leaves_vm_running(self):
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("script --apply --yes", self.calls())
        self.assertIn("apt-get autoremove --purge -y", self.calls())
        self.assertFalse(self.power_calls())

    def test_reboot_is_forwarded_to_script_and_reported_in_job_log(self):
        result = self.run_playbook(ubuntu_vm_slim_post_action="reboot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("script --apply --yes --post-action reboot", self.calls())
        self.assertEqual(len(self.power_calls()), 1)
        self.assertTrue(self.power_calls()[0].startswith("shutdown -r +1 "))
        self.assertIn("Zaplanowano reboot", result.stdout)

    def test_shutdown_is_forwarded_to_script(self):
        result = self.run_playbook(ubuntu_vm_slim_post_action="shutdown")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("script --apply --yes --post-action shutdown", self.calls())
        self.assertEqual(len(self.power_calls()), 1)
        self.assertTrue(self.power_calls()[0].startswith("shutdown -P +1 "))

    def test_status_with_none_only_analyzes_packages(self):
        result = self.run_playbook(ubuntu_vm_slim_mode="status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("script --status --yes", self.calls())
        self.assertNotIn("apt-get autoremove --purge -y", self.calls())
        self.assertFalse(self.power_calls())

    def test_status_rejects_power_actions_before_download(self):
        for action in ("reboot", "shutdown"):
            with self.subTest(action=action):
                result = self.run_playbook(ubuntu_vm_slim_mode="status", ubuntu_vm_slim_post_action=action)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.script_destination.exists())
                self.assertFalse(self.calls())

    def test_invalid_action_is_rejected_before_download(self):
        result = self.run_playbook(ubuntu_vm_slim_post_action="invalid")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.script_destination.exists())
        self.assertFalse(self.calls())

    def test_check_mode_never_executes_or_schedules_action(self):
        result = self.run_playbook(check=True, ubuntu_vm_slim_post_action="reboot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.script_destination.exists())
        self.assertFalse(self.calls())

    def test_cleanup_failure_fails_job_without_power_action(self):
        result = self.run_playbook(fail_cleanup=True, ubuntu_vm_slim_post_action="shutdown")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("script --apply --yes --post-action shutdown", self.calls())
        self.assertFalse(self.power_calls())


if __name__ == "__main__":
    unittest.main(verbosity=2)
