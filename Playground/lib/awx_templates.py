"""Organization-scoped template lookup shared by console search and Excel refresh."""

import json
import re
from copy import deepcopy
from string import Formatter

from . import awx_config as common
from .awx_config import AWXError
from .awx_dependencies import ensure_yaml


def find_organization(client, selector, *, by_name=False):
    numeric = not by_name and (type(selector) is int or isinstance(selector, str) and re.fullmatch('[0-9]+', selector))
    key = ('id', int(selector)) if numeric else ('name', selector.casefold())
    if key not in client.organization_cache:
        if numeric:
            matches = [client.get(f'organizations/{int(selector)}/', purpose=f'Odczyt organizacji ID {selector}')]
        else:
            matches = [item for item in client.items('organizations/', name__iexact=selector,
                                                    purpose=f'Wyszukanie organizacji {selector!r}')
                       if item.get('name', '').casefold() == selector.casefold()]
        if not matches:
            raise AWXError(f'No accessible organization named {selector!r} was found.')
        if len(matches) != 1:
            raise AWXError(f'Organization name {selector!r} is ambiguous; use its numeric ID.')
        organization = matches[0]
        if type(organization.get('id')) is not int or organization['id'] < 1:
            raise AWXError('Invalid organization response from AWX.')
        client.organization_cache[key] = organization
    return client.organization_cache[key]


def name_match_groups(settings, host=None):
    """Require all name fragments in any order; each OS variant forms a group."""
    pattern = settings['JOB_TEMPLATE_NAME_PATTERN']
    try:
        terms = [list(Formatter().parse(term)) for term in re.split(r'[\s*]+', pattern) if term]
    except ValueError as exc:
        raise AWXError(f'Invalid JOB_TEMPLATE_NAME_PATTERN: {exc}') from None
    parts = [part for term in terms for part in term]
    fields = {field for _, field, _, _ in parts if field is not None}
    allowed = {'hostname', 'application_id', 'ostype', 'os_name'}
    if fields - allowed or any(spec or conversion for _, _, spec, conversion in parts):
        raise AWXError('JOB_TEMPLATE_NAME_PATTERN supports only {hostname}, {application_id}, {ostype}, and {os_name}.')
    names = settings['JOB_TEMPLATE_OS_NAMES']
    if host is not None:
        ostype = common.normalize_ostype(str(host.get('ostype') or ''))
        contexts = [{'hostname': host['name'], 'application_id': host.get('ApplicationID') or
                     host.get('INVENTORY', '').split('_', 1)[0],
                     'ostype': ostype, 'os_name': names.get(ostype)}]
    elif fields & {'hostname', 'application_id'}:
        raise AWXError('This JOB_TEMPLATE_NAME_PATTERN requires a host; use --hostname or Excel refresh.')
    else:
        contexts = [{'ostype': ostype, 'os_name': name} for ostype, name in names.items()]
    groups = []
    for context in contexts:
        expressions = []
        for term in terms:
            chunks = []
            for literal, field, _, _ in term:
                chunks.append(''.join('.' if char == '?' else re.escape(char) for char in literal))
                if field is not None:
                    if not context.get(field):
                        label = host['name'] if host is not None else 'search'
                        raise AWXError(f'Cannot expand {{{field}}} for {label!r}. Check ostype and JOB_TEMPLATE_OS_NAMES.')
                    chunks.append(re.escape(str(context[field])))
            expressions.append(''.join(chunks))
        group = tuple(dict.fromkeys(expressions))
        if group not in groups:
            groups.append(group)
    return groups


def name_regexes(settings, host=None):
    """Equivalent Python regexes, kept for callers checking a complete name."""
    return ['(?s)^' + ''.join(f'(?=.*{term})' for term in group) + '.*$'
            for group in name_match_groups(settings, host)]


def find_templates(client, settings, host=None, *, organization_name=None):
    selector = settings['JOB_TEMPLATE_ORGANIZATION'] if organization_name is None else organization_name
    organization = find_organization(client, selector, by_name=organization_name is not None)
    groups = name_match_groups(settings, host)
    key = (organization['id'], tuple(groups))
    if key not in client.template_search_cache:
        filters = {'order_by': 'id'}
        # Use a simple API prefilter; require every fragment locally. The API
        # receives no Python lookaheads and cannot impose a word order.
        if all(groups):
            filters['name__iregex'] = '(' + '|'.join(dict.fromkeys(group[-1] for group in groups)) + ')'
        matches = {}
        for template in client.items(f"organizations/{organization['id']}/job_templates/",
                                     **filters,
                                     purpose=f"Wyszukanie szablonów jobów w organizacji {organization.get('name', organization['id'])!r}"):
            if any(all(re.search(term, template.get('name', ''), re.IGNORECASE) for term in group)
                   for group in groups):
                if type(template.get('id')) is not int or template['id'] < 1:
                    raise AWXError('Invalid job template response from AWX.')
                matches[template['id']] = template
        client.template_search_cache[key] = sorted(matches.values(), key=lambda item: item['id'])
    return organization, client.template_search_cache[key]


