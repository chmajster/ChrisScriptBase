# Nginx Manager

`linux/nginx-manager.sh` is an administrative Nginx module for ChrisScriptBase. Its primary interface uses `dialog`; read-only and automation operations are also available through CLI.

## Supported systems

- Debian, Ubuntu and Linux Mint (`apt`)
- RHEL, Rocky Linux, AlmaLinux, CentOS Stream and Fedora (`dnf` or `yum`)

The manager detects the active Debian-style `sites-available/sites-enabled` or RHEL-style `conf.d` layout. Existing server blocks remain visible and are not modified until an explicit operation is confirmed.

## Requirements

Required for the complete GUI: Bash, Nginx, `dialog`, OpenSSL, curl and systemd. `ss`, `lsof` and Certbot are optional. If Nginx is missing, the installation menu can install it using the detected package manager.

## Running

```bash
sudo bash linux/nginx-manager.sh
bash linux/nginx-manager.sh --help
bash linux/nginx-manager.sh --status
sudo bash linux/nginx-manager.sh --test
sudo bash linux/nginx-manager.sh --reload
sudo bash linux/nginx-manager.sh --backup
bash linux/nginx-manager.sh --list-sites
bash linux/nginx-manager.sh --diagnostic
```

Non-interactive examples:

```bash
sudo bash linux/nginx-manager.sh --non-interactive --add-site \
  --domain example.com --root /var/www/example.com --port 80

sudo bash linux/nginx-manager.sh --non-interactive --add-proxy \
  --domain api.example.com --backend-host 127.0.0.1 \
  --backend-port 8080 --websocket
```

Destructive non-interactive operations require `--yes`. Use `--dry-run` to inspect package and service commands.

## Features

- status, service lifecycle and autostart;
- Virtual Host discovery, creation, editing, cloning, enable/disable and deletion;
- static, PHP-FPM and reverse-proxy generators with WebSocket headers;
- certificate inventory, expiry warnings, Certbot, renewal, existing and self-signed certificates;
- access/error/journal logs with search and filters;
- listening-port inspection using `ss`, with `lsof` fallback;
- timestamped configuration backups, verified restore and rollback;
- diagnostic reports without private keys or tokens;
- security-header and TLS configuration previews;
- CLI and non-interactive operation for Ansible, cron and SSH.

## Safe write sequence

Configuration changes use a staged file, preserve the old file, run `nginx -t`, reload only after a successful test, and restore the previous version after failure. Restore creates an additional safety backup before replacing `/etc/nginx`.

Backups are written to:

```text
/var/backups/chrisscriptbase/nginx/nginx-YYYY-MM-DD_HHMMSS.tar.gz
```

Administrative events are written without secrets to:

```text
/var/log/chrisscriptbase/nginx-manager.log
```

Diagnostic reports default to `/tmp/nginx-diagnostic-YYYYMMDD-HHMMSS.txt` and receive mode `600`.

## Test

```bash
shellcheck -x linux/nginx-manager.sh
bash linux/tests/test-nginx-manager.sh
```

The regression test uses fake Nginx and systemd commands, so it does not alter the host Nginx installation.
