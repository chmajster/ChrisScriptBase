"""Shared Ansible Tower credentials for Python scripts. No network calls on import."""

import getpass
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


CREDENTIALS_FILE = Path(__file__).with_name("tower_credentials.json")


class CredentialsError(ValueError):
    """Invalid or unreadable Tower credentials configuration."""


@dataclass(frozen=True)
class TowerCredentials:
    url: str
    token: str = field(repr=False)


def load_credentials(tower_url=None, *, credentials_file=None, prompt=True):
    """Load URL and token: explicit URL > environment > local JSON > token prompt.

    The default JSON file is next to this module, independent of the working
    directory. Set prompt=False for unattended scripts; missing tokens then fail.
    """
    path = Path(credentials_file) if credentials_file is not None else CREDENTIALS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        data = {}
    except OSError as exc:
        raise CredentialsError("Cannot read the Tower credentials file.") from exc
    except (ValueError, UnicodeError) as exc:
        raise CredentialsError("Tower credentials file must contain valid UTF-8 JSON.") from exc

    if not isinstance(data, dict) or any(
        not isinstance(data.get(key, ""), str) for key in ("url", "token")
    ):
        raise CredentialsError("Tower credentials must be a JSON object with string url and token fields.")

    url = (tower_url or os.environ.get("TOWER_URL") or data.get("url", "")).strip()
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
        parsed.port  # Validate malformed port numbers too.
    except ValueError:
        valid = False
    if not valid:
        raise CredentialsError(
            "Set url in tower_credentials.json, TOWER_URL or --url to an HTTPS Tower URL without credentials."
        )

    token = (os.environ.get("TOWER_TOKEN") or data.get("token", "")).strip()
    if not token and prompt:
        token = getpass.getpass("Tower API token: ").strip()
    if not token:
        raise CredentialsError("Set token in tower_credentials.json or TOWER_TOKEN; API token is empty.")
    if any(char in token for char in "\r\n"):
        raise CredentialsError("API token must not contain line breaks.")
    return TowerCredentials(url=url.rstrip("/"), token=token)
