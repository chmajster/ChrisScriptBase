"""Run the real script with shell fixtures for every mutating system command.

Run as root on Ubuntu. The mkdir fixture redirects backups into each test's
temporary directory. Shutdown, package operations and service changes are mocked.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ubuntu-vm-slim.sh"

FIXTURES = r'''
vm_slim_log() { printf '%s\n' "$*" >> "$VM_SLIM_TEST_LOG"; }
dpkg() {
    if [[ "$1" == --print-architecture ]]; then printf 'amd64\n'; fi
    return 0
}
dpkg-query() {
    case "$*" in
        *'${Status}'*) printf 'install ok installed\n' ;;
        *'${Installed-Size}\t'*) printf '1024\topenssh-server\t1.0\n' ;;
        *'${Installed-Size}'*) printf '1024\n' ;;
        *) printf 'openssh-server\nnano\n' ;;
    esac
}
systemd-detect-virt() { printf 'kvm\n'; }
systemctl() {
    vm_slim_log "systemctl $*"
    if [[ "${VM_SLIM_FAIL_FINAL:-0}" == 1 && "$1" == is-active && "${3:-}" == ssh ]]; then
        return 1
    fi
    return 0
}
apt-get() {
    vm_slim_log "apt-get $*"
    if [[ "${VM_SLIM_FAIL_CLEANUP:-0}" == 1 && "$1" == autoremove ]]; then return 1; fi
    return 0
}
apt-mark() { vm_slim_log "apt-mark $*"; }
mkdir() {
    BACKUP_DIR="$VM_SLIM_TEST_DIR/backup"
    command mkdir -p "$BACKUP_DIR"
}
cp() { vm_slim_log "cp $*"; }
rm() { vm_slim_log "rm $*"; }
journalctl() { vm_slim_log "journalctl $*"; }
df() { printf 'Test filesystem\n'; }
ip() { printf 'default via 192.0.2.1\n'; }
shutdown() {
    vm_slim_log "shutdown $*"
    [[ "${VM_SLIM_FAIL_SHUTDOWN:-0}" != 1 ]]
}
'''


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "Requires root on Ubuntu")
class VmSlimPostActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-slim-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.log = root / "calls.log"
        self.fixture = root / "fixtures.bash"
        self.fixture.write_text(FIXTURES)
        self.environment = {
            **os.environ, "BASH_ENV": str(self.fixture), "NO_COLOR": "1",
            "VM_SLIM_TEST_DIR": str(root), "VM_SLIM_TEST_LOG": str(self.log),
        }

    def run_script(self, *arguments, answer="", **environment):
        return subprocess.run(["bash", str(SCRIPT), *arguments], input=answer,
                              capture_output=True, text=True, timeout=15,
                              env={**self.environment, **environment})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def power_calls(self):
        return [line for line in self.calls() if line.startswith("shutdown ")]

    def test_default_apply_leaves_vm_running(self):
        result = self.run_script("--apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.power_calls())

    def test_reboot_is_scheduled_after_final_checks(self):
        result = self.run_script("--apply", "--yes", "--post-action", "reboot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.power_calls()), 1)
        self.assertTrue(self.power_calls()[0].startswith("shutdown -r +1 "))
        self.assertEqual(self.calls()[-1], self.power_calls()[0])
        self.assertLess(self.calls().index("systemctl is-active --quiet ssh"), len(self.calls()) - 1)

    def test_shutdown_equals_form_schedules_poweroff(self):
        result = self.run_script("--apply", "--yes", "--post-action=shutdown")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.power_calls()), 1)
        self.assertTrue(self.power_calls()[0].startswith("shutdown -P +1 "))

    def test_status_rejects_power_actions_before_system_operations(self):
        for action in ("reboot", "shutdown"):
            with self.subTest(action=action):
                result = self.run_script("--status", "--post-action", action)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("wymaga --apply", result.stderr)
                self.assertFalse(self.calls())

    def test_last_mode_status_also_rejects_power_action(self):
        result = self.run_script("--apply", "--post-action", "reboot", "--status")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.calls())

    def test_invalid_or_missing_action_is_rejected(self):
        for arguments in (("--post-action",), ("--post-action", "invalid"), ("--post-action=",)):
            with self.subTest(arguments=arguments):
                result = self.run_script("--apply", *arguments)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.calls())

    def test_status_with_none_does_not_schedule_anything(self):
        result = self.run_script("--status", "--post-action", "none")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.power_calls())

    def test_cancelled_cleanup_does_not_schedule_reboot(self):
        result = self.run_script("--apply", "--post-action", "reboot", answer="n\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Operacja anulowana", result.stdout)
        self.assertFalse(self.power_calls())

    def test_cleanup_failure_does_not_schedule_shutdown(self):
        result = self.run_script("--apply", "--yes", "--post-action", "shutdown", VM_SLIM_FAIL_CLEANUP="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.power_calls())

    def test_failed_final_check_does_not_schedule_reboot(self):
        result = self.run_script("--apply", "--yes", "--post-action", "reboot", VM_SLIM_FAIL_FINAL="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("ssh.service nie działa", result.stderr)
        self.assertFalse(self.power_calls())

    def test_shutdown_command_failure_fails_script(self):
        result = self.run_script("--apply", "--yes", "--post-action", "shutdown", VM_SLIM_FAIL_SHUTDOWN="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Nie udało się zaplanować akcji", result.stderr)
        self.assertNotIn("[ OK ] Gotowe.", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
