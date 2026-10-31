"""
Xcerebro 2.0 — Agent Memory Manager

Persistent semantic memory backed by Postgres + pgvector.
Embeddings via OpenAI text-embedding-3-small (1536 dims). OpenRouter has NO
embeddings endpoint — routing embeddings through it was the original design's
silent failure (memory degraded to recency-only from day one). Chat completions
still go through OpenRouter; only embeddings hit OpenAI directly.

Each agent invocation:
  1. retrieve() — pulls semantically relevant past memories
  2. (agent runs with that context injected)
  3. store()    — writes the result as a new memory

Memory is shared across agents — the CEO can read what the CFO wrote.
"""

import os
import json
from typing import Optional

from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from tools.embeddings import embed as shared_embed, to_vec_param


# ── schema ────────────────────────────────────────────────────────────────────

MIGRATION_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agent_memories (
    id          SERIAL PRIMARY KEY,
    agent_id    VARCHAR(64)  NOT NULL,
    memory_type VARCHAR(32)  NOT NULL DEFAULT 'episodic',
    content     TEXT         NOT NULL,
    embedding   vector(1536),
    metadata    JSONB        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memories_agent_date
    ON agent_memories (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON agent_memories USING hnsw (embedding vector_cosine_ops);
"""

class MemoryManager:
    """Persistent agent memory with semantic retrieval via pgvector."""

    def __init__(self, database_url: str, openrouter_api_key: str = ""):
        # openrouter_api_key param kept for call-site compatibility; embeddings
        # need a real OpenAI key (OpenRouter has no /embeddings endpoint).
        self.engine = create_engine(database_url, pool_pre_ping=True)
        self._api_key = os.getenv("OPENAI_API_KEY", "")  # kept for tests/introspection
        if not self._api_key:
            logger.error(
                "MemoryManager: OPENAI_API_KEY missing — semantic memory DISABLED, "
                "falling back to recency-only retrieval. Set it in Railway variables."
            )
        self._migrate()

    # ── setup ─────────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Create tables and indexes if they don't exist."""
        try:
            with self.engine.connect() as conn:
                for stmt in MIGRATION_SQL.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        conn.execute(text(stmt))
                conn.commit()
            logger.info("MemoryManager: schema ready")
        except SQLAlchemyError as e:
            logger.warning(f"MemoryManager: migration issue (degraded mode): {e}")

    # ── embeddings (shared helper — see tools/embeddings.py) ─────────────────

    def _embed(self, text_input: str) -> Optional[list[float]]:
        return shared_embed(text_input)

    @staticmethod
    def _vec_param(embedding: Optional[list[float]]) -> Optional[str]:
        return to_vec_param(embedding)

    # ── write ─────────────────────────────────────────────────────────────────

    def store(
        self,
        agent_id: str,
        content: str,
        memory_type: str = "episodic",
        metadata: Optional[dict] = None,
    ) -> None:
        """Persist a memory. Call after each agent invocation."""
        embedding = self._embed(content)
        # All values bound as params; CAST() instead of :: avoids the colon
        # bind-parse failure that broke every store() before this fix.
        sql = text("""
            INSERT INTO agent_memories (agent_id, memory_type, content, embedding, metadata)
            VALUES (:agent_id, :memory_type, :content,
                    CAST(:embedding AS vector), CAST(:metadata AS jsonb))
        """)
        try:
            with self.engine.connect() as conn:
                conn.execute(sql, {
                    "agent_id": agent_id,
                    "memory_type": memory_type,
                    "content": content,
                    "embedding": self._vec_param(embedding),
                    "metadata": json.dumps(metadata or {}),
                })
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"MemoryManager.store failed: {e}")

    # ── read ──────────────────────────────────────────────────────────────────

    def retrieve(
        self,
        agent_id: str,
        query: str,
        top_k: int = 5,
        any_agent: bool = False,
    ) -> list[dict]:
        """
        Return the most relevant memories for this agent.

        Embeddings available: semantic (cosine) search via pgvector.
        Embedding failure (error path only): recency fallback.

        Set any_agent=True to search across ALL agents (useful for CEO
        querying what CFO or CMO agents wrote).
        """
        embedding = self._embed(query)
        agent_filter = "" if any_agent else "AND agent_id = :agent_id"

        if embedding:
            sql = text(f"""
                SELECT agent_id, memory_type, content, metadata, created_at,
                       1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM agent_memories
                WHERE embedding IS NOT NULL {agent_filter}
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :top_k
            """)
            params = {"agent_id": agent_id, "top_k": top_k, "qvec": self._vec_param(embedding)}
        else:
            sql = text(f"""
                SELECT agent_id, memory_type, content, metadata, created_at,
                       1.0 AS similarity
                FROM agent_memories
                WHERE 1=1 {agent_filter}
                ORDER BY created_at DESC
                LIMIT :top_k
            """)
            params = {"agent_id": agent_id, "top_k": top_k}

        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "agent_id": r.agent_id,
                    "memory_type": r.memory_type,
                    "content": r.content,
                    "metadata": r.metadata,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "similarity": float(r.similarity),
                }
                for r in rows
            ]
        except SQLAlchemyError as e:
            logger.error(f"MemoryManager.retrieve failed: {e}")
            return []

    def format_for_context(self, memories: list[dict]) -> str:
        """
        Format retrieved memories as a readable context block to inject
        into the agent's task description.
        """
        if not memories:
            return ""
        lines = ["--- Relevant past context ---"]
        for m in memories:
            ts = m.get("created_at", "")[:10] if m.get("created_at") else "?"
            lines.append(f"[{ts} | {m['agent_id']}] {m['content'][:400]}")
        lines.append("--- End past context ---")
        return "\n".join(lines)
