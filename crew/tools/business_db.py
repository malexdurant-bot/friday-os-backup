"""
Xcerebro 2.0 — Business Data Tools (RehabBooks + DealEngine + audit log)

Read-only SQL access to the business schemas living in the crew's own
Railway Postgres (created by scripts/init-business-db.sql).

One query tool serves every rehabbooks.*/dealengine.* name the agent YAMLs
declare — the loader matches by prefix, so no YAML edits are needed. The
tool enforces read-only two ways: the session is set read-only at the
Postgres level, and the statement must be a single SELECT/WITH.
"""

import json
import re
from typing import Type

from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from config import settings

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
    return _engine


TABLE_CATALOG = """
Available tables (query with schema-qualified names):

rehabbooks.projects      — id, name, address, status(active|listed|sold|archived), budget, start_date, deadline
rehabbooks.transactions  — id, date, amount(neg=expense/pos=income), category, entity(rehabco|notary|personal), project_id, description, needs_review
rehabbooks.accounts      — id, name, type(checking|savings|credit), balance, entity
rehabbooks.budget_items  — id, project_id, category, budgeted, actual
rehabbooks.investor_loans— id, lender, principal, rate, payment_due, project_id
dealengine.properties    — id, address, status(analyzing|under_contract|owned|sold|passed), arv, purchase_price
dealengine.deal_analyses — id, property_id, strategy(flip|wholesale|buy_hold|subto|creative), offer_price, est_profit, notes
dealengine.project_draws — id, project_id, amount, status(pending|approved|funded), requested_at
eos.vto                  — section(mission|vision|core_values|ten_year_target|marketing_strategy|three_year_picture|one_year_plan|issues), content, updated_at
""".strip()

# single statement, must start with SELECT or WITH, no statement separators
_READONLY_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


class QueryInput(BaseModel):
    sql: str = Field(description="A single read-only SELECT (or WITH...SELECT) statement. Schema-qualify tables, e.g. SELECT * FROM rehabbooks.transactions WHERE needs_review LIMIT 50")


class BusinessDataQueryTool(BaseTool):
    name: str = "business_data_query"
    description: str = (
        "Run a read-only SQL SELECT against the business database "
        "(RehabBooks accounting/personal finance + DealEngine deal analysis). "
        "Returns rows as JSON. Write operations are rejected.\n\n" + TABLE_CATALOG
    )
    args_schema: Type[BaseModel] = QueryInput

    def _run(self, sql: str) -> str:
        if not _READONLY_RE.match(sql) or ";" in sql:
            return json.dumps({"error": "Only a single SELECT/WITH statement is allowed (no semicolons)."})
        try:
            with _get_engine().connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                result = conn.execute(text(sql))
                rows = [dict(r._mapping) for r in result.fetchmany(200)]
            return json.dumps({"row_count": len(rows), "rows": rows}, default=str)
        except Exception as e:
            logger.error(f"business_data_query failed: {e}")
            return json.dumps({"error": str(e)[:500]})


class AuditLogInput(BaseModel):
    limit: int = Field(default=50, description="How many recent events to return (max 200)")
    agent_id: str = Field(default="", description="Optional: filter to one agent's events")


class AuditLogTool(BaseTool):
    name: str = "audit_log_read"
    description: str = (
        "Read recent agent activity from the audit log: invocations, approvals, "
        "errors — who did what, when, with result previews."
    )
    args_schema: Type[BaseModel] = AuditLogInput

    def _run(self, limit: int = 50, agent_id: str = "") -> str:
        limit = min(max(limit, 1), 200)
        try:
            with _get_engine().connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                if agent_id:
                    result = conn.execute(
                        text("SELECT timestamp, event_type, agent_id, invocation_id, payload "
                             "FROM audit_events WHERE agent_id = :aid ORDER BY timestamp DESC LIMIT :lim"),
                        {"aid": agent_id, "lim": limit},
                    )
                else:
                    result = conn.execute(
                        text("SELECT timestamp, event_type, agent_id, invocation_id, payload "
                             "FROM audit_events ORDER BY timestamp DESC LIMIT :lim"),
                        {"lim": limit},
                    )
                rows = [dict(r._mapping) for r in result.fetchall()]
            return json.dumps({"row_count": len(rows), "events": rows}, default=str)
        except Exception as e:
            logger.error(f"audit_log_read failed: {e}")
            return json.dumps({"error": str(e)[:500]})


def get_business_db_tools(tool_names: list[str]) -> list:
    """
    Loader for rehabbooks.*, dealengine.*, and postgres.* declared names.
    All data reads map to the one query tool; audit names map to the audit tool.
    """
    tools = []
    if any(n.startswith(("rehabbooks.", "dealengine.")) for n in tool_names):
        tools.append(BusinessDataQueryTool())
    if any(n.startswith("postgres.audit_log") for n in tool_names):
        tools.append(AuditLogTool())
    return tools
