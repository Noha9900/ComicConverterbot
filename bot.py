"""
CBZ/CBR/ZIP/RAR (including multi-volume RAR) -> PDF Telegram bot.

Built on Pyrogram (MTProto) because its native download/upload
`progress` callback gives clean (current_bytes, total_bytes) samples,
which is what powers the live speed/percentage bars. Being MTProto
based also means file transfers aren't capped by the classic Bot-API
HTTP limits -- the real ceiling here is Telegram's own 2GB-per-file
platform limit, not anything this code imposes.

Flow
----
1. User sends a .cbz/.zip -> converted immediately (single archive,
   nothing to wait for).
2. User sends a .cbr/.rar, or any recognizable volume part
   (`Name.part1.rar`, `Name.r00`, ...) -> buffered into a per-user
   "volume group" on disk. The bot waits `MULTIVOLUME_AUTO_FINALIZE_SECONDS`
   of inactivity for more parts, or the user can send /convert to
   finalize immediately. This is what makes multi-volume RAR/CBR sets
   work with no manual merging required.
3. Whichever path finishes, the archive is extracted, converted to a
   single PDF (CPU-bound work runs in a thread pool so the event loop
   stays responsive), and queued.
4. Inline buttons let the user Rename / Skip / Batch-rename every
   queued PDF, after which it's uploaded with its own live progress bar.

The queue and any in-progress volume groups are persisted to disk
(see utils/session.py), so a restart doesn't lose work.
"""

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Tuple

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from utils.converter import (
    ZIP_EXTS,
    ConversionError,
    convert_to_pdf,
    is_archive_file,
    is_first_volume,
    volume_group_key,
)
from utils.progress import ProgressTracker, format_progress
from utils.session import PendingFile, VolumeGroup, clear_session, get_session, save

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("comic-bot")

config.validate()

app = Client(
    "comic_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.DOWNLOAD_DIR,
)

archive_filter = filters.document & filters.create(
    lambda _, __, m: bool(m.document and m.document.file_name and is_archive_file(m.document.file_name))
)

# Tracks the pending auto-finalize timer per (user_id, group_key) so a
# fresh part arriving resets the countdown instead of racing it.
_finalize_tasks: Dict[Tuple[int, str], asyncio.Task] = {}


# Telegram gives bots no way to set an inline button's actual background
# color -- that's fixed by the client, not something InlineKeyboardButton
# exposes. A leading colored-square emoji is the closest real equivalent,
# and it does render as a solid colored square in every Telegram client.
COLOR_YELLOW = "🟨"
COLOR_BLUE = "🟦"
COLOR_GREEN = "🟩"
COLOR_RED = "🟥"
COLOR_PURPLE = "🟪"


def _rename_keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{COLOR_YELLOW} Rename this file", callback_data=f"rename:{idx}"),
                InlineKeyboardButton(f"{COLOR_RED} Skip", callback_data=f"skip:{idx}"),
            ],
            [InlineKeyboardButton(f"{COLOR_GREEN} Batch rename all queued", callback_data="batch")],
            [
                InlineKeyboardButton(f"{COLOR_BLUE} View queue", callback_data="viewqueue"),
                InlineKeyboardButton(f"{COLOR_PURPLE} Cancel queue", callback_data="cancelall"),
            ],
        ]
    )


def _queue_text(session) -> str:
    lines = []
    if session.queue:
        lines.append("**Ready, waiting for rename/skip:**")
        lines += [f"{i + 1}. {f.original_name}" for i, f in enumerate(session.queue)]
    if session.volume_groups:
        lines.append("\n**Multi-volume archives still collecting parts:**")
        for key, group in session.volume_groups.items():
            lines.append(f"• {key}: {len(group.parts)} part(s) received")
    return "\n".join(lines) if lines else "Your queue is empty."


def _safe_filename(name: str) -> str:
    allowed = set(" -_.()")
    cleaned = "".join(c for c in name if c.isalnum() or c in allowed).strip()
    return cleaned or "comic"


def _safe_folder_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    await message.reply_text(
        "**📚 Comic Archive → PDF Converter Bot**\n\n"
        "Send `.cbz` / `.zip` files for instant conversion, or `.cbr` / `.rar` "
        "(including multi-volume sets like `Name.part1.rar` + `Name.part2.rar`, "
        "or `Name.rar` + `Name.r00` + `Name.r01` ...) and I'll wait for all the "
        "parts before converting.\n\n"
        "After each conversion you choose what happens to the file:\n"
        "• **Rename this file** — set a name just for that one PDF\n"
        "• **Skip** — keep the original filename\n"
        "• **Batch rename all queued** — send several comics first, then apply "
        "one numbered pattern (e.g. `Chapter {n}`) to all of them at once\n\n"
        "Commands:\n"
        "/convert — finalize any multi-volume archive right now instead of "
        "waiting for the auto-timer\n"
        "/queue — see what's waiting\n"
        "/cancel — clear your queue and any pending volume parts"
    )


