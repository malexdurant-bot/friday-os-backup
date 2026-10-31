"""
Xcerebro 2.0 — Knowledge Base (RAG over business documents)

Second memory corpus alongside episodic agent memory: documents (V/TO, SOPs,
contracts, market research) chunked + embedded into pgvector, semantically
searchable by every agent.

Deliberately built on the existing Postgres+pgvector instead of deploying the
Dify/Weaviate stack: same retrieval quality at this scale, zero new services,
one engine for both memory corpora.

Loader serves both `kb.*` and the legacy `dify.knowledge_base.*` names that
50 agent YAMLs already declare — those agents gain real knowledge search with
no YAML changes.
"""

import json
import re
from typing import Optional, Type

from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from config import settings
from tools.embeddings import embed, to_vec_param

MIGRATION_SQL = """
CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE IF NOT EXISTS knowledge.documents (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    source_type   TEXT NOT NULL DEFAULT 'drive',
    drive_file_id TEXT UNIQUE,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge.chunks (
    id          SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES knowledge.documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw
    ON knowledge.chunks USING hnsw (embedding vector_cosine_ops)
"""

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            with _engine.connect() as conn:
                for stmt in MIGRATION_SQL.strip().split(";"):
                    if stmt.strip():
                        conn.execute(text(stmt))
                conn.commit()
        except Exception as e:
            logger.warning(f"knowledge: migration issue: {e}")
    return _engine


# ── chunking ──────────────────────────────────────────────────────────────────

def chunk_text(doc_text: str, target_chars: int = 3200, overlap_chars: int = 600) -> list[str]:
    """
    ~800-token chunks with ~150-token overlap, split on paragraph boundaries.
    (chars ≈ tokens*4 heuristic — plenty accurate for chunking purposes.)
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", doc_text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > target_chars:
            chunks.append(current)
            current = current[-overlap_chars:] + "\n\n" + para  # carry overlap tail
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current)
    return chunks


# ── ingestion (importable for scripts; the tool wraps this) ───────────────────

def sync_document(title: str, doc_text: str, drive_file_id: Optional[str] = None,
                  source_type: str = "drive") -> dict:
    """Upsert a document: replace its chunks with freshly embedded ones."""
    engine = _get_engine()
    chunks = chunk_text(doc_text)
    if not chunks:
        return {"error": "Document is empty after chunking."}

    embedded = 0
    with engine.connect() as conn:
        if drive_file_id:
            row = conn.execute(text(
                "INSERT INTO knowledge.documents (title, source_type, drive_file_id, synced_at) "
                "VALUES (:t, :s, :d, now()) "
                "ON CONFLICT (drive_file_id) DO UPDATE SET title = :t, synced_at = now() "
                "RETURNING id"
            ), {"t": title, "s": source_type, "d": drive_file_id}).fetchone()
        else:
            row = conn.execute(text(
                "INSERT INTO knowledge.documents (title, source_type) VALUES (:t, :s) RETURNING id"
            ), {"t": title, "s": source_type}).fetchone()
        doc_id = row[0]

        conn.execute(text("DELETE FROM knowledge.chunks WHERE document_id = :d"), {"d": doc_id})
        for i, chunk in enumerate(chunks):
            vec = embed(chunk)
            if vec:
                embedded += 1
            conn.execute(text(
                "INSERT INTO knowledge.chunks (document_id, chunk_index, content, embedding) "
                "VALUES (:d, :i, :c, CAST(:e AS vector))"
            ), {"d": doc_id, "i": i, "c": chunk, "e": to_vec_param(vec)})
        conn.commit()

    return {"ok": True, "document_id": doc_id, "title": title,
            "chunks": len(chunks), "embedded": embedded}


def search(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search across all knowledge chunks."""
    vec = embed(query)
    if not vec:
        return []
    engine = _get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT d.title, c.content, c.chunk_index,
                   1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity
            FROM knowledge.chunks c
            JOIN knowledge.documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:qvec AS vector)
            LIMIT :k
        """), {"qvec": to_vec_param(vec), "k": top_k}).fetchall()
    return [
        {"document": r.title, "chunk_index": r.chunk_index,
         "content": r.content, "similarity": round(float(r.similarity), 4)}
        for r in rows
    ]


# ── CrewAI tools ──────────────────────────────────────────────────────────────

class KBSearchInput(BaseModel):
    query: str = Field(description="What you want to know from the business knowledge base")
    top_k: int = Field(default=5, description="How many passages to return (max 10)")


class KBSearchTool(BaseTool):
    name: str = "kb_search"
    description: str = (
        "Semantic search over the business knowledge base: the EOS V/TO "
        "(mission, vision, plans), SOPs, and other ingested business documents. "
        "Use this to ground decisions and recommendations in the company's actual "
        "documented strategy."
    )
    args_schema: Type[BaseModel] = KBSearchInput

    def _run(self, query: str, top_k: int = 5) -> str:
        try:
            results = search(query, top_k=min(max(top_k, 1), 10))
            if not results:
                return json.dumps({"results": [], "note": "No matches (knowledge base may be empty)."})
            return json.dumps({"results": results})
        except Exception as e:
            logger.error(f"kb_search failed: {e}")
            return json.dumps({"error": str(e)[:400]})


class KBSyncInput(BaseModel):
    file_id: str = Field(description="Google Drive file ID of the document to ingest/refresh")
    title: str = Field(description="Document title for the knowledge base")


class KBSyncDriveDocTool(BaseTool):
    name: str = "kb_sync_drive_doc"
    description: str = (
        "Ingest or refresh a Google Drive document into the knowledge base "
        "(chunks + embeds it so every agent can search it). Use after a source "
        "document like the V/TO is updated."
    )
    args_schema: Type[BaseModel] = KBSyncInput

    def _run(self, file_id: str, title: str) -> str:
        try:
            from tools.n8n_bridge import _bridge_post
            raw = _bridge_post("drive-read", {"file_id": file_id})
            payload = json.loads(raw)
            content = payload.get("content", "")
            if not content:
                return json.dumps({"error": f"Drive read returned no content: {raw[:300]}"})
            result = sync_document(title=title, doc_text=content, drive_file_id=file_id)
            return json.dumps(result)
        except Exception as e:
            logger.error(f"kb_sync_drive_doc failed: {e}")
            return json.dumps({"error": str(e)[:400]})


def get_kb_tools(tool_names: list[str]) -> list:
    """Loader for kb.* and legacy dify.* declared names — all resolve to KB search;
    sync only when explicitly declared."""
    tools = []
    if any(n.startswith(("kb.search", "dify.")) for n in tool_names):
        tools.append(KBSearchTool())
    if any(n.startswith("kb.sync") for n in tool_names):
        tools.append(KBSyncDriveDocTool())
    return tools
