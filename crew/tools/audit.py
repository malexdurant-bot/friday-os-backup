"""
Xcerebro 2.0 — Audit Logger

Tracks every agent action, approval, and error to Postgres.
Provides forensic audit trail for buyers to review what their agents did.
"""

from datetime import datetime
from typing import Any, Optional
from loguru import logger

from sqlalchemy import create_engine, Column, String, DateTime, JSON, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker

from tools.db import create_all_with_retry

Base = declarative_base()


class AuditEvent(Base):
    """One row per agent action, decision, or error."""
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    agent_id = Column(String(64), nullable=True, index=True)
    invocation_id = Column(String(64), nullable=True, index=True)
    payload = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)


class AuditLogger:
    """Writes audit events to Postgres."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        try:
            self.engine = create_engine(database_url, pool_pre_ping=True)
            create_all_with_retry(self.engine, Base)
            self.SessionLocal = sessionmaker(bind=self.engine)
            self.enabled = True
            logger.info("Audit logger initialized")
        except Exception as e:
            logger.warning(f"Audit DB unavailable, falling back to file logging: {e}")
            self.enabled = False

    def log_event(
        self,
        event_type: str,
        agent_id: Optional[str] = None,
        invocation_id: Optional[str] = None,
        payload: Optional[dict] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Write an audit event."""
        if not self.enabled:
            logger.info(f"AUDIT: {event_type} agent={agent_id} inv={invocation_id} payload={payload}")
            return

        try:
            session = self.SessionLocal()
            event = AuditEvent(
                event_type=event_type,
                agent_id=agent_id,
                invocation_id=invocation_id,
                payload=payload,
                notes=notes,
            )
            session.add(event)
            session.commit()
            session.close()
        except Exception as e:
            logger.error(f"Failed to write audit event: {e}")

    def recent(self, limit: int = 100, agent_id: Optional[str] = None) -> list[dict]:
        """Return recent audit events, optionally filtered by agent."""
        if not self.enabled:
            return []

        try:
            session = self.SessionLocal()
            query = session.query(AuditEvent).order_by(AuditEvent.timestamp.desc())
            if agent_id:
                query = query.filter(AuditEvent.agent_id == agent_id)
            events = query.limit(limit).all()
            result = [
                {
                    "id": e.id,
                    "timestamp": e.timestamp.isoformat(),
                    "event_type": e.event_type,
                    "agent_id": e.agent_id,
                    "invocation_id": e.invocation_id,
                    "payload": e.payload,
                    "notes": e.notes,
                }
                for e in events
            ]
            session.close()
            return result
        except Exception as e:
            logger.error(f"Failed to fetch audit events: {e}")
            return []
