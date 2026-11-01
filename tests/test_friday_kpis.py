from datetime import datetime, timezone
import pytest
from friday_kpis import BusinessEvent, Coverage, reporting_window, summarize

START = datetime(2026, 11, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 2, tzinfo=timezone.utc)


def event(identifier='e1', subject='contact1', kind='warm_conversation', when=START, location='friday', test=False):
    return BusinessEvent('ghl', identifier, location, subject, kind, when, test)


def complete(kind='warm_conversation'):
    return Coverage('friday', kind, START, END, 'ghl', 'approved-test-definition')


def test_missing_data_is_unavailable_not_zero():
    report = summarize([], [], 'friday', START, END)
    assert all(m['value'] is None and m['status'] == 'unavailable' for m in report['metrics'].values())


def test_verified_empty_period_is_zero():
    metric = summarize([], [complete()], 'friday', START, END)['metrics']['warm_conversation']
    assert metric['value'] == 0 and metric['status'] == 'verified'


def test_duplicate_subjects_replays_tests_and_other_tenants():
    rows = [event(), event(), event('e2'), event('e3', test=True),
            event('e4', location='other'), event('e5', subject='contact2')]
    metric = summarize(rows, [complete()], 'friday', START, END)['metrics']['warm_conversation']
    assert metric['value'] == 2


def test_conflicting_replayed_event_fails_closed():
    with pytest.raises(ValueError, match='Conflicting'):
        summarize([event(), event(subject='other')], [complete()], 'friday', START, END)


def test_half_open_period_and_first_transition():
    earlier = datetime(2026, 10, 30, tzinfo=timezone.utc)
    rows = [event(), event('end', subject='at-end', when=END),
            event('old', subject='existing', when=earlier), event('repeat', subject='existing')]
    assert summarize(rows, [complete()], 'friday', START, END)['metrics']['warm_conversation']['value'] == 1


def test_customer_and_onboarding_are_separate_transitions():
    rows = [event(kind='customer_started'), event('renewal-duplicate', kind='customer_started'),
            event('onboard', kind='onboarding_completed')]
    report = summarize(rows, [complete('customer_started'), complete('onboarding_completed')], 'friday', START, END)
    assert report['metrics']['customer_started']['value'] == 1
    assert report['metrics']['onboarding_completed']['value'] == 1
    assert report['metrics']['warm_conversation']['value'] is None


def test_incomplete_period_is_not_treated_as_complete():
    coverage = Coverage('friday', 'warm_conversation', START, START.replace(hour=12), 'ghl', 'approved')
    assert summarize([event()], [coverage], 'friday', START, END)['metrics']['warm_conversation']['value'] is None


def test_conflicting_definitions_rejected():
    other = Coverage('friday', 'warm_conversation', START, END, 'other', 'different')
    with pytest.raises(ValueError, match='definitions'):
        summarize([], [complete(), other], 'friday', START, END)


def test_dst_day_and_week_are_calendar_bounds():
    start, end = reporting_window(START.replace(hour=12), period='day')
    assert (end.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds() == 25*3600
    start, end = reporting_window(START.replace(hour=12), period='week')
    assert start.weekday() == 0 and end.weekday() == 0


def test_naive_event_timestamp_rejected():
    with pytest.raises(ValueError, match='timezone'):
        event(when=datetime(2026, 11, 1))


def test_events_from_unverified_sources_are_not_counted():
    other = BusinessEvent('unverified', 'e2', 'friday', 'another', 'warm_conversation', START)
    metric = summarize([event(), other], [complete()], 'friday', START, END)['metrics']['warm_conversation']
    assert metric['value'] == 1


def test_onboarding_requires_both_a2p_and_usable_sms():
    from friday_kpis import onboarding_readiness_event
    args = ('ghl', 'readiness1', 'friday', 'customer1')
    assert onboarding_readiness_event(*args, a2p_completed_at=START) is None
    assert onboarding_readiness_event(*args, sms_usable_at=START) is None
    ready = onboarding_readiness_event(*args, a2p_completed_at=START, sms_usable_at=START.replace(hour=4))
    assert ready.kind == 'onboarding_completed' and ready.occurred_at.hour == 4
