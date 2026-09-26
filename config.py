"""
Central configuration for the CBZ/CBR -> PDF Telegram bot.
All values are read from environment variables (see .env.example),
so the same image/container works unmodified across VPS, Docker, and
platforms like Koyeb / Railway / Render.

No artificial caps are imposed here on queue size, batch size, or file
count -- the only real ceilings are Telegram's own platform limits
(2GB per file, 4096 chars per message), which live outside this bot's
control entirely.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # no-op in production if you inject real env vars instead of a .env file

# --- Telegram credentials -----------------------------------------------
# API_ID / API_HASH come from https://my.telegram.org (required by Pyrogram
# even for bots). BOT_TOKEN comes from @BotFather.
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# --- Filesystem -------------------------------------------------------------
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
# Where the persistent queue/session store lives, so a container restart
# does not lose files a user already converted but hasn't renamed yet.
DATA_DIR = os.getenv("DATA_DIR", "data")

# --- Behaviour ----------------------------------------------------------------
# How long (seconds) the bot waits for further volume parts of a
# multi-volume .rar/.cbr archive before assuming the user is done
# uploading and auto-finalizing. Users can also finalize immediately
# with /convert. Set to 0 to disable auto-finalize and always require
# /convert.
MULTIVOLUME_AUTO_FINALIZE_SECONDS = int(os.getenv("MULTIVOLUME_AUTO_FINALIZE_SECONDS", "20"))

# How often (seconds) progress messages are allowed to be edited.
# Telegram rate-limits frequent edits to the same message; 1.2-2.0s is safe.
PROGRESS_EDIT_INTERVAL = float(os.getenv("PROGRESS_EDIT_INTERVAL", "1.5"))

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
