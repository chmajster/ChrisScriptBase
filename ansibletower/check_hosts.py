#!/usr/bin/env python3
"""Sprawdz obecnosc hostow w Ansible Tower (Python 3.9+, bez zaleznosci)."""

import argparse
import getpass
import json
import os
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


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
            401: "Nieprawidlowy lub wygasly token (HTTP 401).",
            403: "Brak uprawnien do odczytu hostow (HTTP 403).",
        }
        raise TowerError(messages.get(
            exc.code, f"API zwrocilo HTTP {exc.code}; sprawdz adres Tower."
        )) from exc
    except (URLError, OSError) as exc:
        raise TowerError("Blad polaczenia z Tower; sprawdz adres, siec i certyfikat TLS.") from exc
    except (ValueError, UnicodeError) as exc:
        raise TowerError("Tower nie zwrocil poprawnego JSON.") from exc

    count = data.get("count") if isinstance(data, dict) else None
    if type(count) is not int or count < 0:
        raise TowerError("Nieprawidlowa odpowiedz API: brak poprawnego pola count.")
    return count > 0


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Wartosc musi byc wieksza od zera.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", default=HOSTS, help='Lista, np. "CXCX CXC cxzczxc "')
    parser.add_argument("--url", default=os.environ.get("TOWER_URL"), help="Adres Tower lub TOWER_URL")
    parser.add_argument("--inventory", type=positive_int, help="Opcjonalny identyfikator inventory")
    parser.add_argument("--timeout", type=positive_int, default=15, help="Limit sekund na zapytanie (15)")
    parser.add_argument("--ca-cert", help="Plik PEM z zaufanym certyfikatem CA firmy")
    args = parser.parse_args(argv)
    hosts = parse_hosts(args.hosts)
    if not hosts:
        parser.error("Lista hostow jest pusta.")
    url = urlsplit(args.url or "")
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        parser.error("Ustaw TOWER_URL lub --url, np. https://tower.example.com (bez danych logowania).")

    try:
        token = (os.environ.get("TOWER_TOKEN") or getpass.getpass("Token API Tower: ")).strip()
        if not token:
            parser.error("Token API jest pusty.")
        context = ssl.create_default_context(cafile=args.ca_cert)
        opener = build_opener(HTTPSHandler(context=context), NoRedirect())
        missing = 0
        for host in hosts:
            try:
                exists = host_exists(opener, args.url, token, host, args.inventory, args.timeout)
            except TowerError as exc:
                print(f"BLAD [{host}]: {exc} Sprawdzanie przerwane.", file=sys.stderr)
                return 2
            print(f"{'ISTNIEJE' if exists else 'BRAK'}: {host}")
            missing += not exists
    except (OSError, ValueError, EOFError) as exc:
        print(f"BLAD: Nie udalo sie przygotowac polaczenia lub tokenu ({type(exc).__name__}).", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nSprawdzanie przerwane.", file=sys.stderr)
        return 2

    print(f"Podsumowanie: sprawdzono {len(hosts)}, istnieje {len(hosts) - missing}, brak {missing}.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
