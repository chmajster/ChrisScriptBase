#!/usr/bin/env python3
r"""Wyświetl szablony jobów AWX znalezione po organizacji i wzorcu nazwy.

    python .\awx_find_templates.py --organization IT2
    python .\awx_find_templates.py --organization IT2 --name-pattern '*zabbix*it2*window*'
    python .\awx_find_templates.py --organization IT2 --hostname server1

Ustawienia wspólne: awx_variable. Kolejność nadpisań:
argumenty komendy > VARIABLES poniżej > TEMPLATES_OVERRIDES > wartości wspólne.
Domyślny wzorzec: *zabbix*it2*{os_name}*. Bez --hostname wyświetla wszystkie
pasujące warianty; z --hostname ustala ostype i ApplicationID hosta w AWX.
JOB_TEMPLATE_OS_NAMES mapuje Linux/AIX na unix, a Windows na window.
* oznacza dowolny ciąg, ? jeden znak. Wzorzec obsługuje {hostname},
{application_id}, {ostype} i {os_name}. Wyniki: nazwa, ID, organizacja, link.

Te same ustawienia sterują doborem Job_Link przy odświeżaniu Excela po włączeniu
JOB_TEMPLATE_AUTO_DISCOVERY. Nazwa trafia do kolumny template_job_name.
"""

from lib.awx_dependencies import ensure_virtual_environment

if __name__ == '__main__':
    ensure_virtual_environment()

from lib.awx import run


VARIABLES = {
    # Dokładna nazwa organizacji albo jej numeryczne ID.
    # 'JOB_TEMPLATE_ORGANIZATION': 'IT2',
    # Wyszukiwanie nazwy bez rozróżniania wielkości liter.
    # 'JOB_TEMPLATE_NAME_PATTERN': '*zabbix*it2*{os_name}*',
    # 'JOB_TEMPLATE_OS_NAMES': {'linux': 'unix', 'aix': 'unix', 'windows': 'window'},
    # 'DEBUG': True,
}


if __name__ == '__main__':
    raise SystemExit(run('templates', variables=VARIABLES))
