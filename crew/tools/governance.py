"""
Xcerebro 2.0 — Governance Store

Postgres-backed state for the three checks-and-balances systems:
  - approvals:    pending/resolved human approval requests (survives restarts)
  - agent_trust:  clean-approval counters per (agent, action_type) for graduated autonomy
  - llm_calls:    per-invocation cost log, powers the daily spend kill switch

Follows the same self-contained SQLAlchemy pattern as tools/audit.py:
degrades to disabled (log-only) if the DB is unreachable so startup never crashes.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, date
from typing import Any, Optional

from loguru import logger
from sqlalchemy import (
    create_engine, func, Column, String, DateTime, JSON, Text, Integer,
    Float, Boolean, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from tools.db import create_all_with_retry

Base = declarative_base()


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id = Column(String(64), primary_key=True)
    agent_id = Column(String(64), nullable=True, index=True)
    invocation_id = Column(String(64), nullable=True)
    action_type = Column(String(32), nullable=True, index=True)
    title = Column(String(256), nullable=True)
    task = Column(Text, nullable=True)
    context = Column(JSON, nullable=True)
    status = Column(String(16), default="pending", nullable=False, index=True)
    approver = Column(String(128), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class AgentTrustRow(Base):
    __tablename__ = "agent_trust"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(String(64), nullable=False, index=True)
    action_type = Column(String(32), nullable=False)
    clean_approvals = Column(Integer, default=0, nullable=False)
    auto_approve = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LlmCallRow(Base):
    __tablename__ = "llm_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    agent_id = Column(String(64), nullable=True, index=True)
    model_slug = Column(String(128), nullable=True)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    success = Column(Boolean, default=True)


class GovernanceStore:
    """All reads/writes for approvals, trust, and spend."""

    def __init__(self, database_url: str):
        try:
            self.engine = create_engine(database_url, pool_pre_ping=True)
            create_all_with_retry(self.engine, Base)
            self.SessionLocal = sessionmaker(bind=self.engine)
            self.enabled = True
            logger.info("Governance store initialized")
        except Exception as e:
            logger.warning(f"Governance DB unavailable (degraded, nothing gated persists): {e}")
            self.enabled = False

    @contextmanager
    def friday_run(self, agent_id: str, model: str, reservation: float,
                   daily_cap: float, hourly_limit: int):
        """One active run across workers; reserve spend durably before any LLM call.

        Failure/crash retains the conservative reservation. It is never silently
        refunded: a provider may have billed a call that did not return usage.
        """
        if not self.enabled:
            raise RuntimeError("GOVERNANCE_UNAVAILABLE: model work refused")
        with self.engine.connect() as lock:
            acquired = lock.execute(text("SELECT pg_try_advisory_lock(46729401)")).scalar()
            if not acquired:
                raise RuntimeError("BUSY: Friday already has an active run")
            try:
                if self.today_spend() + reservation > daily_cap + 1e-12:
                    raise RuntimeError("SPEND_CAP: insufficient remaining reserved budget")
                if self.calls_last_hour() >= hourly_limit:
                    raise RuntimeError("RATE_LIMIT: hourly invocation limit reached")
                with self.SessionLocal() as session:
                    row = LlmCallRow(agent_id=agent_id, model_slug=model,
                                     cost_usd=reservation, success=False)
                    session.add(row)
                    session.commit()
                    run_id = row.id
                yield run_id
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(46729401)"))

    def finish_friday_run(self, run_id: int, input_tokens: int,
                          output_tokens: int, cost: float) -> None:
        if not self.enabled:
            raise RuntimeError("GOVERNANCE_UNAVAILABLE: usage could not be recorded")
        with self.SessionLocal() as session:
            row = session.get(LlmCallRow, run_id)
            if row is None or row.success:
                raise RuntimeError("Invalid Friday usage reservation")
            row.input_tokens = input_tokens
            row.output_tokens = output_tokens
            row.cost_usd = cost
            row.success = True
            session.commit()

    # ── approvals ─────────────────────────────────────────────────────────────

    def create_approval(
        self,
        agent_id: str,
        task: str,
        context: dict,
        action_type: Optional[str],
        invocation_id: str,
        title: str,
    ) -> str:
        approval_id = str(uuid.uuid4())
        if not self.enabled:
            logger.warning(f"Governance DB down — approval {approval_id} NOT persisted")
            return approval_id
        session = self.SessionLocal()
        try:
            session.add(ApprovalRow(
                id=approval_id,
                agent_id=agent_id,
                invocation_id=invocation_id,
                action_type=action_type,
                title=title,
                task=task,
                context=context,
            ))
            session.commit()
        finally:
            session.close()
        return approval_id

    def get_approval(self, approval_id: str) -> Optional[dict]:
        if not self.enabled:
            return None
        session = self.SessionLocal()
        try:
            row = session.get(ApprovalRow, approval_id)
            return self._approval_dict(row) if row else None
        finally:
            session.close()

    def resolve_approval(
        self, approval_id: str, decision: str, approver: str, note: Optional[str]
    ) -> Optional[dict]:
        """Mark pending approval resolved. Returns the record, or None if missing/already resolved."""
        if not self.enabled:
            return None
        session = self.SessionLocal()
        try:
            row = session.get(ApprovalRow, approval_id)
            if not row or row.status != "pending":
                return None
            row.status = decision
            row.approver = approver
            row.note = note
            row.resolved_at = datetime.utcnow()
            session.commit()
            return self._approval_dict(row)
        finally:
            session.close()

    def pending_approvals(self) -> list[dict]:
        if not self.enabled:
            return []
        session = self.SessionLocal()
        try:
            rows = (
                session.query(ApprovalRow)
                .filter(ApprovalRow.status == "pending")
                .order_by(ApprovalRow.created_at.desc())
                .all()
            )
            return [self._approval_dict(r) for r in rows]
        finally:
            session.close()

    @staticmethod
    def _approval_dict(row: ApprovalRow) -> dict:
        return {
            "id": row.id,
            "agent_id": row.agent_id,
            "invocation_id": row.invocation_id,
            "action_type": row.action_type,
            "title": row.title,
            "task": row.task,
            "context": row.context or {},
            "status": row.status,
            "approver": row.approver,
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }

    # ── graduated trust ───────────────────────────────────────────────────────

    def record_decision(self, agent_id: str, action_type: Optional[str], approved: bool) -> None:
        """Approve increments the clean counter; reject resets it to zero."""
        if not self.enabled or not action_type:
            return
        session = self.SessionLocal()
        try:
            row = (
                session.query(AgentTrustRow)
                .filter_by(agent_id=agent_id, action_type=action_type)
                .first()
            )
            if not row:
                # explicit values: Column defaults only apply at flush, not on the Python object
                row = AgentTrustRow(
                    agent_id=agent_id, action_type=action_type,
                    clean_approvals=0, auto_approve=False,
                )
                session.add(row)
            if approved:
                row.clean_approvals += 1
            else:
                row.clean_approvals = 0
                row.auto_approve = False  # any rejection revokes earned autonomy
            session.commit()
        finally:
            session.close()

    def is_auto_approved(self, agent_id: str, action_type: str) -> bool:
        if not self.enabled:
            return False
        session = self.SessionLocal()
        try:
            row = (
                session.query(AgentTrustRow)
                .filter_by(agent_id=agent_id, action_type=action_type, auto_approve=True)
                .first()
            )
            return row is not None
        finally:
            session.close()

    def set_auto_approve(self, agent_id: str, action_type: str, value: bool) -> bool:
        """Human-initiated flip (via API). Returns False if no trust row exists yet."""
        if not self.enabled:
            return False
        session = self.SessionLocal()
        try:
            row = (
                session.query(AgentTrustRow)
                .filter_by(agent_id=agent_id, action_type=action_type)
                .first()
            )
            if not row:
                return False
            row.auto_approve = value
            session.commit()
            return True
        finally:
            session.close()

    def trust_summary(self, threshold: int) -> dict:
        """All trust rows + which ones are candidates for an auto-approve flip."""
        if not self.enabled:
            return {"rows": [], "flip_candidates": []}
        session = self.SessionLocal()
        try:
            rows = session.query(AgentTrustRow).order_by(AgentTrustRow.agent_id).all()
            out = [
                {
                    "agent_id": r.agent_id,
                    "action_type": r.action_type,
                    "clean_approvals": r.clean_approvals,
                    "auto_approve": r.auto_approve,
                }
                for r in rows
            ]
            candidates = [
                r for r in out
                if not r["auto_approve"] and r["clean_approvals"] >= threshold
            ]
            return {"rows": out, "flip_candidates": candidates}
        finally:
            session.close()

    # ── spend tracking ────────────────────────────────────────────────────────

    def log_llm_call(
        self,
        agent_id: str,
        model_slug: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        success: bool = True,
    ) -> None:
        if not self.enabled:
            return
        session = self.SessionLocal()
        try:
            session.add(LlmCallRow(
                agent_id=agent_id,
                model_slug=model_slug,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                success=success,
            ))
            session.commit()
        except Exception as e:
            logger.error(f"Failed to log LLM call: {e}")
        finally:
            session.close()

    def today_spend(self) -> float:
        if not self.enabled:
            return 0.0
        session = self.SessionLocal()
        try:
            start = datetime.combine(date.today(), datetime.min.time())
            total = (
                session.query(func.coalesce(func.sum(LlmCallRow.cost_usd), 0.0))
                .filter(LlmCallRow.timestamp >= start)
                .scalar()
            )
            return float(total or 0.0)
        finally:
            session.close()

    def calls_last_hour(self) -> int:
        if not self.enabled:
            return 0
        session = self.SessionLocal()
        try:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(hours=1)
            return (
                session.query(func.count(LlmCallRow.id))
                .filter(LlmCallRow.timestamp >= cutoff)
                .scalar()
            ) or 0
        finally:
            session.close()

    def spend_summary(self, days: int = 7) -> dict:
        if not self.enabled:
            return {"total_cost_usd": 0.0, "by_agent": {}, "by_model": {}}
        session = self.SessionLocal()
        try:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=days)
            q = session.query(LlmCallRow).filter(LlmCallRow.timestamp >= cutoff)
            by_agent: dict[str, float] = {}
            by_model: dict[str, float] = {}
            total = 0.0
            calls = 0
            for row in q.all():
                total += row.cost_usd or 0.0
                calls += 1
                by_agent[row.agent_id or "?"] = round(
                    by_agent.get(row.agent_id or "?", 0.0) + (row.cost_usd or 0.0), 4)
                by_model[row.model_slug or "?"] = round(
                    by_model.get(row.model_slug or "?", 0.0) + (row.cost_usd or 0.0), 4)
            return {
                "period_days": days,
                "total_calls": calls,
                "total_cost_usd": round(total, 4),
                "today_cost_usd": round(self.today_spend(), 4),
                "by_agent": by_agent,
                "by_model": by_model,
            }
        finally:
            session.close()
