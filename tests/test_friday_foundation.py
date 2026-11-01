import asyncio
import copy
from contextlib import contextmanager
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from config import Settings, settings
from friday_policy import FRIDAY_ID, validate_friday
from crews.registry import CrewRegistry
import comms
import main

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def registry():
    r = CrewRegistry(ROOT / 'agents')
    r.load_all()
    return r


def test_only_friday_loaded(registry):
    assert list(registry.agents) == [FRIDAY_ID]
    assert not registry.crews and not registry.workflow_map
    assert registry._build_tools(registry.get_agent(FRIDAY_ID)) == []
    assert not any(name in sys.modules for name in ('tools.ghl', 'tools.memory', 'tools.clickup'))


@pytest.mark.parametrize('field,value', [
    ('id', 'auto-ceo'), ('schedule', [{'cron': '* * * * *'}]),
    ('data_sources', ['customer_database']), ('llm_model', 'surprise/model'),
])
def test_definition_rejects_capability_drift(registry, field, value):
    data = copy.deepcopy(registry.get_agent(FRIDAY_ID))
    data[field] = value
    with pytest.raises(ValueError):
        validate_friday(data)


@pytest.mark.parametrize('permission,value', [
    ('can_call_tools', ['ghl.read_contacts']),
    ('can_delegate_to', ['auto-ceo']), ('always_require_approval', True),
])
def test_permissions_rejected(registry, permission, value):
    registry.agents[FRIDAY_ID]['permissions'][permission] = value
    with pytest.raises(ValueError):
        registry._build_tools(registry.agents[FRIDAY_ID])
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke(FRIDAY_ID, 'hello', {}))


def test_missing_definition_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        CrewRegistry(tmp_path).load_all()


def test_workforce_profile_not_configurable():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_profile='workforce')


def test_other_execution_paths_rejected(registry):
    registry.agents['auto-ceo'] = registry.agents[FRIDAY_ID]
    with pytest.raises(ValueError):
        asyncio.run(registry.invoke('auto-ceo', 'hello', {}))
    with pytest.raises(PermissionError):
        asyncio.run(registry.run_crew('daily-kpi-brief', {}))
    with pytest.raises(PermissionError):
        asyncio.run(registry.resume_after_approval({'agent_id': FRIDAY_ID}))


def test_direct_routing_and_explicit_escape(registry):
    assert comms.resolve_agent('hello', registry) == (FRIDAY_ID, 'hello')
    assert comms.resolve_agent('@friday-operations hello', registry) == (FRIDAY_ID, 'hello')
    with pytest.raises(ValueError):
        comms.resolve_agent('@auto-ceo hello', registry)


def test_chat_does_not_classify_or_request_approval(registry, monkeypatch):
    classifier = AsyncMock(side_effect=AssertionError('classifier must not run'))
    monkeypatch.setattr(comms, '_route_agent', classifier)
    monkeypatch.setattr(comms, '_load_history', lambda _: [])
    store = Mock()
    monkeypatch.setattr(comms, '_store', store)
    registry.invoke = AsyncMock(return_value='Advisory draft only')
    approval = SimpleNamespace(request=AsyncMock(side_effect=AssertionError('no approval actions')))
    app = SimpleNamespace(state=SimpleNamespace(registry=registry, audit=Mock(), approval=approval))
    result = asyncio.run(comms.handle_chat(app, 'local', 'test', 'owner', 'send an email'))
    assert result['agent_id'] == FRIDAY_ID
    classifier.assert_not_called()
    approval.request.assert_not_called()
    assert 'no tools or connected systems' in registry.invoke.call_args.kwargs['task']
    assert store.call_count == 2


def test_invocation_has_no_context_or_tools(registry, monkeypatch):
    observed = {}
    class Agent:
        def __init__(self, **kwargs): observed['agent'] = kwargs
    class Task:
        def __init__(self, **kwargs): observed['task'] = kwargs
    class Crew:
        def __init__(self, **kwargs): observed['crew'] = kwargs
        def kickoff(self): return 'Offline stub response'
    fake = SimpleNamespace(Agent=Agent, Task=Task, Crew=Crew, Process=SimpleNamespace(sequential='sequential'))
    monkeypatch.setitem(sys.modules, 'crewai', fake)
    monkeypatch.setattr(settings, 'friday_model', 'openai/gpt-4.1-mini')
    @contextmanager
    def reserve(*args):
        yield 123
    registry.set_governance(SimpleNamespace(enabled=True, friday_run=reserve, finish_friday_run=Mock()))
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, reported_cost=0)
    monkeypatch.setattr(registry, '_build_llm', lambda *a, **k: (usage, 'openai/gpt-4.1-mini'))
    for name in ('_get_memory', '_get_business_context'):
        monkeypatch.setattr(registry, name, Mock(side_effect=AssertionError('business data forbidden')))
    result = asyncio.run(registry.invoke(FRIDAY_ID, 'Help prioritize my tasks', {}))
    assert result == 'Offline stub response'
    assert observed['agent']['tools'] == []
    assert observed['agent']['allow_delegation'] is False
    assert observed['agent']['verbose'] is False
    assert observed['crew']['verbose'] is False
    assert len(observed['crew']['agents']) == 1
    assert observed['agent']['max_retry_limit'] == 0
    registry._governance.finish_friday_run.assert_called_once()


