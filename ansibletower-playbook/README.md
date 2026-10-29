# Ansible Tower playbooks

- `ubuntu-vm-slim.yml`: download and run the Ubuntu VM cleanup script.
- `ubuntu-update.yml`: update Ubuntu packages with an optional reboot.

## Ubuntu VM Slim

`ubuntu-vm-slim.yml` downloads `linux/ubuntu-vm-slim.sh` from this repository's
`main` branch onto each selected Ubuntu server, checks its Bash syntax and runs
it as root. By default it uses `--apply --yes` to perform the script's standard
cleanup without interactive prompts. Aggressive cleanup and snapd removal are
disabled by default.

The target needs Python 3, Bash and HTTPS access to `raw.githubusercontent.com`.
The connection account must be root or have permission to become root with sudo.

### Ansible Tower / AWX

1. Sync a Project using `https://github.com/chmajster/ChrisScriptBase.git` and
   branch `main`.
2. Create a Job Template with the target Inventory, a Machine credential and
   playbook `ansibletower-playbook/ubuntu-vm-slim.yml`.
3. Configure privilege escalation credentials if sudo requires a password.
4. Set the Limit to the servers to process, then launch the job. The script's
   output is shown in the job log; a nonzero exit code fails the job.

The play targets `all` hosts in the selected inventory, subject to the job's Limit.

### Command line

```bash
ansible-playbook -i inventory.ini ansibletower-playbook/ubuntu-vm-slim.yml \
  --limit ubuntu_servers --ask-become-pass
```

Omit `--ask-become-pass` when using root or passwordless sudo.

### Extra variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ubuntu_vm_slim_mode` | `apply` | `apply` performs cleanup; `status` only analyzes packages. |
| `ubuntu_vm_slim_aggressive` | `false` | Adds `--aggressive` to remove desktop and peripheral packages. |
| `ubuntu_vm_slim_remove_snapd` | `false` | Adds `--remove-snapd`; the script removes snapd only if no snaps are installed. |
| `ubuntu_vm_slim_ref` | `main` | Git branch, tag or commit to download. |
| `ubuntu_vm_slim_path` | `/usr/local/sbin/ubuntu-vm-slim` | Absolute destination on the remote server; its parent directory must exist. |

For package analysis in Tower, use these Extra Variables with Job Type **Run**:

```yaml
ubuntu_vm_slim_mode: status
```

The script's final validation requires `openssh-server` even in `status` mode.
If it is missing, the script returns an error and the job fails.

This mode still downloads the script onto the server. Job Type **Check** (CLI
`--check`) only validates the playbook's assertions and download URL; it skips
script execution.

To enable aggressive cleanup, set:

```yaml
ubuntu_vm_slim_aggressive: true
```

Every Run job executes the script, even when the downloaded file is unchanged.
The execution task reports a change in `apply` mode and no change in `status` mode.

## Ubuntu updates

`ubuntu-update.yml` refreshes the APT package index when it is older than one
hour, then runs the equivalent of `apt-get dist-upgrade` to update installed
packages and resolve dependency changes. It updates the current Ubuntu release;
it does not run a release upgrade such as Ubuntu 24.04 to 26.04.

The controller or Tower execution environment needs ansible-core 2.12 or newer.
The target needs Python 3, `python3-apt`, working APT repositories and root or
sudo access. The playbook processes one server at a time (`serial: 1`).

### Ansible Tower / AWX

Use the same Project, Inventory and Machine credential setup described above,
but select `ansibletower-playbook/ubuntu-update.yml` in the Job Template.
Set a Limit to select the Ubuntu servers to update.

### Command line

```bash
ansible-playbook -i inventory.ini ansibletower-playbook/ubuntu-update.yml \
  --limit ubuntu_servers --ask-become-pass
```

To preview package changes, add `--check --diff` or use Job Type **Check** in
Tower. The preview uses the existing package index and the current reboot marker.

### Optional reboot

By default `ubuntu_update_reboot` is `false`: the playbook reports whether
`/var/run/reboot-required` exists and leaves rebooting to the operator.
To reboot automatically when Ubuntu requests it, set these Extra Variables:

```yaml
ubuntu_update_reboot: true
```

When enabled, Ansible waits for the server to restart and accept commands before
moving on to the next host. Omit `--ask-become-pass` with root or passwordless sudo.