def template_details(client, template_id):
    if template_id not in client.template_detail_cache:
        details = client.get(f'job_templates/{template_id}/', purpose=f'Odczyt definicji szablonu ID {template_id}')
        if details.get('id') != template_id or not isinstance(details.get('name'), str) or not details['name'].strip():
            raise AWXError(f'Invalid job template definition for ID {template_id}.')
        client.template_detail_cache[template_id] = details
    return client.template_detail_cache[template_id]


def template_for_host(client, settings, host):
    application_id = str(host.get('ApplicationID') or host.get('INVENTORY', '').split('_', 1)[0]).strip()
    if not application_id or application_id.casefold() == 'none':
        raise AWXError(f"Host {host['name']!r}: ApplicationID is required to find its AWX organization.")
    _, matches = find_templates(client, settings, host, organization_name=application_id)
    label = f"Host {host['name']!r}, ostype={host.get('ostype')!r}"
    if not matches:
        raise AWXError(f"{label}: no job template matches words {settings['JOB_TEMPLATE_NAME_PATTERN']!r} "
                       f"in organization {application_id!r} (ApplicationID).")
    if len(matches) != 1:
        found = ', '.join(f"{item['id']}: {item['name']}" for item in matches)
        raise AWXError(f'{label}: ambiguous job templates ({found}). Narrow JOB_TEMPLATE_NAME_PATTERN.')
    return template_details(client, matches[0]['id'])


def template_variables(template):
    value = template.get('extra_vars') or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            yaml = ensure_yaml()
            try:
                value = yaml.safe_load(value)
            except yaml.YAMLError:
                raise AWXError(f"Template {template['id']}: invalid extra_vars YAML/JSON.") from None
        if value is None:
            value = {}
    if not isinstance(value, dict):
        raise AWXError(f"Template {template['id']}: extra_vars must be an object.")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise AWXError(f"Template {template['id']}: extra_vars must contain JSON-compatible values.") from None
    return deepcopy(value)


def main(argv=None):
    from . import awx

    parser = awx.operation_parser('Wyszukaj i wyświetl szablony jobów AWX.',
                                  with_sheet=False, with_file=False, operation='templates')
    parser.add_argument('--organization', help='Dokładna nazwa lub ID organizacji; nadpisuje JOB_TEMPLATE_ORGANIZATION.')
    parser.add_argument('--name-pattern', '--name', help='Wzorzec nazwy; nadpisuje JOB_TEMPLATE_NAME_PATTERN.')
    parser.add_argument('--hostname', help='Host do ustalenia ApplicationID i ostype z inventory AWX.')
    args = awx.parse_operation_arguments(parser, argv)
    settings = common.read_template_settings()
    if args.organization is not None:
        settings['JOB_TEMPLATE_ORGANIZATION'] = args.organization
    if args.name_pattern is not None:
        settings['JOB_TEMPLATE_NAME_PATTERN'] = args.name_pattern
    common.normalize_template_settings(settings, require_organization=args.hostname is None)
    awx.validate_connection_settings(settings)
    awx.configure_debug(args.debug, settings['AWX_TOKEN'].strip())
    awx.apply_connection_settings(settings)
    client = awx.AWXClient(settings['AWX_URL'], settings['AWX_TOKEN'].strip())
    host = None
    if args.hostname is not None:
        names = common.normalize_hostnames(args.hostname, required=True)
        if len(names) != 1:
            raise AWXError('--hostname requires one literal hostname.')
        report = awx.fetch_refresh_report(client, names, settings['REFRESH_INVENTORY_PATTERN'])
        if not report['hosts']:
            raise AWXError(f'Host {names[0]!r} was not found in inventories matching REFRESH_INVENTORY_PATTERN.')
        host = report['hosts'][0]
    organization_name = None
    if host is not None and args.organization is None:
        organization_name = host['INVENTORY'].split('_', 1)[0].strip()
    organization, matches = find_templates(client, settings, host, organization_name=organization_name)
    for template in matches:
        link = client._resolve_url(f"job_templates/{template['id']}/")
        awx.console.print_message(client.redact(
            f"template_job_name: {template['name']}\nID: {template['id']}\n"
            f"Organizacja: {organization.get('name', organization['id'])}\nJob_Link: {link}"
        ), level='data')
    if not matches:
        awx.console.print_message('Nie znaleziono szablonów pasujących do organizacji i wzorca nazwy.', level='warning')
    return 0
