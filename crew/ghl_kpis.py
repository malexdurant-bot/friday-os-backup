"""Read-only, tenant-fixed KPI reports. Raw CRM records never go to the model."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import time
from zoneinfo import ZoneInfo

import httpx
from friday_kpis import BusinessEvent, Coverage, onboarding_readiness_event, reporting_window, summarize

BASE = 'https://services.leadconnectorhq.com'
DEFINITIONS = {'warm_conversation': 'First recorded two-way conversation showing relevant need or buying interest',
               'customer_started': 'First successful live positive-value payment per GHL contact',
               'onboarding_completed': 'Customer A2P completed AND SMS usable, with customer-account mapping'}
LABELS = {'warm_conversation': 'Warm conversations', 'customer_started': 'New paying customers',
          'onboarding_completed': 'Onboarding completed (A2P + SMS ready)'}


class KpiSourceError(RuntimeError):
    pass


def instant(value):
    try:
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise KpiSourceError('Tracking date is missing or lacks a timezone') from None


class GhlKpiSource:
    def __init__(self, token, location_id):
        if not token or not location_id:
            raise KpiSourceError('GHL reporting credentials are not configured')
        self.location = location_id
        self.deadline = time.monotonic() + 60
        self.client = httpx.Client(headers={'Authorization': 'Bearer ' + token,
                                           'Version': '2021-07-28'},
                                   timeout=15, follow_redirects=False)

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        if (method, path) not in {('POST', '/contacts/search'), ('GET', '/payments/transactions')}:
            raise KpiSourceError('KPI source permits only reporting reads')
        if time.monotonic() > self.deadline:
            raise KpiSourceError('KPI source time limit reached; partial counts refused')
        try:
            with self.client.stream(method, BASE + path, **kwargs) as response:
                if response.status_code != 200:
                    raise KpiSourceError(f'GHL read returned HTTP {response.status_code}')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise KpiSourceError('KPI response exceeds size limit')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except httpx.HTTPError:
            raise KpiSourceError('GHL reporting connection unavailable') from None
        except (TypeError, ValueError):
            raise KpiSourceError('Invalid GHL reporting response') from None

    def collect(self, field_id=None):
        """Complete bounded pagination; refuse truncated, shifting or duplicate pages."""
        seen, expected = {}, None
        for page in range(1, 101):
            if field_id:
                data = self.request('POST', '/contacts/search', json={
                    'locationId': self.location, 'page': page, 'pageLimit': 100,
                    'filters': [{'field': 'customFields.' + field_id, 'operator': 'exists'}]})
                rows, total = data.get('contacts'), data.get('total')
            else:
                data = self.request('GET', '/payments/transactions', params={
                    'altId': self.location, 'altType': 'location', 'limit': 100, 'offset': (page-1)*100})
                rows, total = data.get('data'), data.get('totalCount')
            if not isinstance(rows, list) or type(total) is not int or total < 0 or total > 10000:
                raise KpiSourceError('KPI pagination metadata is invalid or exceeds limits')
            if expected is not None and total != expected:
                raise KpiSourceError('GHL data changed during pagination; retry report')
            expected = total
            for row in rows:
                key = row.get('id') if field_id else row.get('_id')
                location = row.get('locationId') if field_id else row.get('altId')
                if not key or key in seen or location != self.location:
                    raise KpiSourceError('Duplicate, invalid or cross-location record; count refused')
                # Strip personal names, addresses, emails, phone numbers and payment details immediately.
                keep = ('id', 'locationId', 'tags', 'customFields') if field_id else (
                    '_id', 'altId', 'altType', 'contactId', 'status', 'liveMode', 'amount', 'createdAt')
                seen[key] = {k: row.get(k) for k in keep}
            if len(seen) == expected:
                return list(seen.values())
            if not rows or len(seen) > expected:
                raise KpiSourceError('Incomplete GHL pagination; partial counts refused')
        raise KpiSourceError('KPI pagination limit reached; partial counts refused')


def _test_contact(row):
    return bool({str(t).casefold() for t in row.get('tags') or []} & {'test', 'test list', 'test-tag', 'kpi:test'})


def contact_events(rows, location, warm_field, a2p_field, sms_field, account_field, kind):
    events = []
    for row in rows:
        if _test_contact(row):
            continue
        fields = {x['id']: x.get('value') for x in row.get('customFields') or [] if 'id' in x}
        if kind == 'warm_conversation':
            if not fields.get(warm_field):
                raise KpiSourceError('GHL warm-conversation filter did not match its tracking field')
            at = instant(fields[warm_field])
            events.append(BusinessEvent('ghl:markers', 'warm:'+row['id'], location, row['id'], kind, at))
        elif fields.get(a2p_field) and fields.get(sms_field):
            if not fields.get(account_field):
                raise KpiSourceError('Completed readiness dates lack the customer account mapping')
            event = onboarding_readiness_event('ghl:markers', 'onboard:'+row['id'], location, row['id'],
                a2p_completed_at=instant(fields[a2p_field]), sms_usable_at=instant(fields[sms_field]))
            events.append(event)
    return events


def customer_events(rows, location):
    events = []
    for row in rows:
        if row.get('status') != 'succeeded':
            continue
        live = row.get('liveMode')
        if live is False or live == 'false':
            continue
        if live is not True and live != 'true':
            raise KpiSourceError('Successful payment has unknown live/test status')
        try:
            amount = Decimal(str(row.get('amount')))
            if not amount.is_finite():
                raise InvalidOperation()
            if amount <= 0:
                continue
        except InvalidOperation:
            raise KpiSourceError('Payment amount is invalid') from None
        if not row.get('contactId') or row.get('altId') != location or row.get('altType') != 'location':
            raise KpiSourceError('Payment has no verified customer/location identity')
        events.append(BusinessEvent('ghl:payments', row['_id'], location, row['contactId'],
                                    'customer_started', instant(row['createdAt'])))
    return events


def build_report(source, *, now, period, timezone_name, fields, tracking_since):
    start, _ = reporting_window(now, timezone_name, period)
    result = {'location_id': source.location, 'start': start.isoformat(), 'end_exclusive': now.isoformat(),
              'timezone': timezone_name, 'period': period, 'metrics': {}, 'definitions': DEFINITIONS}
    for kind in DEFINITIONS:
        try:
            if kind == 'customer_started':
                events = customer_events(source.collect(), source.location)
                lower = start
            else:
                required = [fields.get('warm')] if kind == 'warm_conversation' else [fields.get(k) for k in ('a2p','sms','account')]
                if not all(required) or tracking_since is None:
                    raise KpiSourceError('Tracking fields have not been configured')
                rows = source.collect(fields['warm'] if kind == 'warm_conversation' else fields['a2p'])
                events = contact_events(rows, source.location, fields.get('warm'), fields.get('a2p'),
                                        fields.get('sms'), fields.get('account'), kind)
                if not events:
                    raise KpiSourceError('No verified tracking events recorded yet; not a measured zero')
                lower = max(start, tracking_since)
            if lower >= now:
                raise KpiSourceError('Tracking begins after this reporting interval')
            origin = 'ghl:payments' if kind == 'customer_started' else 'ghl:markers'
            coverage = [Coverage(source.location, kind, lower, now, origin, DEFINITIONS[kind])]
            metric = summarize(events, coverage, source.location, lower, now)['metrics'][kind]
            if lower > start:
                metric.update(status='partial', since=lower.isoformat())
            result['metrics'][kind] = metric
        except (KpiSourceError, ValueError, KeyError, TypeError, AttributeError) as error:
            # Only expose errors authored in this module, never raw upstream data.
            result['metrics'][kind] = {'status': 'unavailable', 'value': None,
                'reason': str(error) if isinstance(error, KpiSourceError) else 'Source tracking data is invalid'}
    return result


def is_kpi_request(message):
    return bool(re.search(r'\bkpis?\b|\bhow many\b.*\b(leads|customers|onboardings|onboarded)\b', message, re.I))


def report_for_message(message, settings):
    if re.search(r'\byesterday\b|\blast\b|\b20\d{2}[-/]\d', message, re.I):
        return 'KPI reporting currently supports today, this week and this month. Which of those should I show?'
    period = 'day' if re.search(r'\btoday\b', message, re.I) else 'month' if re.search(r'\bmonth\b', message, re.I) else 'week'
    token = settings.ghl_api_key
    source = GhlKpiSource(token.get_secret_value() if token else None, settings.ghl_location_id)
    try:
        report = build_report(source, now=datetime.now(timezone.utc), period=period,
            timezone_name=settings.kpi_timezone, tracking_since=settings.kpi_tracking_since,
            fields={'warm': settings.kpi_warm_field_id, 'a2p': settings.kpi_a2p_field_id,
                    'sms': settings.kpi_sms_field_id, 'account': settings.kpi_customer_location_field_id})
    finally:
        source.close()
    label = {'day':'today','week':'this week to date','month':'this month to date'}[period]
    zone = ZoneInfo(report['timezone'])
    beginning = instant(report['start']).astimezone(zone)
    updated = instant(report['end_exclusive']).astimezone(zone)
    lines = [f'Friday Ai KPIs — {label}',
             f'{beginning:%b %d, %I:%M %p %Z} to {updated:%b %d, %I:%M %p %Z}']
    for kind, metric in report['metrics'].items():
        value = str(metric['value']) if metric['value'] is not None else 'not available'
        lines.append(f'• {LABELS[kind]}: {value}')
        if metric['status'] == 'unavailable':
            reason = metric['reason']
            if 'No verified tracking events' in reason:
                reason = ('No warm conversations have been marked yet.' if kind == 'warm_conversation'
                          else 'No customer has both A2P and usable SMS recorded yet.')
            elif 'HTTP 401' in reason or 'HTTP 403' in reason:
                reason = 'GHL reporting access needs attention.'
            elif 'not been configured' in reason:
                reason = 'Tracking setup is incomplete.'
            else:
                reason = 'The source data could not be fully verified; try again later.'
            lines.append('  '+reason)
        elif metric['status'] == 'partial':
            lines.append('  Recorded tracking only, since '+instant(metric['since']).astimezone(zone).strftime('%b %d, %I:%M %p %Z'))
    lines.append(f'Source: live GHL records for Friday Ai. Updated {updated:%b %d, %I:%M %p %Z}.')
    lines.append('New customers use each contact’s first successful live payment transaction date; repeat payments do not count again. Lead and onboarding history starts with the dedicated tracking fields.')
    return '\n'.join(lines)
