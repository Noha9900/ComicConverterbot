"""
Central configuration for the comic-archive Telegram bot.
All values are read from environment variables (see .env.example), so
the same image/container works unmodified across VPS, Docker, and
platforms like Koyeb / Railway / Render.
"""

import os
import shutil

from dotenv import load_dotenv

load_dotenv()  # no-op in production if you inject real env vars instead of a .env file

# --- Telegram credentials -----------------------------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# --- Filesystem -----------------------------------------------------------
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
DATA_DIR = os.getenv("DATA_DIR", "data")

# --- Behaviour --------------------------------------------------------------
MULTIVOLUME_AUTO_FINALIZE_SECONDS = int(os.getenv("MULTIVOLUME_AUTO_FINALIZE_SECONDS", "20"))
PROGRESS_EDIT_INTERVAL = float(os.getenv("PROGRESS_EDIT_INTERVAL", "1.5"))

# Loose-image batches (for "multiple images -> CBZ/PDF/EPUB") use the same
# idle-window auto-finalize idea as multi-volume RAR parts.
IMAGE_BATCH_AUTO_FINALIZE_SECONDS = int(os.getenv("IMAGE_BATCH_AUTO_FINALIZE_SECONDS", "25"))

# --- Admins / access control -------------------------------------------------
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}

# --- Auto-cleanup ("files only live 2 hours") --------------------------------
FILE_TTL_HOURS = float(os.getenv("FILE_TTL_HOURS", "2"))
CLEANUP_INTERVAL_MINUTES = float(os.getenv("CLEANUP_INTERVAL_MINUTES", "10"))

# --- Rate limiting / quotas (admin-adjustable at runtime via /admin) --------
DEFAULT_RATE_LIMIT_PER_MIN = int(os.getenv("DEFAULT_RATE_LIMIT_PER_MIN", "0"))  # 0 = unlimited
DEFAULT_DAILY_QUOTA_MB = int(os.getenv("DEFAULT_DAILY_QUOTA_MB", "0"))  # 0 = unlimited

# --- Misc UX -----------------------------------------------------------------
FILE_SIZE_WARNING_MB = int(os.getenv("FILE_SIZE_WARNING_MB", "300"))
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "en")

# --- Ops: health server, logging ---------------------------------------------
ENABLE_HEALTH_SERVER = os.getenv("ENABLE_HEALTH_SERVER", "true").lower() == "true"
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8080"))
LOG_FILE = os.getenv("LOG_FILE", "")  # empty string = console-only logging

# Pyrogram talks MTProto directly (a persistent socket), not the classic
# Bot-API HTTP long-poll/webhook model, so there is no "push updates over
# HTTP" mode to switch on here. What WEBHOOK_MODE actually enables is a
# small aiohttp server (see webhook_server.py) that exposes /health and an
# optional /notify endpoint another one of your systems can call to make
# the bot push a message to a chat -- useful for external triggers, but it
# is not Telegram-update delivery. See README for the full explanation.
WEBHOOK_MODE = os.getenv("WEBHOOK_MODE", "false").lower() == "true"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Real CBR archives are the proprietary RAR format. We can only *produce*
# genuine .cbr/.rar output if a real `rar` command-line binary is present
# on the host/image; otherwise "CBR output" silently falls back to a
# .cbz (ZIP) with the same page contents, which every comic reader accepts
# just as well for reading (only the file extension/compression differs).
RAR_BINARY = shutil.which("rar")

for _d in (DOWNLOAD_DIR, OUTPUT_DIR, DATA_DIR):
    os.makedirs(_d, exist_ok=True)


def validate() -> None:
    """Fail fast with a clear message instead of a cryptic Pyrogram error."""
    missing = [
        name
        for name, val in (("API_ID", API_ID), ("API_HASH", API_HASH), ("BOT_TOKEN", BOT_TOKEN))
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in your .env file or in your host's environment settings."
        )
    if not ADMIN_IDS:
        import logging

        logging.getLogger("comic-bot").warning(
            "ADMIN_IDS is empty -- no one will be able to use /admin or /stats. "
            "Set ADMIN_IDS=<your numeric Telegram user id> in .env."
        )
