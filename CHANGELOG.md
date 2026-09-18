# Changelog

## 2026-09-18 — AWX Inventory Sync

- Added `linux/awx-inventory-sync.sh` for autonomous SSH-based AWX inventory synchronization.
- Detects AWX running in Kubernetes through kubectl, K3s or MicroK8s and imports inventory with `awx-manage`.
- Bootstraps a dedicated ED25519 key from a one-time SSH password, does not persist the password, and installs a five-minute cron synchronization.

## 2026-09-18 — Nginx Manager 1.2.0

- Added global migration of active Nginx `listen` directives between ports.
- Added `--disable-port-80 --port NEW_PORT` and `--move-listen-port --from-port OLD --port NEW`.
- Added coordinated backup, `nginx -t` validation, source-port verification and rollback across all affected files.
- Added global port migration to the dialog GUI.
- Added a configurable frontend/listen port to the reverse-proxy wizard and CLI.
- Added regression tests for IPv4/IPv6 migration, preservation of unrelated ports and rollback.

## 2026-09-17 — Nginx Manager 1.1.1

- Replaced manual domain entry with a Virtual Host selection list for edit, port, delete, enable, disable, preview, clone and SSL operations.
- Added status, HTTP port and configuration filename to each selection row.

## 2026-09-17 — Nginx Manager 1.1.0

- Added atomic HTTP port changes for individual Virtual Hosts.
- Added default Nginx HTTP port changes through GUI and non-interactive CLI.
- Preserved SSL listen ports while changing HTTP ports.

## 2026-09-17

- Added Nginx Manager with dialog and non-interactive CLI modes.
- Added Debian/RHEL layout and package-manager detection.
- Added safe Virtual Host, PHP-FPM, reverse-proxy and SSL workflows.
- Added atomic configuration writes, `nginx -t` gates, backups and rollback.
- Added logs, ports, diagnostics, security previews and administrative audit logging.
- Added ShellCheck workflow and regression tests.