@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, message: Message):
    clear_session(message.from_user.id)
    await message.reply_text("Your queue and any pending volume parts have been cleared.")


@app.on_message(filters.command("queue"))
async def queue_cmd(_, message: Message):
    session = get_session(message.from_user.id)
    await message.reply_text(_queue_text(session))


@app.on_message(filters.command("convert"))
async def convert_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    session = get_session(user_id)
    if not session.volume_groups:
        await message.reply_text("There's no multi-volume archive waiting to be finalized.")
        return
    for key in list(session.volume_groups.keys()):
        _cancel_finalize_task(user_id, key)
        await finalize_group(client, user_id, message.chat.id, key)


# --------------------------------------------------------------------------
# Standalone (.cbz / .zip) documents: download -> convert -> queue
# --------------------------------------------------------------------------

@app.on_message(archive_filter)
async def handle_document(client: Client, message: Message):
    filename = message.document.file_name
    ext = Path(filename).suffix.lower()

    if ext in ZIP_EXTS:
        await _download_and_convert_single(client, message, filename)
    else:
        await _buffer_volume_part(client, message, filename)


async def _download_and_convert_single(client: Client, message: Message, original_name: str):
    user_id = message.from_user.id
    job_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(config.DOWNLOAD_DIR, "_jobs", job_id)
    os.makedirs(work_dir, exist_ok=True)
    archive_path = os.path.join(work_dir, original_name)

    status = await message.reply_text(f"⬇️ Starting download of `{original_name}`...")
    await _download_with_progress(message, archive_path, status, original_name)
    if not os.path.exists(archive_path):
        return  # download failed; error already reported

    await _convert_and_queue(client, user_id, message.chat.id, archive_path, work_dir, original_name, status)


# --------------------------------------------------------------------------
# Multi-volume (.cbr / .rar / .rNN / .partN.rar) documents
# --------------------------------------------------------------------------

def _volume_group_folder(user_id: int, group_key: str) -> str:
    return os.path.join(config.DOWNLOAD_DIR, "_volumes", str(user_id), _safe_folder_name(group_key))


async def _buffer_volume_part(client: Client, message: Message, filename: str):
    user_id = message.from_user.id
    chat_id = message.chat.id
    group_key = volume_group_key(filename) or Path(filename).stem.lower()

    session = get_session(user_id)
    group = session.volume_groups.get(group_key)
    folder = _volume_group_folder(user_id, group_key)
    os.makedirs(folder, exist_ok=True)
    if group is None:
        group = VolumeGroup(group_key=group_key, folder=folder)
        session.volume_groups[group_key] = group

    part_path = os.path.join(folder, filename)
    status = await message.reply_text(f"⬇️ Downloading part `{filename}`...")
    await _download_with_progress(message, part_path, status, filename)
    if not os.path.exists(part_path):
        return

    if part_path not in group.parts:
        group.parts.append(part_path)
    group.last_part_at = time.time()
    save(user_id)

    if config.MULTIVOLUME_AUTO_FINALIZE_SECONDS > 0:
        note = (
            f"⏳ Auto-finalizing in {config.MULTIVOLUME_AUTO_FINALIZE_SECONDS}s "
            f"if no more parts arrive, or send /convert now."
        )
        _schedule_auto_finalize(client, user_id, chat_id, group_key)
    else:
        note = "Send more parts, then /convert when the archive is complete."

    await status.edit_text(f"✅ Got part `{filename}` ({len(group.parts)} part(s) so far).\n{note}")


def _cancel_finalize_task(user_id: int, group_key: str) -> None:
    task = _finalize_tasks.pop((user_id, group_key), None)
    if task and not task.done():
        task.cancel()


def _schedule_auto_finalize(client: Client, user_id: int, chat_id: int, group_key: str) -> None:
    _cancel_finalize_task(user_id, group_key)

    async def _runner():
        try:
            await asyncio.sleep(config.MULTIVOLUME_AUTO_FINALIZE_SECONDS)
            await finalize_group(client, user_id, chat_id, group_key)
        except asyncio.CancelledError:
            pass

    _finalize_tasks[(user_id, group_key)] = asyncio.create_task(_runner())


