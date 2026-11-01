"""Single Socket Mode worker for hosting; configuration comes from the environment."""
import asyncio
import os
import signal

os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

from config import settings
from main import app, lifespan
from slack_socket import run
from loguru import logger


async def serve():
    if not settings.slack_enabled or not settings.slack_socket_mode or not settings.slack_app_token:
        raise RuntimeError("Complete and enable the Socket Mode configuration before starting")
    if settings.obsidian_enabled:
        if settings.obsidian_live:
            from obsidian import refresh_live_snapshot
            token = settings.obsidian_github_token
            await asyncio.to_thread(
                refresh_live_snapshot, settings.obsidian_snapshot_path,
                token.get_secret_value() if token else None, settings.obsidian_seed_path)
        from obsidian import load_snapshot
        snapshot = load_snapshot(settings.obsidian_snapshot_path)
        logger.info("Business Brain ready: notes={} revision={}",
                    len(snapshot["notes"]), snapshot["revision"])
    if os.environ.get("FRIDAY_RESTORE_CHECKPOINT") == "milestone-5":
        from friday_restore import restore
        await asyncio.to_thread(restore, settings.database_url)
    async with lifespan(app):
        receiver = asyncio.create_task(run(app))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, receiver.cancel)
        try:
            await receiver
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(serve())
