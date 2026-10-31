"""Durable Slack inbox/outbox. Unknown side effects are never replayed blindly."""
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (Column, String, Text, DateTime, UniqueConstraint,
                        create_engine, text)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class SlackEvent(Base):
    __tablename__ = "friday_slack_events"
    event_id = Column(String(128), primary_key=True)
    team_id = Column(String(32), nullable=False)
    app_id = Column(String(32), nullable=False)
    channel_id = Column(String(32), nullable=False)
    owner_id = Column(String(32), nullable=False)
    event_ts = Column(String(40), nullable=False)
    thread_ts = Column(String(40), nullable=False)
    message = Column(Text, nullable=False)
    state = Column(String(32), nullable=False, default="queued")
    reply = Column(Text, nullable=True)
    reply_ts = Column(String(40), nullable=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("team_id", "channel_id", "event_ts"),)


class SlackInbox:
    def __init__(self, database_url):
        # Keep database trouble from holding the acknowledgement past Slack's
        # deadline. HTTP ingress returns 503 when it cannot durably accept work.
        self.engine = create_engine(
            database_url, pool_pre_ping=True, pool_size=4, max_overflow=0,
            pool_timeout=1, connect_args={"connect_timeout": 1,
                                        "options": "-c statement_timeout=1500"},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def enqueue(self, event):
        with self.Session() as session:
            session.add(SlackEvent(**event, state="queued"))
            try:
                session.commit()
                return True
            except IntegrityError as error:
                session.rollback()
                if getattr(error.orig, "pgcode", None) == "23505":
                    return False
                raise

    @contextmanager
    def worker_lock(self):
        with self.engine.connect() as connection:
            acquired = connection.execute(text("SELECT pg_try_advisory_lock(46729402)")).scalar()
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(46729402)"))

    def recover_interrupted(self):
        # Call only under the exclusive worker lock: no other worker can be
        # actively processing these rows. Ready replies/queued inputs are safe.
        with self.Session() as session:
            for row in session.query(SlackEvent).filter(
                SlackEvent.state.in_(["processing", "delivering"])
            ).all():
                row.error_code = "interrupted_" + row.state
                row.state = "uncertain"
                row.updated_at = datetime.utcnow()
            session.commit()

    def next_ready(self):
        with self.Session() as session:
            row = session.query(SlackEvent).filter(
                SlackEvent.state.in_(["queued", "reply_ready"])
            ).order_by(SlackEvent.created_at, SlackEvent.event_id).first()
            return self.as_dict(row) if row else None

    def transition(self, event_id, expected, new, **values):
        if set(values) - {"reply", "reply_ts", "error_code"}:
            raise ValueError("Invalid Slack state update")
        with self.Session() as session:
            count = session.query(SlackEvent).filter_by(
                event_id=event_id, state=expected
            ).update({"state": new, "updated_at": datetime.utcnow(), **values})
            if count != 1:
                raise RuntimeError("Slack event state changed unexpectedly")
            session.commit()

    @staticmethod
    def as_dict(row):
        return {name: getattr(row, name) for name in (
            "event_id", "team_id", "app_id", "channel_id", "owner_id", "event_ts",
            "thread_ts", "message", "state", "reply", "reply_ts", "error_code",
        )}