def _pick_entry_file(parts) -> str:
    for p in parts:
        if is_first_volume(os.path.basename(p)):
            return p
    return sorted(parts)[0]  # best-effort fallback if no recognizable first volume


def _display_stem(entry_path: str) -> str:
    name = os.path.basename(entry_path)
    m = re.match(r"^(.+)\.part\d+\.rar$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.match(r"^(.+)\.r\d{2,3}$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    return Path(name).stem


async def finalize_group(client: Client, user_id: int, chat_id: int, group_key: str) -> None:
    session = get_session(user_id)
    group = session.volume_groups.pop(group_key, None)
    save(user_id)
    if not group or not group.parts:
        return

    entry = _pick_entry_file(group.parts)
    display_name = _display_stem(entry)
    status = await client.send_message(
        chat_id, f"🛠 Converting `{display_name}` ({len(group.parts)} part(s)) to PDF..."
    )
    output_pdf = os.path.join(group.folder, display_name + ".pdf")

    await _convert_and_queue(
        client, user_id, chat_id, entry, group.folder, display_name + Path(entry).suffix, status,
        output_pdf_override=output_pdf,
        cleanup_paths=group.parts,
    )


# --------------------------------------------------------------------------
# Shared download / convert / queue helpers
# --------------------------------------------------------------------------

async def _download_with_progress(message: Message, dest_path: str, status: Message, label: str) -> None:
    tracker = ProgressTracker()
    last_edit = {"t": 0.0}

    async def dl_progress(current: int, total: int):
        now = time.time()
        if now - last_edit["t"] < config.PROGRESS_EDIT_INTERVAL and current != total:
            return
        last_edit["t"] = now
        try:
            text = format_progress(f"⬇️ Downloading `{label}`", current, total, tracker)
            await status.edit_text(text)
        except MessageNotModified:
            pass
        except Exception as e:  # noqa: BLE001 - progress edits must never crash the transfer
            logger.debug("Progress edit skipped: %s", e)

    try:
        await message.download(file_name=dest_path, progress=dl_progress)
    except Exception as e:
        logger.exception("Download failed for %s", label)
        await status.edit_text(f"❌ Download failed: {e}")


async def _convert_and_queue(
    client: Client,
    user_id: int,
    chat_id: int,
    archive_path: str,
    work_dir: str,
    original_name: str,
    status: Message,
    output_pdf_override: str = None,
    cleanup_paths=None,
) -> None:
    output_pdf = output_pdf_override or os.path.join(
        work_dir, os.path.splitext(original_name)[0] + ".pdf"
    )

    try:
        await status.edit_text(f"🛠 Converting `{original_name}` to PDF, please wait...")
    except MessageNotModified:
        pass

    try:
        loop = asyncio.get_running_loop()
        # CPU-bound (zip/rar extraction + image re-encoding): run off the event loop.
        await loop.run_in_executor(None, convert_to_pdf, archive_path, work_dir, output_pdf)
    except ConversionError as e:
        await status.edit_text(f"❌ Conversion failed: {e}")
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected conversion error for %s", original_name)
        await status.edit_text(f"❌ Unexpected error during conversion: {e}")
        return
    finally:
        for p in (cleanup_paths or [archive_path]):
            try:
                os.remove(p)
            except OSError:
                pass

    session = get_session(user_id)
    session.queue.append(
        PendingFile(message_id=0, original_name=original_name, pdf_path=output_pdf)
    )
    idx = len(session.queue) - 1
    save(user_id)

    await status.edit_text(
        f"✅ `{original_name}` converted!\n\n"
        f"Queued as file #{idx + 1}. What would you like to do with it?",
        reply_markup=_rename_keyboard(idx),
    )


async def _send_pdf(client: Client, chat_id: int, pending: PendingFile, display_name: str):
    tracker = ProgressTracker()
    status = await client.send_message(chat_id, f"⬆️ Uploading `{display_name}`...")
    last_edit = {"t": 0.0}

    async def up_progress(current: int, total: int):
        now = time.time()
        if now - last_edit["t"] < config.PROGRESS_EDIT_INTERVAL and current != total:
            return
        last_edit["t"] = now
        try:
            text = format_progress(f"⬆️ Uploading `{display_name}`", current, total, tracker)
            await status.edit_text(text)
        except MessageNotModified:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress edit skipped: %s", e)

    final_path = os.path.join(os.path.dirname(pending.pdf_path), display_name)
    if final_path != pending.pdf_path:
        os.replace(pending.pdf_path, final_path)

    try:
        await client.send_document(chat_id, final_path, file_name=display_name, progress=up_progress)
        await status.delete()
    except Exception as e:
        logger.exception("Upload failed for %s", display_name)
        await status.edit_text(f"❌ Upload failed: {e}")
    finally:
        try:
            os.remove(final_path)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(final_path))
        except OSError:
            pass  # directory not empty (other queued files still in it) - fine


