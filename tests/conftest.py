"""Offline tests: never read project .env, call services, or run CrewAI."""
import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'crew'))
# Clear only task-specific settings during tests; never print inherited secrets.
for key in list(os.environ):
    if key.upper().startswith(('SLACK_', 'OBSIDIAN_', 'GHL_', 'KPI_')) or key.upper() in {'RUNTIME_PROFILE', 'AGENTS_PATH', 'FRIDAY_MODEL', 'OPENROUTER_API_KEY', 'AGENT_RUNTIME_API_KEY', 'FRIDAY_KPIS_ENABLED'}:
        os.environ.pop(key)

@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('External network forbidden during offline tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
