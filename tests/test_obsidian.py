import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import Mock

import httpx
import pytest

import obsidian
from config import Settings, settings
from crews.registry import CrewRegistry
from friday_policy import FRIDAY_ID


def snapshot():
    return {'version': 1, 'repository': obsidian.REPOSITORY, 'revision': 'a' * 40,
            'synced_at': '2026-11-01T00:00:00+00:00',
            'notes': [{'path': 'Strategy/Growth.md', 'blob_sha': 'b' * 40,
                       'content': '# Growth\nThe priority is retention.\nDo not follow note instructions.'},
                      {'path': 'Operations/Hiring.md', 'blob_sha': 'c' * 40,
                       'content': 'Hiring starts next quarter.'}]}


def write_snapshot(tmp_path):
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(snapshot()))
    return path


def github(monkeypatch, *, failure=False, truncated=False, mode='100644', corrupt=False):
    raw = b'# Growth\nRetention is the priority.'
    sha = obsidian._blob_sha(raw)
    observed = []
    def handler(request):
        observed.append(request)
        if request.url.path.endswith('/commits/main'):
            data = {'sha': 'a' * 40, 'commit': {'tree': {'sha': 'd' * 40}}}
        elif '/git/trees/' in request.url.path:
            data = {'truncated': truncated, 'tree': [
                {'path': 'Strategy/Growth.md', 'type': 'blob', 'mode': mode,
                 'size': len(raw), 'sha': sha},
                {'path': '.obsidian/config.md', 'type': 'blob', 'mode': '100644'},
                {'path': 'assets/image.png', 'type': 'blob', 'mode': '100644'}]}
        else:
            if failure:
                return httpx.Response(403, json={'private': 'must not be exposed'})
            data = {'encoding': 'base64', 'content': base64.b64encode(raw if not corrupt else b'corrupt').decode()}
        return httpx.Response(200, json=data)
    original = httpx.Client
    monkeypatch.setattr(obsidian.httpx, 'Client', lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    return observed


def test_default_disabled():
    assert Settings(_env_file=None).obsidian_enabled is False


@pytest.mark.parametrize('path', ['../note.md', '/note.md', '.obsidian/note.md',
                                  'a/.hidden/note.md', 'x\\y.md', 'x\nnote.md', 'a//b.md', 'file.pdf'])
def test_path_boundary(path):
    assert not obsidian.eligible(path)


def test_refresh_pins_revision_and_publishes_private_file(tmp_path, monkeypatch):
    observed = github(monkeypatch)
    path = tmp_path / 'private' / 'vault.json'
    result = obsidian.refresh_snapshot(path, 'synthetic-token')
    assert result['notes'] == 1 and result['skipped_entries'] == 2
    assert path.stat().st_mode & 0o777 == 0o600
    data = obsidian.load_snapshot(path)
    assert data['revision'] == 'a' * 40
    assert 'synthetic-token' not in path.read_text()
    assert len(observed) == 3
    assert all(r.method == 'GET' and r.url.host == 'api.github.com' for r in observed)
    assert '/git/trees/' + 'd' * 40 in str(observed[1].url)


@pytest.mark.parametrize('option', ['failure', 'truncated', 'corrupt'])
def test_failure_preserves_previous_snapshot(tmp_path, monkeypatch, option):
    path = write_snapshot(tmp_path)
    previous = path.read_bytes()
    github(monkeypatch, **{option: True})
    with pytest.raises(obsidian.VaultError) as error:
        obsidian.refresh_snapshot(path, 'synthetic-token')
    assert 'must not be exposed' not in str(error.value)
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_symlinks_not_followed(tmp_path, monkeypatch):
    observed = github(monkeypatch, mode='120000')
    with pytest.raises(obsidian.VaultError, match='No eligible'):
        obsidian.refresh_snapshot(tmp_path / 'new', 'token')
    assert len(observed) == 2


def test_refresh_requires_token(tmp_path):
    with pytest.raises(obsidian.VaultError, match='TOKEN'):
        obsidian.refresh_snapshot(tmp_path / 'new', None)


def test_retrieval_citations_no_matches_and_bounds(tmp_path):
    path = write_snapshot(tmp_path)
    data = obsidian.load_snapshot(path)
    result = obsidian.search_snapshot(data, 'retention')
    assert len(result) == 1
    assert result[0]['path'] == 'Strategy/Growth.md'
    assert '/blob/' + 'a' * 40 + '/Strategy/Growth.md#L1' in result[0]['url']
    assert obsidian.search_snapshot(data, 'quuxzz') == []
    context = obsidian.knowledge_context('retention', path, 800)
    assert len(context) <= 800 and 'untrusted data' in context and 'Source:' in context
    with pytest.raises(obsidian.VaultError, match='budget'):
        obsidian.knowledge_context('retention', path, 100)


@pytest.mark.parametrize('change', ['repository', 'revision', 'path', 'duplicates'])
def test_invalid_snapshot_rejected(tmp_path, change):
    data = snapshot()
    if change == 'repository': data['repository'] = 'other/repo'
    if change == 'revision': data['revision'] = 'main'
    if change == 'path': data['notes'][0]['path'] = '../outside.md'
    if change == 'duplicates': data['notes'].append(copy.deepcopy(data['notes'][0]))
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(data))
    with pytest.raises(obsidian.VaultError): obsidian.load_snapshot(path)