# --------------------------------------------------------------------------
# Inline button callbacks
# --------------------------------------------------------------------------

@app.on_callback_query(filters.regex(r"^skip:(\d+)$"))
async def cb_skip(client: Client, cq: CallbackQuery):
    idx = int(cq.matches[0].group(1))
    user_id = cq.from_user.id
    session = get_session(user_id)
    if idx >= len(session.queue):
        await cq.answer("This file is no longer available.", show_alert=True)
        return
    pending = session.queue.pop(idx)
    save(user_id)
    await cq.answer()
    await cq.message.edit_text(f"Keeping the original name for `{pending.original_name}`.")
    display_name = os.path.splitext(pending.original_name)[0] + ".pdf"
    await _send_pdf(client, user_id, pending, display_name)


@app.on_callback_query(filters.regex(r"^rename:(\d+)$"))
async def cb_rename(_, cq: CallbackQuery):
    idx = int(cq.matches[0].group(1))
    user_id = cq.from_user.id
    session = get_session(user_id)
    if idx >= len(session.queue):
        await cq.answer("This file is no longer available.", show_alert=True)
        return
    session.awaiting_rename_for = idx
    save(user_id)
    await cq.answer()
    await cq.message.edit_text(
        f"Send the new name for `{session.queue[idx].original_name}` "
        f"(you don't need to include `.pdf`)."
    )


@app.on_callback_query(filters.regex(r"^batch$"))
async def cb_batch(_, cq: CallbackQuery):
    user_id = cq.from_user.id
    session = get_session(user_id)
    if not session.queue:
        await cq.answer("Your queue is empty.", show_alert=True)
        return
    session.awaiting_batch_pattern = True
    save(user_id)
    await cq.answer()
    await cq.message.edit_text(
        f"Send a naming pattern for all {len(session.queue)} queued file(s).\n"
        f"Use `{{n}}` for the sequence number, e.g. `Chapter {{n}}` -> "
        f"`Chapter 1.pdf`, `Chapter 2.pdf`, ..."
    )


@app.on_callback_query(filters.regex(r"^viewqueue$"))
async def cb_viewqueue(client: Client, cq: CallbackQuery):
    user_id = cq.from_user.id
    session = get_session(user_id)
    await cq.answer()
    await client.send_message(user_id, _queue_text(session))


@app.on_callback_query(filters.regex(r"^cancelall$"))
async def cb_cancelall(_, cq: CallbackQuery):
    user_id = cq.from_user.id
    session = get_session(user_id)
    for key in list(session.volume_groups.keys()):
        _cancel_finalize_task(user_id, key)
    clear_session(user_id)
    await cq.answer("Queue and pending volume parts cleared.", show_alert=True)


# --------------------------------------------------------------------------
# Free-text handler: only used to capture rename / batch-pattern replies
# --------------------------------------------------------------------------

@app.on_message(
    filters.text & filters.private & ~filters.command(["start", "cancel", "queue", "convert"])
)
async def handle_text(client: Client, message: Message):
    user_id = message.from_user.id
    session = get_session(user_id)

    if session.awaiting_rename_for is not None:
        idx = session.awaiting_rename_for
        session.awaiting_rename_for = None
        if idx >= len(session.queue):
            save(user_id)
            await message.reply_text("That file is no longer queued.")
            return
        pending = session.queue.pop(idx)
        save(user_id)
        new_name = _safe_filename(message.text.strip()) + ".pdf"
        await message.reply_text(f"Renaming to `{new_name}` and uploading...")
        await _send_pdf(client, user_id, pending, new_name)
        return

    if session.awaiting_batch_pattern:
        session.awaiting_batch_pattern = False
        pattern = message.text.strip()
        if "{n}" not in pattern:
            pattern = pattern + " {n}"
        queue, session.queue = session.queue, []
        save(user_id)
        await message.reply_text(f"Batch renaming and uploading {len(queue)} file(s)...")
        for i, pending in enumerate(queue, start=1):
            new_name = _safe_filename(pattern.replace("{n}", str(i))) + ".pdf"
            await _send_pdf(client, user_id, pending, new_name)
        return

    # Not awaiting anything - a normal chat message, nothing to do.


if __name__ == "__main__":
    logger.info("Starting Comic-to-PDF bot...")
    app.run()
