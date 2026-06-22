#!/usr/bin/env bash
###############################################################################
#  install.sh – Pełny instalator aplikacji Flask „Hosts Manager"
#  Uruchomienie:  sudo bash install.sh
###############################################################################
set -euo pipefail
IFS=$'\n\t'

APP_DIR="/opt/hosts-manager"
VENV_DIR="${APP_DIR}/venv"
SERVICE_NAME="hosts-manager"
APP_USER="root"
APP_PORT=81
BIND_ADDR="0.0.0.0"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✔]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✘]${NC} $*" >&2; exit 1; }

###############################################################################
# 1. Sprawdzenie uprawnień
###############################################################################
[[ $EUID -ne 0 ]] && err "Uruchom skrypt jako root:  sudo bash $0"

###############################################################################
# 2. Wykrywanie systemu i instalacja zależności
###############################################################################
log "Wykrywanie systemu..."

install_packages() {
    if command -v apt-get &>/dev/null; then
        log "Debian/Ubuntu – używam apt"
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv >/dev/null 2>&1
    elif command -v dnf &>/dev/null; then
        log "Fedora/RHEL – używam dnf"
        dnf install -y -q python3 python3-pip python3-virtualenv >/dev/null 2>&1
    elif command -v yum &>/dev/null; then
        log "CentOS/RHEL – używam yum"
        yum install -y -q python3 python3-pip python3-virtualenv >/dev/null 2>&1
    elif command -v pacman &>/dev/null; then
        log "Arch Linux – używam pacman"
        pacman -Sy --noconfirm --quiet python python-pip python-virtualenv >/dev/null 2>&1
    elif command -v zypper &>/dev/null; then
        log "openSUSE – używam zypper"
        zypper install -y -q python3 python3-pip python3-virtualenv >/dev/null 2>&1
    else
        err "Nieobsługiwany menedżer pakietów. Zainstaluj ręcznie: python3, pip, venv."
    fi
}

for cmd in python3 pip3; do
    if ! command -v "$cmd" &>/dev/null; then
        warn "Brakuje $cmd – instaluję zależności..."
        install_packages
        break
    fi
done

python3 -c "import venv" 2>/dev/null || { warn "Brakuje modułu venv"; install_packages; }

log "Python: $(python3 --version)"

###############################################################################
# 3. Tworzenie struktury katalogów
###############################################################################
log "Tworzenie katalogu projektu: ${APP_DIR}"
mkdir -p "${APP_DIR}"/{config,backups,templates,static/css,static/js}

###############################################################################
# 4. Generowanie SECRET_KEY
###############################################################################
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

cat > "${APP_DIR}/config/settings.py" << 'PYEOF'
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
HOSTS_FILE = os.environ.get("HOSTS_FILE", "/etc/hosts")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
MAX_BACKUPS = 50
PYEOF

cat > "${APP_DIR}/config/.env" << ENVEOF
FLASK_SECRET_KEY=${SECRET_KEY}
HOSTS_FILE=/etc/hosts
ENVEOF

log "Wygenerowano config i SECRET_KEY"

###############################################################################
# 5. requirements.txt
###############################################################################
cat > "${APP_DIR}/requirements.txt" << 'EOF'
Flask>=3.0,<4.0
EOF

log "Wygenerowano requirements.txt"

###############################################################################
# 6. Virtualenv + Flask
###############################################################################
log "Tworzenie virtualenv..."
python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q
pip install -r "${APP_DIR}/requirements.txt" -q
deactivate
log "Flask zainstalowany w virtualenv"

###############################################################################
# 7. Główna aplikacja Flask – app.py
###############################################################################
cat > "${APP_DIR}/app.py" << 'PYEOF'
#!/usr/bin/env python3
"""Hosts Manager – Flask application."""

