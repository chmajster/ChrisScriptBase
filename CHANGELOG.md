# Changelog

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
