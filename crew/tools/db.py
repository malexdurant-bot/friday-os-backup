"""
Xcerebro 2.0 — DB bootstrap helper

The runtime boots with `uvicorn --workers 2` (see Dockerfile), so every
worker's FastAPI lifespan runs Base.metadata.create_all() concurrently on
first boot. When a table doesn't exist yet, two workers racing on the same
CREATE TABLE can collide on Postgres's implicit composite-type creation
(psycopg2.errors.UniqueViolation on pg_type_typname_nsp_index) even though
one of them succeeds. A retry clears it: by the second attempt the table
already exists and create_all() is a no-op.
"""

import time
from loguru import logger
from sqlalchemy.engine import Engine


def create_all_with_retry(engine: Engine, base, attempts: int = 3, delay_seconds: float = 0.5) -> None:
    """Run Base.metadata.create_all(), retrying through concurrent-worker create races."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            base.metadata.create_all(engine)
            return
        except Exception as e:
            last_error = e
            if attempt < attempts:
                logger.warning(f"create_all race (attempt {attempt}/{attempts}), retrying: {e}")
                time.sleep(delay_seconds)
    raise last_error