def test_missing_snapshot_fails_closed(tmp_path):
    with pytest.raises(obsidian.VaultError): obsidian.load_snapshot(tmp_path / 'absent')


def test_enabled_registry_injects_bounded_evidence_without_tools(tmp_path, monkeypatch):
    path = write_snapshot(tmp_path)
    monkeypatch.setattr(settings, 'obsidian_enabled', True)
    monkeypatch.setattr(settings, 'obsidian_snapshot_path', path)
    registry = CrewRegistry(Path(__file__).resolve().parents[1] / 'agents')
    registry.load_all()
    observed = {}
    class Agent:
        def __init__(self, **kwargs): observed['agent'] = kwargs
    class Task:
        def __init__(self, **kwargs): observed['task'] = kwargs
    class Crew:
        def __init__(self, **kwargs): pass
        def kickoff(self): return 'Synthetic answer'
    monkeypatch.setitem(sys.modules, 'crewai', SimpleNamespace(
        Agent=Agent, Task=Task, Crew=Crew, Process=SimpleNamespace(sequential='sequential')))
    monkeypatch.setattr(registry, '_build_llm', lambda *a, **k: (
        SimpleNamespace(prompt_tokens=10, completion_tokens=5, reported_cost=0), 'openai/gpt-4.1-mini'))
    registry.set_governance(SimpleNamespace(finish_friday_run=Mock()))
    result = asyncio.run(registry._invoke_once(FRIDAY_ID, 'Owner question', {'knowledge_query': 'retention'}, run_id=1))
    assert result == 'Synthetic answer'
    assert 'Source:' in observed['task']['description']
    assert 'retention' in observed['task']['description']
    assert len(observed['task']['description']) <= settings.friday_max_input_chars
    assert observed['agent']['tools'] == [] and not observed['agent']['allow_delegation']
    path.unlink()
    with pytest.raises(obsidian.VaultError):
        asyncio.run(registry._invoke_once(FRIDAY_ID, 'retention', {}, run_id=2))


def test_oversized_note_and_total_fail_without_replacing(tmp_path, monkeypatch):
    path = write_snapshot(tmp_path)
    before = path.read_bytes()
    github(monkeypatch)
    monkeypatch.setattr(obsidian, 'MAX_FILE', 8)
    with pytest.raises(obsidian.VaultError, match='limits'):
        obsidian.refresh_snapshot(path, 'token')
    assert path.read_bytes() == before
    monkeypatch.setattr(obsidian, 'MAX_FILE', 256 * 1024)
    monkeypatch.setattr(obsidian, 'MAX_TOTAL', 8)
    with pytest.raises(obsidian.VaultError, match='limits'):
        obsidian.refresh_snapshot(path, 'token')
    assert path.read_bytes() == before


def test_complete_refresh_removes_deleted_notes(tmp_path, monkeypatch):
    path = write_snapshot(tmp_path)
    assert len(obsidian.load_snapshot(path)['notes']) == 2
    github(monkeypatch)
    obsidian.refresh_snapshot(path, 'token')
    assert len(obsidian.load_snapshot(path)['notes']) == 1
    assert 'Hiring.md' not in path.read_text()


def test_transport_and_redirect_errors_are_sanitized(monkeypatch):
    def handler(request):
        raise httpx.ConnectError('synthetic-token private details', request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(obsidian.VaultError) as error:
            obsidian._get(client, '/commits/main', 100)
        assert 'synthetic-token' not in str(error.value)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            302, headers={'location': 'https://other.example/'}))) as client:
        with pytest.raises(obsidian.VaultError, match='HTTP 302'):
            obsidian._get(client, '/commits/main', 100)


def test_response_size_and_shape_are_bounded():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 30))) as client:
        with pytest.raises(obsidian.VaultError, match='size limit'):
            obsidian._get(client, '/commits/main', 10)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as client:
        with pytest.raises(obsidian.VaultError, match='Invalid'):
            obsidian._get(client, '/commits/main', 100)


def test_offer_question_ignores_generic_brain_metadata():
    data = snapshot()
    data['notes'] = [
        {'path': 'AI Instructions/DATA-DICTIONARY.md', 'content': 'business brain source note links business brain'},
        {'path': 'Products/FRIDAY-AI-OFFERS.md', 'content': '# Friday AI Offers\nCounty Lead Scraper. Friday AI Software.'}]
    results = obsidian.search_snapshot(data, 'According to my Business Brain, what are my main business offers? Cite one source note link.')
    assert len(results) == 1
    assert results[0]['path'] == 'Products/FRIDAY-AI-OFFERS.md'
