"""
Auto-cleanup: nothing this bot produces or buffers is meant to live on
disk (or in the chat) forever. A background loop sweeps job/work
directories every CLEANUP_INTERVAL_MINUTES and removes anything older
than FILE_TTL_HOURS (default 2h). bot.py separately schedules deletion
of the *Telegram messages* it sends after the same TTL, so both the
server copy and the delivered copy disappear on the same schedule.
"""

import asyncio
import logging
import os
import shutil
import time

import config

logger = logging.getLogger(__name__)


def _sweep_dir(base: str, cutoff: float) -> int:
    removed = 0
    if not os.path.isdir(base):
        return removed
    for name in os.listdir(base):
        path = os.path.join(base, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
        except OSError as e:
            logger.debug("Cleanup could not remove %s: %s", path, e)
    return removed


def sweep_once() -> int:
    cutoff = time.time() - config.FILE_TTL_HOURS * 3600
    total = 0
    for base in (
        os.path.join(config.DOWNLOAD_DIR, "_jobs"),
        os.path.join(config.DOWNLOAD_DIR, "_volumes"),
        os.path.join(config.DOWNLOAD_DIR, "_images"),
        os.path.join(config.OUTPUT_DIR, "_jobs"),
    ):
        total += _sweep_dir(base, cutoff)
    if total:
        logger.info("Auto-cleanup removed %d item(s) older than %sh", total, config.FILE_TTL_HOURS)
    return total


async def cleanup_loop() -> None:
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, sweep_once)
        except Exception:  # noqa: BLE001 - a bad sweep must never kill the bot
            logger.exception("Auto-cleanup sweep failed")
        await asyncio.sleep(max(60, config.CLEANUP_INTERVAL_MINUTES * 60))
