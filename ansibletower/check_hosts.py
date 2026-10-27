#!/usr/bin/env python3
"""Check whether hosts exist in Ansible Tower (Python 3.9+, no dependencies)."""

import argparse
import json
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from tower_auth import CredentialsError, load_credentials


# Wklej tutaj hosty rozdzielone spacjami, tabulatorami lub nowymi liniami.
HOSTS = "CXCX CXC cxzczxc "


class TowerError(Exception):
    """Blad zapytania do Tower, a nie brak hosta."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Token pozostaje przy wskazanym serwerze, takze przy przekierowaniu.
        return None


def parse_hosts(text):
    """Usun powtorzenia bez rozrozniania wielkosci liter; zachowaj kolejnosc."""
    unique = {}
    for host in text.split():
        unique.setdefault(host.casefold(), host)
    return list(unique.values())


def host_exists(opener, tower_url, token, host, inventory=None, timeout=15):
    params = {"name__iexact": host, "page_size": 1}
    if inventory is not None:
        params["inventory"] = inventory
    url = tower_url.rstrip("/") + "/api/v2/hosts/?" + urlencode(params)
    request = Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            data = json.load(response)
    except HTTPError as exc:
        messages = {
            401: "Invalid or expired token (HTTP 401).",
            403: "Permission denied when reading hosts (HTTP 403).",
        }
        raise TowerError(messages.get(
            exc.code, f"API returned HTTP {exc.code}; check the Tower URL."
        )) from exc
    except (URLError, OSError) as exc:
        raise TowerError("Cannot connect to Tower; check the URL, network and TLS certificate.") from exc
    except (ValueError, UnicodeError) as exc:
        raise TowerError("Tower did not return valid JSON.") from exc

    count = data.get("count") if isinstance(data, dict) else None
    if type(count) is not int or count < 0:
        raise TowerError("Invalid API response: missing or invalid count field.")
    return count > 0


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", default=HOSTS, help='Host list, e.g. "CXCX CXC cxzczxc "')
    parser.add_argument("--url", help="Tower URL (overrides shared configuration)")
    parser.add_argument("--inventory", type=positive_int, help="Optional inventory ID")
    parser.add_argument("--timeout", type=positive_int, default=15, help="Request timeout in seconds (15)")
    parser.add_argument("--ca-cert", help="PEM file containing a trusted corporate CA certificate")
    args = parser.parse_args(argv)
    hosts = parse_hosts(args.hosts)
    if not hosts:
        parser.error("Host list is empty.")
    try:
        try:
            credentials = load_credentials(args.url)
        except CredentialsError as exc:
            parser.error(str(exc))
        context = ssl.create_default_context(cafile=args.ca_cert)
        opener = build_opener(HTTPSHandler(context=context), NoRedirect())
        missing = 0
        for host in hosts:
            try:
                exists = host_exists(opener, credentials.url, credentials.token, host,
                                     args.inventory, args.timeout)
            except TowerError as exc:
                print(f"ERROR [{host}]: {exc} Check aborted.", file=sys.stderr)
                return 2
            print(f"{'EXISTS' if exists else 'MISSING'}: {host}")
            missing += not exists
    except (OSError, ValueError, EOFError) as exc:
        print(f"ERROR: Could not prepare the connection or token ({type(exc).__name__}).", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCheck aborted.", file=sys.stderr)
        return 2

    print(f"Summary: checked {len(hosts)}, exists {len(hosts) - missing}, missing {missing}.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
