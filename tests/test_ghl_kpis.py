from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from config import settings
from crews.registry import CrewRegistry
from ghl_kpis import (GhlKpiSource, KpiSourceError, build_report, customer_events,
                      contact_events, is_kpi_request)

NOW = datetime(2026, 11, 1, 18, tzinfo=timezone.utc)
FIELDS = {'warm':'w','a2p':'a','sms':'s','account':'l'}


def payment(identifier='p1', contact='c1', at='2026-10-31T12:00:00Z', **values):
    return {'_id':identifier,'contactId':contact,'altId':'friday','altType':'location',
            'status':'succeeded','liveMode':True,'amount':'20','createdAt':at,**values}


def contact(**values):
    return {'id':'c1','locationId':'friday','tags':[],
            'customFields':[{'id':key,'value':value} for key,value in values.items()]}


class Source:
    location = 'friday'
    def __init__(self, data): self.data = data
    def collect(self, field_id=None):
        value = self.data.get(field_id, [])
        if isinstance(value, Exception): raise value
        return value


def report(source):
    return build_report(source, now=NOW, period='week', timezone_name='America/Los_Angeles',
                        fields=FIELDS, tracking_since=datetime(2026,10,1,tzinfo=timezone.utc))


def test_repeat_payments_not_new_customers_and_test_failed_free_excluded():
    rows = [payment(at='2026-10-01T12:00:00Z'),payment('renewal'),
            payment('new','c2'),payment('test','c3',liveMode=False),
            payment('failed','c4',status='failed'),payment('free','c5',amount='0')]
    metric = report(Source({None:rows}))['metrics']['customer_started']
    assert metric['value'] == 1


def test_empty_complete_payment_history_is_zero_not_unknown():
    data = report(Source({}))
    assert data['metrics']['customer_started']['value'] == 0
    assert data['metrics']['warm_conversation']['value'] is None
    assert data['metrics']['onboarding_completed']['value'] is None


def test_source_failure_never_zero():
    metric = report(Source({None:KpiSourceError('HTTP 403')}))['metrics']['customer_started']
    assert metric['value'] is None and '403' in metric['reason']


def test_warm_and_readiness_require_verified_dates_and_mapping():
    data = Source({'w':[contact(w='2026-10-31T12:00:00Z')],
                   'a':[contact(a='2026-10-30T12:00:00Z',s='2026-10-31T12:00:00Z',l='customer-account')]})
    result = report(data)
    assert result['metrics']['warm_conversation']['value'] == 1
    assert result['metrics']['onboarding_completed']['value'] == 1
    data.data['a']=[contact(a='2026-10-30T12:00:00Z')]
    assert report(data)['metrics']['onboarding_completed']['value'] is None
    data.data['a']=[contact(a='2026-10-30T12:00:00Z',s='2026-10-31T12:00:00Z')]
    assert report(data)['metrics']['onboarding_completed']['value'] is None


def test_invalid_dates_and_payment_identity_fail_closed():
    with pytest.raises(KpiSourceError): customer_events([payment(contact=None)], 'friday')
    with pytest.raises(KpiSourceError): customer_events([payment(liveMode=None)], 'friday')
    with pytest.raises(KpiSourceError): customer_events([payment(amount='NaN')], 'friday')
    result = report(Source({'w':[contact(w='yesterday')]}))
    assert result['metrics']['warm_conversation']['value'] is None


def test_http_source_is_readonly_strips_personal_data_and_paginates(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'totalCount':1,'data':[payment(contactName='Private Name',contactEmail='private@example.com')]})
    original = httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs: original(transport=httpx.MockTransport(handler),**kwargs))
    source=GhlKpiSource('synthetic-token','friday')
    try:
        data=source.collect()
        assert 'contactName' not in data[0] and 'contactEmail' not in data[0]
        assert requests[0].method=='GET'
        assert requests[0].url.params['altId']=='friday'
        with pytest.raises(KpiSourceError,match='only reporting reads'):
            source.request('POST','/contacts/c1/tags',json={'tags':['warm']})
    finally:source.close()


@pytest.mark.parametrize('mode',['wrong-tenant','partial','changing-total','duplicate'])
def test_pagination_refuses_incomplete_or_cross_tenant_data(monkeypatch,mode):
    calls=[]
    def handler(request):
        calls.append(request)
        row=payment(altId='other' if mode=='wrong-tenant' else 'friday')
        if mode=='partial' and len(calls)>1:rows=[]
        else:rows=[row]
        total=3 if mode=='changing-total' and len(calls)>1 else 2
        return httpx.Response(200,json={'totalCount':total,'data':rows})
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs: original(transport=httpx.MockTransport(handler),**kwargs))
    source=GhlKpiSource('token','friday')
    try:
        with pytest.raises(KpiSourceError):source.collect()
    finally:source.close()


def test_recognizes_owner_kpi_question_without_model_tools():
    assert is_kpi_request('Show my KPIs today')
    assert is_kpi_request('How many new customers this month?')
    assert not is_kpi_request('Draft a marketing post')


def test_chat_kpi_path_bypasses_model_and_exposes_only_report(monkeypatch):
    import comms,ghl_kpis
    registry=CrewRegistry(Path(__file__).resolve().parents[1]/'agents');registry.load_all()
    registry.invoke=AsyncMock(side_effect=AssertionError('No model allowed for counts'))
    monkeypatch.setattr(settings,'friday_kpis_enabled',True)
    monkeypatch.setattr(comms,'_load_history',lambda _:[])
    monkeypatch.setattr(comms,'_store',Mock())
    monkeypatch.setattr(ghl_kpis,'report_for_message',lambda *a:'Verified deterministic KPI report')
    app=SimpleNamespace(state=SimpleNamespace(registry=registry,audit=Mock()))
    out=asyncio.run(comms.handle_chat(app,'slack','thread','owner','Show KPIs today'))
    assert out['reply']=='Verified deterministic KPI report'
    registry.invoke.assert_not_called()


def test_complete_multiple_pages_succeed(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request)
        offset=int(request.url.params['offset'])
        rows=[payment('p'+str(i),'c'+str(i)) for i in range(offset,min(offset+100,101))]
        return httpx.Response(200,json={'totalCount':101,'data':rows})
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs: original(transport=httpx.MockTransport(handler),**kwargs))
    source=GhlKpiSource('token','friday')
    try:assert len(source.collect())==101 and len(requests)==2
    finally:source.close()


def test_date_window_before_tracking_is_explicitly_partial():
    source=Source({'w':[contact(w='2026-11-01T15:00:00Z')]})
    result=build_report(source,now=NOW,period='week',timezone_name='America/Los_Angeles',fields=FIELDS,
                        tracking_since=datetime(2026,11,1,12,tzinfo=timezone.utc))
    metric=result['metrics']['warm_conversation']
    assert metric['value']==1 and metric['status']=='partial'
