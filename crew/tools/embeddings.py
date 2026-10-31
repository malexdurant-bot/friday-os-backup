"""
Xcerebro 2.0 — Shared embeddings

One place for the embedding model choice, used by both the episodic memory
(tools/memory.py) and the knowledge base (tools/knowledge.py).

text-embedding-3-large truncated to 1536 dims: outperforms 3-small at the
same vector size and fits the existing vector(1536) pgvector columns.
OpenRouter has no embeddings endpoint — this goes to OpenAI directly.
"""

import os
from typing import Optional

from loguru import logger

EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMS = 1536

_client = None


def _get_client():
    global _client
    if _client is None:
        import openai
        _client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    return _client


def embed(text_input: str) -> Optional[list[float]]:
    """Embed text. Returns None (logged loudly) on failure or missing key."""
    if not os.getenv("OPENAI_API_KEY"):
        logger.error("embeddings: OPENAI_API_KEY missing — semantic retrieval degraded")
        return None
    try:
        response = _get_client().embeddings.create(
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMS,
            input=text_input[:8000],
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"embeddings: FAILED (semantic retrieval degraded): {e}")
        return None


def to_vec_param(embedding: Optional[list[float]]) -> Optional[str]:
    """pgvector text literal '[0.1,0.2,...]' for use as a bound SQL param."""
    if not embedding:
        return None
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"
