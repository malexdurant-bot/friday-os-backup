"""Restricted foundation policy. No credentials or external integrations."""

FRIDAY_ID = "friday-operations"


def validate_friday(data: dict) -> None:
    """Reject capability drift before loading or invoking Friday."""
    allowed = {"id", "name", "role", "goal", "backstory", "expected_output",
               "permissions", "schedule", "data_sources", "_tier", "_path"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Friday definition contains unsupported fields")
    if data.get("id") != FRIDAY_ID:
        raise ValueError("Only friday-operations is allowed")
    for field in ("name", "role", "goal", "backstory", "expected_output"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"Friday requires {field}")
    if data.get("permissions") != {
        "always_require_approval": False,
        "can_delegate_to": [],
        "can_call_tools": [],
    }:
        raise ValueError("Friday must have no tools, delegation or approval actions")
    if data.get("schedule") != [] or data.get("data_sources") != []:
        raise ValueError("Friday must have no schedules or data sources")