def test_model_must_be_explicit(registry, monkeypatch):
    llm = Mock()
    monkeypatch.setitem(sys.modules, 'crewai', SimpleNamespace(LLM=llm))
    monkeypatch.setattr(settings, 'friday_model', None)
    with pytest.raises(ValueError, match='FRIDAY_MODEL'):
        registry._build_llm(registry.agents[FRIDAY_ID])
    llm.assert_not_called()


@pytest.mark.parametrize('path', [
    '/agents/auto-ceo/invoke', '/crews/daily-kpi-brief/run', '/workflows/trigger',
    '/slack/events', '/slack/interactions', '/approvals/example', '/governance/trust/flip',
])
def test_http_blocked_surfaces(path):
    # No context manager: do not start lifespan or open a database.
    response = TestClient(main.app).post(path, json={})
    assert response.status_code == 403


def test_http_authentication_and_inventory(registry, monkeypatch):
    monkeypatch.setattr(main.app.state, 'registry', registry, raising=False)
    client = TestClient(main.app)
    monkeypatch.setattr(settings, 'agent_runtime_api_key', None)
    assert client.get('/agents').status_code == 503
    monkeypatch.setattr(settings, 'agent_runtime_api_key', 'offline-test-only')
    assert client.get('/agents').status_code == 401
    assert client.get('/agents', headers={'x-api-key': 'wrong'}).status_code == 401
    r = client.get('/agents', headers={'x-api-key': 'offline-test-only'})
    assert r.status_code == 200 and r.json()['total'] == 1
    assert r.json()['friday'][0]['id'] == FRIDAY_ID
    r = client.get('/crews', headers={'x-api-key': 'offline-test-only'})
    assert r.json()['total'] == 0


def test_startup_does_not_schedule_or_enable_slack(monkeypatch):
    monkeypatch.setattr(settings, 'agents_path', ROOT / 'agents')
    monkeypatch.setattr(main, 'AuditLogger', Mock())
    monkeypatch.setattr(main, 'GovernanceStore', Mock())
    approval = Mock()
    monkeypatch.setattr(main, 'ApprovalManager', approval)
    watchdog = AsyncMock(side_effect=AssertionError('watchdog forbidden'))
    monkeypatch.setattr(main, '_n8n_watchdog', watchdog)
    async def check():
        async with main.lifespan(main.app):
            assert main.app.state.n8n_watchdog is None
            assert list(main.app.state.registry.agents) == [FRIDAY_ID]
    asyncio.run(check())
    watchdog.assert_not_called()
    assert approval.call_args.kwargs['slack_token'] is None


def test_http_chat_cannot_select_workforce(registry, monkeypatch):
    monkeypatch.setattr(main.app.state, 'registry', registry, raising=False)
    monkeypatch.setattr(main.app.state, 'audit', Mock(), raising=False)
    monkeypatch.setattr(settings, 'agent_runtime_api_key', 'offline-test-only')
    response = TestClient(main.app).post('/chat',
        headers={'x-api-key': 'offline-test-only'},
        json={'channel': 'local', 'conversation_id': 'test', 'text': '@auto-ceo hello'})
    assert response.status_code == 400


def test_governance_required_before_invocation(registry, monkeypatch):
    monkeypatch.setattr(settings, 'friday_model', 'openai/gpt-4.1-mini')
    with pytest.raises(RuntimeError, match='GOVERNANCE_UNAVAILABLE'):
        asyncio.run(registry.invoke(FRIDAY_ID, 'hello', {}))


def test_oversized_input_rejected(registry):
    with pytest.raises(ValueError, match='character limit'):
        asyncio.run(registry.invoke(FRIDAY_ID, 'x' * 9000, {}))


def test_startup_requires_storage(monkeypatch):
    monkeypatch.setattr(settings, 'agents_path', ROOT / 'agents')
    monkeypatch.setattr(main, 'AuditLogger', lambda **k: SimpleNamespace(enabled=False))
    monkeypatch.setattr(main, 'GovernanceStore', lambda **k: SimpleNamespace(enabled=False))
    async def check():
        async with main.lifespan(main.app):
            pytest.fail('Startup should have failed')
    with pytest.raises(RuntimeError, match='storage'):
        asyncio.run(check())
