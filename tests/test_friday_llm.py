import httpx
import pytest

# Real CrewAI import: test runtime must include the pinned runtime dependencies.
from friday_llm import FridayLLM


def response(status=200, **overrides):
    data = {'choices': [{'message': {'content': 'Final Answer: Advisory only'}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'cost': 0.000024}}
    data.update(overrides)
    return httpx.Response(status, json=data)


@pytest.fixture
def adapter(monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append(kwargs)
            return response()
    monkeypatch.setattr(httpx, 'Client', Client)
    return FridayLLM('openai/gpt-4.1-mini', 'offline-only', 256, 0.01), calls


def test_bounded_adapter_usage_and_call_limit(adapter):
    llm, calls = adapter
    for _ in range(2):
        assert 'Advisory' in llm.call('hello')
    with pytest.raises(RuntimeError, match='call limit'):
        llm.call('third')
    assert len(calls) == 2
    assert llm.prompt_tokens == 40 and llm.completion_tokens == 20
    assert calls[0]['json']['provider']['allow_fallbacks'] is False
    assert calls[0]['json']['max_tokens'] == 256
    assert 'tools' not in calls[0]['json']


def test_tools_and_large_messages_refused_without_network(adapter):
    llm, calls = adapter
    with pytest.raises(RuntimeError, match='tools'):
        llm.call('hello', tools=[{'name': 'crm'}])
    with pytest.raises(RuntimeError, match='size limit'):
        llm.call('x' * 100000)
    with pytest.raises(RuntimeError, match='budget'):
        llm.call('x' * 24000)
    assert not calls


def test_provider_error_sanitized_without_retry(monkeypatch):
    attempts = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            attempts.append(1)
            return response(401, error='SENSITIVE_RESPONSE_MUST_NOT_APPEAR')
    monkeypatch.setattr(httpx, 'Client', Client)
    llm = FridayLLM('openai/gpt-4.1-mini', 'offline-only', 256, 0.01)
    with pytest.raises(RuntimeError) as caught:
        llm.call('hello')
    assert '401' in str(caught.value)
    assert 'SENSITIVE' not in str(caught.value)
    assert len(attempts) == 1


def test_missing_usage_refused(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return response(usage={})
    monkeypatch.setattr(httpx, 'Client', Client)
    llm = FridayLLM('openai/gpt-4.1-mini', 'offline-only', 256, 0.01)
    with pytest.raises(RuntimeError, match='usage missing'):
        llm.call('hello')
