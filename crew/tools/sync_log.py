"""
Xcerebro 2.0 - GHL Sync Log Query Tool

Gives agents read access to the ghl_sync_runs and ghl_sync_contacts
tables written by the OneClick Scrape & Skiptrace GitHub Actions pipeline.
Answers questions like: how many leads synced this week? which failed?
what XC match rate are we getting?
"""

import os
from typing import Type
from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL not set")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


class SyncLogInput(BaseModel):
    query_type: str = Field(
        ...,
        description=(
            "What to query. Options:\n"
            "  recent_runs       - last N sync runs with created/updated/failed counts\n"
            "  county_summary    - totals per county (all time or last N days)\n"
            "  failed_contacts   - contacts that failed to sync (county optional)\n"
            "  xc_match_rate     - % of contacts matched against local intelligence data\n"
            "  contacts_synced   - list contacts synced (filter by county and/or days)"
        ),
    )
    county: str = Field(default="", description="Filter by county (e.g. charleston_sc). Empty = all.")
    days: int = Field(default=7, description="Lookback window in days.")
    limit: int = Field(default=20, description="Max rows to return.")


class SyncLogQueryTool(BaseTool):
    name: str = "sync_log_query"
    description: str = (
        "Query the GHL sync history database. Find out how many DealMachine leads "
        "were synced to GoHighLevel, which contacts were created vs updated vs failed, "
        "what the cross-reference (XC) match rate is, and which counties are active. "
        "Data is written after every daily sync run."
    )
    args_schema: Type[BaseModel] = SyncLogInput

    def _run(self, query_type: str, county: str = "", days: int = 7, limit: int = 20) -> str:
        try:
            engine = _get_engine()
        except RuntimeError as e:
            return f"DB unavailable: {e}"

        county_filter = "AND county = :county" if county else ""
        county_filter_r = "AND r.county = :county" if county else ""
        params: dict = {"days": days, "limit": limit}
        if county:
            params["county"] = county

        try:
            with engine.connect() as conn:
                if query_type == "recent_runs":
                    sql = text(
                        f"SELECT run_at::date AS date, county, trigger, dry_run, "
                        f"total_csv_rows, new_leads, gained_contact, "
                        f"created, updated, failed, xc_matched, "
                        f"ROUND(duration_seconds::numeric, 1) AS secs "
                        f"FROM ghl_sync_runs "
                        f"WHERE run_at >= NOW() - INTERVAL '{days} days' "
                        f"{county_filter} "
                        f"ORDER BY run_at DESC "
                        f"LIMIT :limit"
                    )
                    rows = conn.execute(sql, params).mappings().all()
                    if not rows:
                        return f"No sync runs found in the last {days} days."
                    lines = [f"Last {len(rows)} sync runs (past {days}d):"]
                    for r in rows:
                        dr = " [DRY RUN]" if r["dry_run"] else ""
                        lines.append(
                            f"  {r['date']} | {r['county']} | {r['trigger']}{dr} | "
                            f"csv:{r['total_csv_rows']} new:{r['new_leads']} "
                            f"created:{r['created']} updated:{r['updated']} "
                            f"failed:{r['failed']} xc:{r['xc_matched']} ({r['secs']}s)"
                        )
                    return "\n".join(lines)

                elif query_type == "county_summary":
                    sql = text(
                        f"SELECT county, COUNT(*) AS runs, "
                        f"SUM(created) AS total_created, SUM(updated) AS total_updated, "
                        f"SUM(failed) AS total_failed, SUM(total_csv_rows) AS total_rows "
                        f"FROM ghl_sync_runs "
                        f"WHERE run_at >= NOW() - INTERVAL '{days} days' "
                        f"AND dry_run = FALSE {county_filter} "
                        f"GROUP BY county ORDER BY total_created DESC"
                    )
                    rows = conn.execute(sql, params).mappings().all()
                    if not rows:
                        return f"No data in last {days} days."
                    lines = [f"County summary (past {days}d, live runs only):"]
                    for r in rows:
                        lines.append(
                            f"  {r['county']}: {r['runs']} runs | "
                            f"created:{r['total_created']} updated:{r['total_updated']} "
                            f"failed:{r['total_failed']} | {r['total_rows']} CSV rows total"
                        )
                    return "\n".join(lines)

                elif query_type == "failed_contacts":
                    sql = text(
                        f"SELECT c.dm_lead_id, c.address, c.first_name, c.last_name, "
                        f"c.error, c.synced_at::date AS date "
                        f"FROM ghl_sync_contacts c "
                        f"JOIN ghl_sync_runs r ON r.run_id = c.run_id "
                        f"WHERE c.action = 'FAIL' "
                        f"AND c.synced_at >= NOW() - INTERVAL '{days} days' "
                        f"{county_filter_r} "
                        f"ORDER BY c.synced_at DESC "
                        f"LIMIT :limit"
                    )
                    rows = conn.execute(sql, params).mappings().all()
                    if not rows:
                        return f"No failed contacts in the last {days} days."
                    lines = [f"{len(rows)} failed contact(s) (past {days}d):"]
                    for r in rows:
                        lines.append(
                            f"  DM#{r['dm_lead_id']} | {r['first_name']} {r['last_name']} "
                            f"| {r['address']} | {r['date']} | error: {r['error']}"
                        )
                    return "\n".join(lines)

                elif query_type == "xc_match_rate":
                    sql = text(
                        f"SELECT r.county, COUNT(*) AS total, "
                        f"SUM(CASE WHEN c.xc_matched THEN 1 ELSE 0 END) AS matched, "
                        f"ROUND(100.0 * SUM(CASE WHEN c.xc_matched THEN 1 ELSE 0 END) "
                        f"/ NULLIF(COUNT(*), 0), 1) AS match_pct "
                        f"FROM ghl_sync_contacts c "
                        f"JOIN ghl_sync_runs r ON r.run_id = c.run_id "
                        f"WHERE c.synced_at >= NOW() - INTERVAL '{days} days' "
                        f"AND r.dry_run = FALSE {county_filter_r} "
                        f"GROUP BY r.county"
                    )
                    rows = conn.execute(sql, params).mappings().all()
                    if not rows:
                        return f"No data in last {days} days."
                    lines = [f"XC (local intelligence) match rate (past {days}d):"]
                    for r in rows:
                        lines.append(
                            f"  {r['county']}: {r['matched']}/{r['total']} matched ({r['match_pct']}%)"
                        )
                    return "\n".join(lines)

                elif query_type == "contacts_synced":
                    sql = text(
                        f"SELECT c.dm_lead_id, c.first_name, c.last_name, c.address, "
                        f"c.phone, c.action, c.tags, c.xc_matched, c.synced_at::date AS date "
                        f"FROM ghl_sync_contacts c "
                        f"JOIN ghl_sync_runs r ON r.run_id = c.run_id "
                        f"WHERE c.synced_at >= NOW() - INTERVAL '{days} days' "
                        f"AND r.dry_run = FALSE AND c.action != 'FAIL' "
                        f"{county_filter_r} "
                        f"ORDER BY c.synced_at DESC "
                        f"LIMIT :limit"
                    )
                    rows = conn.execute(sql, params).mappings().all()
                    if not rows:
                        return f"No contacts synced in last {days} days."
                    lines = [f"{len(rows)} contact(s) synced (past {days}d):"]
                    for r in rows:
                        xc = " XC" if r["xc_matched"] else ""
                        tags = ", ".join(r["tags"] or [])[:60]
                        lines.append(
                            f"  [{r['action']}]{xc} {r['first_name']} {r['last_name']} | "
                            f"{r['address']} | {r['date']} | tags: {tags}"
                        )
                    return "\n".join(lines)

                else:
                    return (
                        f"Unknown query_type: '{query_type}'. "
                        "Valid: recent_runs, county_summary, failed_contacts, "
                        "xc_match_rate, contacts_synced"
                    )
        except Exception as exc:
            logger.error(f"SyncLogQueryTool error: {exc}")
            return f"Query failed: {exc}"


SYNC_LOG_TOOL_REGISTRY: dict[str, type] = {
    "sync_log.query": SyncLogQueryTool,
}


def get_sync_log_tools(tool_names: list[str]) -> list:
    return [SyncLogQueryTool() for n in tool_names if n in SYNC_LOG_TOOL_REGISTRY]
