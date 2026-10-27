#!/usr/bin/env python3
"""Add hosts to an Ansible Tower inventory (Python 3.9+, no dependencies)."""

import argparse
import json
import ssl
import sys
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener

from check_hosts import NoRedirect, TowerError, host_exists, parse_hosts, positive_int
from tower_auth import CredentialsError, load_credentials


# Paste hosts separated by spaces, tabs or newlines, or use --hosts.
HOSTS = ""

UNKNOWN_RESULT = "Creation may have completed. Check the inventory before rerunning."


def create_host(opener, tower_url, token, host, inventory, timeout=15):
    """Create one enabled host. Never retry a POST with an uncertain outcome."""
    payload = {"name": host, "inventory": inventory, "enabled": True}
    request = Request(
        tower_url.rstrip("/") + "/api/v2/hosts/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 201:
                raise TowerError("Unexpected creation response. " + UNKNOWN_RESULT)
            data = json.load(response)
    except HTTPError as exc:
        messages = {
            400: "Host rejected (HTTP 400); check the host name, inventory and possible duplicates.",
            401: "Invalid or expired token (HTTP 401).",
            403: "Permission denied when adding hosts (HTTP 403).",
            404: "API or inventory not found (HTTP 404); check the Tower URL and inventory ID.",
            409: "Host creation conflict (HTTP 409); check for an existing host.",
        }
        message = messages.get(exc.code, f"Host creation returned HTTP {exc.code}.")
        if exc.code >= 500:
            message += " " + UNKNOWN_RESULT
        raise TowerError(message) from exc
    except (URLError, OSError, HTTPException) as exc:
        raise TowerError("Connection failed while adding the host. " + UNKNOWN_RESULT) from exc
    except (ValueError, UnicodeError) as exc:
        raise TowerError("Invalid JSON in creation response. " + UNKNOWN_RESULT) from exc

    if (not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] <= 0
            or data.get("name") != host or data.get("inventory") != inventory):
        raise TowerError("Invalid host details in creation response. " + UNKNOWN_RESULT)
    return data["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", default=HOSTS, help='Host list, e.g. "host01 host02 host03"')
    parser.add_argument("--inventory", required=True, type=positive_int, help="Target inventory ID")
    parser.add_argument("--url", help="Tower URL (overrides shared configuration)")
    parser.add_argument("--timeout", type=positive_int, default=15, help="Request timeout in seconds (15)")
    parser.add_argument("--ca-cert", help="PEM file containing a trusted corporate CA certificate")
    parser.add_argument("--dry-run", action="store_true", help="Check hosts and show planned additions without writing")
    args = parser.parse_args(argv)
    hosts = parse_hosts(args.hosts)
    if not hosts:
        parser.error("Host list is empty. Set HOSTS in the script or pass --hosts.")

    try:
        try:
            credentials = load_credentials(args.url)
        except CredentialsError as exc:
            parser.error(str(exc))
        context = ssl.create_default_context(cafile=args.ca_cert)
        opener = build_opener(HTTPSHandler(context=context), NoRedirect())
        added = skipped = planned = 0
        for host in hosts:
            try:
                if host_exists(opener, credentials.url, credentials.token, host,
                               args.inventory, args.timeout):
                    print(f"SKIPPED (already exists): {host}")
                    skipped += 1
                elif args.dry_run:
                    print(f"WOULD ADD: {host}")
                    planned += 1
                else:
                    host_id = create_host(opener, credentials.url, credentials.token, host,
                                          args.inventory, args.timeout)
                    print(f"ADDED: {host} (ID {host_id})")
                    added += 1
            except TowerError as exc:
                print(f"ERROR [{host}]: {exc} Addition aborted; previously added hosts remain.", file=sys.stderr)
                return 2
    except (OSError, ValueError, EOFError) as exc:
        print(f"ERROR: Could not prepare the connection or token ({type(exc).__name__}).", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAddition aborted; previously added hosts remain. Check the inventory before rerunning.", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"Dry-run summary: checked {len(hosts)}, would add {planned}, skipped {skipped}.")
    else:
        print(f"Summary: processed {len(hosts)}, added {added}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
