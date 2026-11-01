"""Deterministic KPI counting over verified business events, not model guesses.

Source adapters must map approved business definitions and establish coverage.
This module performs no network calls, CRM writes, or model work.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KINDS = ('warm_conversation', 'customer_started', 'onboarding_completed')


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('KPI event times and reporting bounds must include a timezone')
    return value


@dataclass(frozen=True)
class BusinessEvent:
    source: str
    event_id: str
    location_id: str
    subject_id: str
    kind: str
    occurred_at: datetime
    is_test: bool = False

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError('Unsupported KPI event kind')
        if not all(isinstance(v, str) and v.strip() for v in (
                self.source, self.event_id, self.location_id, self.subject_id)):
            raise ValueError('KPI event requires source, event, location and subject IDs')
        _aware(self.occurred_at)


@dataclass(frozen=True)
class Coverage:
    """A source's explicit guarantee that this event stream is fully observed."""
    location_id: str
    kind: str
    start: datetime
    end: datetime
    source: str
    definition: str

    def __post_init__(self):
        if self.kind not in KINDS or not self.location_id or not self.source or not self.definition:
            raise ValueError('KPI coverage requires an approved definition and source')
        if _aware(self.end) <= _aware(self.start):
            raise ValueError('Invalid coverage interval')


def reporting_window(day, timezone='America/Los_Angeles', period='week'):
    """Calendar reporting bounds honor local midnight and DST, with exclusive end."""
    zone = ZoneInfo(timezone)
    local = _aware(day).astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'day':
        end = start + timedelta(days=1)
    elif period == 'week':
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == 'month':
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        raise ValueError('KPI period must be day, week or month')
    return start, end


def summarize(events, coverage, location_id, start, end):
    """Count distinct subjects' first authoritative transition, excluding tests.

    customer_started must mean the FIRST customer event, not every payment.
    Its adapter must establish first-customer status; renewals are not this event.
    Warm conversations require an approved warmth signal, not contact creation.
    Onboarding means A2P completed AND SMS usable, not first workflow success.
    Current tags/stages alone do not establish a historical transition date.
    """
    if not location_id or _aware(end) <= _aware(start):
        raise ValueError('KPI report requires one location and valid time bounds')
    unique = {}
    for event in events:
        if event.location_id != location_id:
            continue
        key = (event.source, event.event_id)
        if key in unique and unique[key] != event:
            raise ValueError('Conflicting KPI source event; report refused')
        unique[key] = event
    result = {'location_id': location_id, 'start': start.isoformat(),
              'end_exclusive': end.isoformat(), 'metrics': {}}
    for kind in KINDS:
        known = [c for c in coverage if c.location_id == location_id and c.kind == kind
                 and c.start <= start and c.end >= end]
        definitions = {c.definition for c in known}
        if len(definitions) > 1:
            raise ValueError('Conflicting KPI definitions; report refused')
        if not known:
            result['metrics'][kind] = {'status': 'unavailable', 'value': None,
                                       'reason': 'Verified source coverage is missing'}
            continue
        sources = {c.source for c in known}
        first = {}
        for event in unique.values():
            if event.kind != kind or event.is_test or event.source not in sources:
                continue
            first[event.subject_id] = min(first.get(event.subject_id, event.occurred_at), event.occurred_at)
        result['metrics'][kind] = {
            'status': 'verified',
            'value': sum(start <= instant < end for instant in first.values()),
            'definition': known[0].definition,
            'sources': sorted({c.source for c in known}),
        }
    return result


def onboarding_readiness_event(source, event_id, location_id, subject_id,
                               *, a2p_completed_at=None, sms_usable_at=None):
    """Only verified evidence of BOTH conditions can establish onboarding completion.

    Adapters must map this customer to their own SMS account; the agency's or
    Friday Ai account's A2P approval is not proof about every customer's account.
    Missing timestamps are unknown, not an inferred completion or a zero count.
    """
    if a2p_completed_at is None or sms_usable_at is None:
        return None
    completed = max(_aware(a2p_completed_at), _aware(sms_usable_at))
    return BusinessEvent(source, event_id, location_id, subject_id,
                         'onboarding_completed', completed)
