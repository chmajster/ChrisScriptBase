# Ansible Tower playbooks

- `ubuntu-vm-slim.yml`: download and run the Ubuntu VM cleanup script.
- `ubuntu-update.yml`: update Ubuntu packages with an optional reboot.
- `update_awx.yaml`: back up and upgrade an existing AWX installation on K3s.

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

## AWX updates on K3s

`update_awx.yaml` follows the installation layout in `initial_awx.yaml`:
namespace `awx`, instance `awx`, work directory `/opt/awx`, local-path storage and
NodePort `30080`. It upgrades AWX by installing the published AWX Operator release
that manages the corresponding AWX version, following the
[operator upgrade procedure](https://docs.ansible.com/projects/awx-operator/en/latest/upgrade/upgrading.html).

Run this playbook **on the K3s server, from outside the AWX instance being upgraded**.
An AWX job that upgrades its own instance can be interrupted as its task pod is
replaced. Use a maintenance window after existing jobs have finished.
The host needs Ansible, Python 3, Git, root access, `/usr/local/bin/k3s` and access
to GitHub and the container registries used by the operator.

```bash
sudo ansible-playbook -i localhost, ansibletower-playbook/update_awx.yaml
```

The default `awx_operator_version: latest` resolves GitHub's latest published
release. If both the requested operator and its bundled AWX version are already
running, the playbook finishes after preflight. For a specific published release:

```bash
sudo ansible-playbook -i localhost, ansibletower-playbook/update_awx.yaml \
  -e awx_operator_version=2.19.1
```

The playbook validates the existing installation and creates an `AWXBackup` using
the current operator. It waits for `status.backupDirectory`, which the backup role
publishes after completing the database and secrets backup, before applying the
new operator. It then verifies the expected images in both AWX deployments, waits
for their rollouts and checks that the API serves the bundled AWX version.
Backup failure stops the upgrade before the operator changes; later failures
produce diagnostics and fail the playbook.

The existing AWX resource and its storage/settings are reused, including LDAP
settings in the database. The playbook requires the standard upstream AWX image,
enabled `auto_upgrade` and an unset `spec.image_version`; downgrades are rejected.
It updates AWX and its operator rather than the K3s installation.

| Variable | Default | Purpose |
| --- | --- | --- |
| `awx_operator_version` | `latest` | Latest published release or an explicit operator version. |
| `awx_namespace` / `awx_name` | `awx` / `awx` | Existing namespace and AWX resource name. |
| `awx_workdir` | `/opt/awx` | Parent directory for private upgrade reports and manifests. |
| `awx_api_url` | Detected server IPv4 address, HTTP port `30080` | Base URL for unauthenticated `/api/v2/ping/` checks; override for HTTPS or a different endpoint. |
| `awx_backup_storage_class` | `local-path` | StorageClass for the backup PVC. |
| `awx_backup_storage_size` | `20Gi` | Requested backup storage; allow enough space for the database and retained backups. |
| `awx_wait_retries` / `awx_wait_delay` | `120` / `10` seconds | Backup, image reconciliation and API polling limits. |

Reports, the original AWX/operator JSON and the completed backup reference are
saved under `/opt/awx/upgrades/<timestamp>/` with private permissions. The operator
retains backup archives in the `awx-backup-claim` PVC (or the equivalent name for
`awx_name`). The native backup covers the database, selected secrets and AWX
configuration; manually maintained files in the projects PVC need a separate
volume backup. See the
[backup role](https://github.com/ansible/awx-operator/blob/2.19.1/roles/backup/README.md)
and [restore role](https://github.com/ansible/awx-operator/blob/2.19.1/roles/restore/README.md)
for recovery. Database migrations require restoring a compatible backup for
rollback; this playbook does not perform automatic rollback.

`--check` performs read-only preflight against the existing cluster and AWX API,
then shows the version plan. It creates no backup or local upgrade files and
does not apply Kubernetes changes. After an upgrade, use this update playbook for
maintenance; rerunning an older installer can select its older operator version.

Tests use real Ansible with simulated Kubernetes and HTTP endpoints, so they do
not require an AWX/K3s cluster:

```bash
python ansibletower-playbook/tests/test_update_awx.py
```
