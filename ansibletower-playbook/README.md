# Ansible Tower playbooks

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

This mode still downloads the script onto the server. Job Type **Check** (CLI
`--check`) only validates the playbook's assertions and download URL; it skips
script execution.

To enable aggressive cleanup, set:

```yaml
ubuntu_vm_slim_aggressive: true
```

Every Run job executes the script, even when the downloaded file is unchanged.
The execution task reports a change in `apply` mode and no change in `status` mode.