import os
import re
import shutil
import tempfile
import glob
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify
)

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Wczytaj zmienne z .env (prosty parser, bez dodatkowych zależności)
_env_path = os.path.join(BASE_DIR, "config", ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-fallback-key")

HOSTS_FILE = os.environ.get("HOSTS_FILE", "/etc/hosts")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
MAX_BACKUPS = 50

os.makedirs(BACKUP_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Walidacja
# ---------------------------------------------------------------------------
IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
)
HOSTNAME_RE = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def validate_ip(ip: str) -> bool:
    return bool(IPV4_RE.match(ip.strip()))


def validate_hostname(name: str) -> bool:
    return bool(HOSTNAME_RE.match(name.strip())) and len(name) <= 253


# ---------------------------------------------------------------------------
# Odczyt / zapis /etc/hosts
# ---------------------------------------------------------------------------

def read_hosts() -> list[dict]:
    """Zwraca listę słowników: {line_num, ip, hostnames, raw, is_comment}."""
    entries = []
    try:
        with open(HOSTS_FILE, "r") as f:
            for idx, raw_line in enumerate(f):
                raw = raw_line.rstrip("\n")
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    entries.append({
                        "line_num": idx,
                        "ip": "",
                        "hostnames": "",
                        "raw": raw,
                        "is_comment": True,
                    })
                else:
                    parts = stripped.split()
                    ip = parts[0] if parts else ""
                    hostnames = " ".join(parts[1:]) if len(parts) > 1 else ""
                    entries.append({
                        "line_num": idx,
                        "ip": ip,
                        "hostnames": hostnames,
                        "raw": raw,
                        "is_comment": False,
                    })
    except FileNotFoundError:
        pass
    return entries


def make_backup() -> str:
    """Tworzy kopię zapasową pliku hosts. Zwraca ścieżkę."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = os.path.join(BACKUP_DIR, f"hosts.{ts}.bak")
    shutil.copy2(HOSTS_FILE, dst)
    _rotate_backups()
    return dst


def _rotate_backups():
    """Usuwa najstarsze kopie jeśli przekroczono MAX_BACKUPS."""
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "hosts.*.bak")))
    while len(files) > MAX_BACKUPS:
        os.remove(files.pop(0))


def atomic_write(path: str, content: str):
    """Zapisuje plik atomowo przez tmp + rename."""
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".hosts_tmp_")
    try:
        with os.fdopen(fd, "w") as tmp_f:
            tmp_f.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def write_hosts(lines: list[str]):
    """Robi backup, potem atomowo zapisuje nowy plik hosts."""
    make_backup()
    content = "\n".join(lines) + "\n"
    atomic_write(HOSTS_FILE, content)


def check_duplicate(ip: str, hostnames: str, exclude_line: int = -1) -> bool:
    """True jeśli wpis (ip + hostnames) już istnieje (pomijając exclude_line)."""
    entries = read_hosts()
    for e in entries:
        if e["is_comment"]:
            continue
        if e["line_num"] == exclude_line:
            continue
        if e["ip"] == ip and e["hostnames"] == hostnames:
            return True
    return False


# ---------------------------------------------------------------------------
# Widoki
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    entries = read_hosts()
    return render_template("index.html", entries=entries)


@app.route("/add", methods=["POST"])
def add_entry():
    ip = request.form.get("ip", "").strip()
    hostnames = request.form.get("hostnames", "").strip()

    if not ip or not hostnames:
        flash("Adres IP i nazwa hosta nie mogą być puste.", "danger")
        return redirect(url_for("index"))

    if not validate_ip(ip):
        flash(f"Nieprawidłowy adres IPv4: {ip}", "danger")
        return redirect(url_for("index"))

    for h in hostnames.split():
        if not validate_hostname(h):
            flash(f"Nieprawidłowa nazwa hosta: {h}", "danger")
            return redirect(url_for("index"))

    if check_duplicate(ip, hostnames):
        flash("Taki wpis już istnieje.", "warning")
        return redirect(url_for("index"))

    try:
        entries = read_hosts()
        lines = [e["raw"] for e in entries]
        lines.append(f"{ip}\t{hostnames}")
        write_hosts(lines)
        flash(f"Dodano wpis: {ip} → {hostnames}", "success")
    except Exception as exc:
        flash(f"Błąd zapisu: {exc}", "danger")

    return redirect(url_for("index"))


@app.route("/edit/<int:line_num>", methods=["POST"])
def edit_entry(line_num):
    ip = request.form.get("ip", "").strip()
    hostnames = request.form.get("hostnames", "").strip()

    if not ip or not hostnames:
        flash("Adres IP i nazwa hosta nie mogą być puste.", "danger")
        return redirect(url_for("index"))

    if not validate_ip(ip):
        flash(f"Nieprawidłowy adres IPv4: {ip}", "danger")
        return redirect(url_for("index"))

    for h in hostnames.split():
        if not validate_hostname(h):
            flash(f"Nieprawidłowa nazwa hosta: {h}", "danger")
            return redirect(url_for("index"))

    if check_duplicate(ip, hostnames, exclude_line=line_num):
        flash("Taki wpis już istnieje.", "warning")
        return redirect(url_for("index"))

    try:
        entries = read_hosts()
        lines = [e["raw"] for e in entries]
        if 0 <= line_num < len(lines):
            lines[line_num] = f"{ip}\t{hostnames}"
            write_hosts(lines)
            flash(f"Zaktualizowano wiersz {line_num}.", "success")
        else:
            flash("Nieprawidłowy numer wiersza.", "danger")
    except Exception as exc:
        flash(f"Błąd zapisu: {exc}", "danger")

    return redirect(url_for("index"))


@app.route("/delete/<int:line_num>", methods=["POST"])
def delete_entry(line_num):
    try:
        entries = read_hosts()
        lines = [e["raw"] for e in entries]
        if 0 <= line_num < len(lines):
            removed = lines.pop(line_num)
            write_hosts(lines)
            flash(f"Usunięto wiersz: {removed}", "success")
        else:
            flash("Nieprawidłowy numer wiersza.", "danger")
    except Exception as exc:
        flash(f"Błąd zapisu: {exc}", "danger")

    return redirect(url_for("index"))


@app.route("/backups")
def list_backups():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "hosts.*.bak")), reverse=True)
    backups = []
    for fp in files:
        stat = os.stat(fp)
        backups.append({
            "name": os.path.basename(fp),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return render_template("backups.html", backups=backups)


@app.route("/restore/<filename>", methods=["POST"])
def restore_backup(filename):
    safe_name = os.path.basename(filename)
    src = os.path.join(BACKUP_DIR, safe_name)
    if not os.path.isfile(src):
        flash("Backup nie istnieje.", "danger")
        return redirect(url_for("list_backups"))
    try:
        make_backup()
        shutil.copy2(src, HOSTS_FILE)
        flash(f"Przywrócono z kopii: {safe_name}", "success")
    except Exception as exc:
        flash(f"Błąd przywracania: {exc}", "danger")
    return redirect(url_for("index"))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


# ---------------------------------------------------------------------------
# Uruchomienie
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 81
    app.run(host="0.0.0.0", port=port, debug=False)
PYEOF

log "Wygenerowano app.py"

###############################################################################
# 8. Szablon – base.html
###############################################################################
cat > "${APP_DIR}/templates/base.html" << 'HTMLEOF'
<!DOCTYPE html>
<html lang="pl" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% block title %}Hosts Manager{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
          rel="stylesheet"
          integrity="sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YcnS/oPxdOTrIH2+SL7R4dXOGcB2IA05mRZ"
          crossorigin="anonymous">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"
          rel="stylesheet">
    <link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
        <div class="container">
            <a class="navbar-brand" href="{{ url_for('index') }}">
                <i class="bi bi-hdd-network"></i> Hosts Manager
            </a>
            <div class="navbar-nav ms-auto">
                <a class="nav-link" href="{{ url_for('index') }}">
                    <i class="bi bi-house"></i> Główna
                </a>
                <a class="nav-link" href="{{ url_for('list_backups') }}">
                    <i class="bi bi-clock-history"></i> Kopie zapasowe
                </a>
            </div>
        </div>
    </nav>

    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
                        {{ message }}
                        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        {% block content %}{% endblock %}
    </div>

    <footer class="text-center text-muted py-4 mt-5 border-top">
        <small>Hosts Manager &copy; {{ now().year if now is defined else "2025" }} &mdash; Flask</small>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
            integrity="sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz"
            crossorigin="anonymous"></script>
    <script src="{{ url_for('static', filename='js/app.js') }}"></script>
</body>
</html>
HTMLEOF

###############################################################################
# 9. Szablon – index.html
###############################################################################
cat > "${APP_DIR}/templates/index.html" << 'HTMLEOF'
{% extends "base.html" %}
{% block title %}Hosts Manager – /etc/hosts{% endblock %}

{% block content %}
<div class="row g-4">
    <!-- ======== Formularz dodawania ======== -->
    <div class="col-lg-4">
        <div class="card shadow-sm">
            <div class="card-header bg-primary text-white">
                <i class="bi bi-plus-circle"></i> Dodaj wpis
            </div>
            <div class="card-body">
                <form method="post" action="{{ url_for('add_entry') }}" id="addForm">
                    <div class="mb-3">
                        <label for="addIp" class="form-label">Adres IPv4</label>
                        <input type="text" class="form-control" id="addIp" name="ip"
                               placeholder="np. 192.168.1.10" required
                               pattern="^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
                               title="Podaj prawidłowy adres IPv4">
                    </div>
                    <div class="mb-3">
                        <label for="addHost" class="form-label">Nazwa(-y) hosta</label>
                        <input type="text" class="form-control" id="addHost" name="hostnames"
                               placeholder="np. myserver.local" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">
                        <i class="bi bi-plus-lg"></i> Dodaj
                    </button>
                </form>
            </div>
        </div>

        <div class="card shadow-sm mt-3">
            <div class="card-header bg-secondary text-white">
                <i class="bi bi-info-circle"></i> Informacje
            </div>
            <div class="card-body small">
                <p class="mb-1"><strong>Plik:</strong> /etc/hosts</p>
                <p class="mb-1"><strong>Wierszy:</strong> {{ entries|length }}</p>
                <p class="mb-0"><strong>Wpisów:</strong> {{ entries|selectattr("is_comment", "false")|list|length }}</p>
            </div>
        </div>
    </div>

    <!-- ======== Tabela wpisów ======== -->
    <div class="col-lg-8">
        <div class="card shadow-sm">
            <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center">
                <span><i class="bi bi-file-earmark-text"></i> Zawartość /etc/hosts</span>
                <input type="text" id="filterInput" class="form-control form-control-sm w-auto"
                       placeholder="Filtruj..." style="max-width: 200px;">
            </div>
            <div class="table-responsive">
                <table class="table table-hover table-striped mb-0" id="hostsTable">
                    <thead class="table-light">
                        <tr>
                            <th style="width:40px">#</th>
                            <th style="width:160px">IP</th>
                            <th>Hostname(s)</th>
                            <th style="width:140px" class="text-end">Akcje</th>
                        </tr>
                    </thead>
                    <tbody>
                    {% for e in entries %}
                        <tr class="{{ 'table-secondary text-muted' if e.is_comment else '' }}">
                            <td><small>{{ e.line_num }}</small></td>

                            {% if e.is_comment %}
                                <td colspan="2"><code>{{ e.raw }}</code></td>
                                <td class="text-end">
                                    <form method="post" action="{{ url_for('delete_entry', line_num=e.line_num) }}"
                                          class="d-inline" onsubmit="return confirm('Usunąć ten wiersz?');">
                                        <button class="btn btn-outline-danger btn-sm" title="Usuń">
                                            <i class="bi bi-trash"></i>
                                        </button>
                                    </form>
                                </td>
                            {% else %}
                                <td><code>{{ e.ip }}</code></td>
                                <td><code>{{ e.hostnames }}</code></td>
                                <td class="text-end text-nowrap">
                                    <button class="btn btn-outline-primary btn-sm"
                                            title="Edytuj"
                                            onclick="openEditModal({{ e.line_num }}, '{{ e.ip }}', '{{ e.hostnames }}')">
                                        <i class="bi bi-pencil"></i>
                                    </button>
                                    <form method="post" action="{{ url_for('delete_entry', line_num=e.line_num) }}"
                                          class="d-inline" onsubmit="return confirm('Usunąć wpis {{ e.ip }} {{ e.hostnames }}?');">
                                        <button class="btn btn-outline-danger btn-sm" title="Usuń">
                                            <i class="bi bi-trash"></i>
                                        </button>
                                    </form>
                                </td>
                            {% endif %}
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</div>

<!-- ======== Modal edycji ======== -->
<div class="modal fade" id="editModal" tabindex="-1">
    <div class="modal-dialog">
        <div class="modal-content">
            <form method="post" id="editForm">
                <div class="modal-header bg-primary text-white">
                    <h5 class="modal-title"><i class="bi bi-pencil-square"></i> Edytuj wpis</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-3">
                        <label class="form-label">Adres IPv4</label>
                        <input type="text" class="form-control" id="editIp" name="ip" required
                               pattern="^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$">
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Nazwa(-y) hosta</label>
                        <input type="text" class="form-control" id="editHost" name="hostnames" required>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Anuluj</button>
                    <button type="submit" class="btn btn-primary">
                        <i class="bi bi-check-lg"></i> Zapisz
                    </button>
                </div>
            </form>
        </div>
    </div>
</div>
{% endblock %}
HTMLEOF

###############################################################################
# 10. Szablon – backups.html
###############################################################################
cat > "${APP_DIR}/templates/backups.html" << 'HTMLEOF'
{% extends "base.html" %}
{% block title %}Hosts Manager – Kopie zapasowe{% endblock %}

{% block content %}
<div class="card shadow-sm">
    <div class="card-header bg-dark text-white">
        <i class="bi bi-clock-history"></i> Kopie zapasowe ({{ backups|length }})
    </div>
    {% if backups %}
    <div class="table-responsive">
        <table class="table table-hover mb-0">
            <thead class="table-light">
                <tr>
                    <th>Nazwa pliku</th>
                    <th>Rozmiar</th>
                    <th>Data</th>
                    <th class="text-end">Akcja</th>
                </tr>
            </thead>
            <tbody>
            {% for b in backups %}
                <tr>
                    <td><code>{{ b.name }}</code></td>
                    <td>{{ b.size }} B</td>
                    <td>{{ b.mtime }}</td>
                    <td class="text-end">
                        <form method="post" action="{{ url_for('restore_backup', filename=b.name) }}"
                              onsubmit="return confirm('Przywrócić tę kopię? Obecny plik hosts zostanie zbackupowany.');">
                            <button class="btn btn-outline-success btn-sm">
                                <i class="bi bi-arrow-counterclockwise"></i> Przywróć
                            </button>
                        </form>
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <div class="card-body text-center text-muted py-5">
        <i class="bi bi-inbox" style="font-size:2rem"></i>
        <p class="mt-2">Brak kopii zapasowych.</p>
    </div>
    {% endif %}
</div>
{% endblock %}
HTMLEOF

log "Wygenerowano szablony HTML"

###############################################################################
# 11. CSS
###############################################################################
cat > "${APP_DIR}/static/css/style.css" << 'CSSEOF'
/* Hosts Manager – custom styles */
body {
    background-color: #f4f6f9;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
}

.navbar-brand {
    font-weight: 600;
    letter-spacing: 0.5px;
}

.card {
    border: none;
    border-radius: 0.5rem;
}

.card-header {
    border-radius: 0.5rem 0.5rem 0 0 !important;
    font-weight: 600;
}

.table code {
    color: inherit;
    font-size: 0.9em;
}

.table td, .table th {
    vertical-align: middle;
}

.alert {
    border-radius: 0.5rem;
}

footer {
    font-size: 0.85rem;
}

#filterInput::placeholder {
    opacity: 0.6;
}
CSSEOF

###############################################################################
# 12. JavaScript
###############################################################################
cat > "${APP_DIR}/static/js/app.js" << 'JSEOF'
/* Hosts Manager – frontend logic */

function openEditModal(lineNum, ip, hostnames) {
    const form = document.getElementById('editForm');
    form.action = '/edit/' + lineNum;
    document.getElementById('editIp').value = ip;
    document.getElementById('editHost').value = hostnames;
    new bootstrap.Modal(document.getElementById('editModal')).show();
}

/* Filtrowanie tabeli */
document.addEventListener('DOMContentLoaded', function () {
    const filterInput = document.getElementById('filterInput');
    if (!filterInput) return;

    filterInput.addEventListener('input', function () {
        const query = this.value.toLowerCase();
        const rows = document.querySelectorAll('#hostsTable tbody tr');
        rows.forEach(function (row) {
            const text = row.textContent.toLowerCase();
            row.style.display = text.includes(query) ? '' : 'none';
        });
    });

    /* Automatyczne ukrywanie alertów po 5s */
    document.querySelectorAll('.alert').forEach(function (alert) {
        setTimeout(function () {
            var bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
            bsAlert.close();
        }, 5000);
    });
});
JSEOF

log "Wygenerowano static (CSS + JS)"

###############################################################################
# 13. run.sh
###############################################################################
cat > "${APP_DIR}/run.sh" << RUNEOF
#!/usr/bin/env bash
# Uruchomienie Hosts Manager
cd "${APP_DIR}"
source "${VENV_DIR}/bin/activate"
exec python3 app.py ${APP_PORT}
RUNEOF
chmod +x "${APP_DIR}/run.sh"

log "Wygenerowano run.sh"

###############################################################################
# 14. Usługa systemd
###############################################################################
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SVCEOF
[Unit]
Description=Hosts Manager – Flask Web App
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/config/.env
ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/app.py ${APP_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1
systemctl restart "${SERVICE_NAME}.service"

log "Usługa systemd '${SERVICE_NAME}' aktywna i włączony autostart"

###############################################################################
# 15. Weryfikacja
###############################################################################
sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    log "Aplikacja działa na http://${BIND_ADDR}:${APP_PORT}"
else
    warn "Usługa nie wystartowała. Sprawdź:  journalctl -u ${SERVICE_NAME} -n 30"
fi

echo ""
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}  Instalacja zakończona!${NC}"
echo -e "${GREEN}  URL:       http://<ADRES_SERWERA>:${APP_PORT}${NC}"
echo -e "${GREEN}  Katalog:   ${APP_DIR}${NC}"
echo -e "${GREEN}  Usługa:    systemctl status ${SERVICE_NAME}${NC}"
echo -e "${GREEN}  Logi:      journalctl -u ${SERVICE_NAME} -f${NC}"
echo -e "${GREEN}================================================================${NC}"