"""Load integrations only when explicitly requested, never on package import."""
from importlib import import_module

_EXPORTS = {
    "AuditLogger": "tools.audit",
    "ApprovalManager": "tools.approval",
    "MemoryManager": "tools.memory",
    "get_ghl_tools": "tools.ghl",
    "GHL_TOOL_REGISTRY": "tools.ghl",
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(_EXPORTS[name]), name)

__all__ = ["AuditLogger", "ApprovalManager", "MemoryManager", "get_ghl_tools", "GHL_TOOL_REGISTRY"]
