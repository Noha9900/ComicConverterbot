"""
Comic/ebook converter Telegram bot -- Pyrogram (MTProto) based.

See utils/i18n.py "start" string for the user-facing feature list, and
the README for the full architecture writeup. This file wires the
pieces in utils/ (converter, archives, ebook, pdfutil, session,
admin_store, ratelimit, cleanup, webhook_server, i18n) into Telegram
handlers and inline-button UI.
"""

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import utils.admin_store as admin_store
import utils.ratelimit as ratelimit
from utils.converter import (
    ZIP_EXTS,
    ConversionError,
    ConversionOptions,
    convert_with_options,
    is_archive_file,
    is_first_volume,
    volume_group_key,
)
from utils import pdfutil
from utils.cleanup import cleanup_loop
from utils.i18n import t
from utils.progress import ProgressTracker, format_progress
from utils.session import (
    ImageBatch,
    PendingFile,
    VolumeGroup,
    cancel_active_job,
    clear_active_job,
    clear_queue_only,
    clear_session,
    get_session,
    save,
    set_active_job,
)
from utils.webhook_server import start as start_webhook_server

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

_handlers = [logging.StreamHandler()]
if config.LOG_FILE:
    _handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=_handlers,
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

IMAGE_DOC_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PDF_EXT = ".pdf"

archive_filter = filters.document & filters.create(
    lambda _, __, m: bool(m.document and m.document.file_name and is_archive_file(m.document.file_name))
)
pdf_filter = filters.document & filters.create(
    lambda _, __, m: bool(m.document and m.document.file_name and m.document.file_name.lower().endswith(PDF_EXT))
)
loose_image_filter = (filters.document & filters.create(
    lambda _, __, m: bool(
        m.document and m.document.file_name and Path(m.document.file_name).suffix.lower() in IMAGE_DOC_EXTS
    )
)) | filters.photo

_finalize_tasks: Dict[Tuple[int, str], asyncio.Task] = {}
_pending_confirmations: Dict[str, dict] = {}  # large-file-size confirmations

COLOR_YELLOW = "🟨"
COLOR_BLUE = "🟦"
COLOR_GREEN = "🟩"
COLOR_RED = "🟥"
COLOR_PURPLE = "🟪"
CHECK = "✅"


# --------------------------------------------------------------------------
# Access control / rate limiting (applied to every incoming message)
# --------------------------------------------------------------------------

async def _access_check(message: Message) -> bool:
    user_id = message.from_user.id
    lang = get_session(user_id).settings.language
    if admin_store.is_banned(user_id):
        await message.reply_text(t("banned", lang))
        return False
    if not admin_store.is_allowed(user_id):
        await message.reply_text(t("not_allowlisted", lang))
        return False
    admin_store.touch_user(user_id)
    err = ratelimit.check_rate_limit(user_id)
    if err:
        await message.reply_text(err)
        return False
    return True


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
             InlineKeyboardButton("❓ Help", callback_data="menu:help")],
            [InlineKeyboardButton("📥 Queue", callback_data="viewqueue"),
             InlineKeyboardButton("🔗 Merge PDFs", callback_data="menu:merge")],
            [InlineKeyboardButton("✂️ Split a PDF", callback_data="menu:split")],
        ]
    )


def _settings_keyboard(s) -> InlineKeyboardMarkup:
    def mark(current, value):
        return f"{CHECK} " if current == value else ""

    rows = [
        [InlineKeyboardButton("— Output format —", callback_data="noop")],
        [
            InlineKeyboardButton(f"{mark(s.output_format,'pdf')}PDF", callback_data="set:format:pdf"),
            InlineKeyboardButton(f"{mark(s.output_format,'cbz')}CBZ", callback_data="set:format:cbz"),
            InlineKeyboardButton(f"{mark(s.output_format,'cbr')}CBR", callback_data="set:format:cbr"),
        ],
        [
            InlineKeyboardButton(f"{mark(s.output_format,'epub')}EPUB", callback_data="set:format:epub"),
            InlineKeyboardButton(f"{mark(s.output_format,'images')}Images", callback_data="set:format:images"),
        ],
        [InlineKeyboardButton("— Quality preset —", callback_data="noop")],
        [
            InlineKeyboardButton(f"{mark(s.quality,'low')}Low", callback_data="set:quality:low"),
            InlineKeyboardButton(f"{mark(s.quality,'medium')}Medium", callback_data="set:quality:medium"),
            InlineKeyboardButton(f"{mark(s.quality,'high')}High", callback_data="set:quality:high"),
        ],
        [InlineKeyboardButton("— Reading direction —", callback_data="noop")],
        [
            InlineKeyboardButton(f"{mark(s.reading_direction,'ltr')}Left → Right", callback_data="set:dir:ltr"),
            InlineKeyboardButton(f"{mark(s.reading_direction,'rtl')}Right → Left (Manga)", callback_data="set:dir:rtl"),
        ],
        [InlineKeyboardButton("— Language —", callback_data="noop")],
        [
            InlineKeyboardButton(f"{mark(s.language,'en')}English", callback_data="set:lang:en"),
            InlineKeyboardButton(f"{mark(s.language,'es')}Español", callback_data="set:lang:es"),
            InlineKeyboardButton(f"{mark(s.language,'hi')}हिन्दी", callback_data="set:lang:hi"),
        ],
        [InlineKeyboardButton("— Page tools (toggle) —", callback_data="noop")],
        [InlineKeyboardButton(f"{CHECK if s.split_spreads else '⬜'} Split two-page spreads", callback_data="toggle:split_spreads")],
        [InlineKeyboardButton(f"{CHECK if s.webtoon_slice else '⬜'} Webtoon slicer", callback_data="toggle:webtoon_slice")],
        [InlineKeyboardButton(f"{CHECK if s.kindle_optimize else '⬜'} Kindle / E-Ink optimize", callback_data="toggle:kindle_optimize")],
        [InlineKeyboardButton(f"{CHECK if s.add_thumbnail else '⬜'} Add thumbnail", callback_data="toggle:add_thumbnail")],
        [InlineKeyboardButton(f"{CHECK if s.skip_cover else '⬜'} Skip detected cover page", callback_data="toggle:skip_cover")],
        [InlineKeyboardButton("✍️ Set PDF title/author", callback_data="menu:pdfmeta")],
        [InlineKeyboardButton(f"{COLOR_RED} Close", callback_data="menu:close")],
    ]
    return InlineKeyboardMarkup(rows)


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
            [InlineKeyboardButton("🛑 Cancel this job", callback_data="job_cancel")],
        ]
    )


def _confirm_large_keyboard(cid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Continue anyway", callback_data=f"biglarge_yes:{cid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"biglarge_no:{cid}"),
        ]]
    )


def _admin_keyboard() -> InlineKeyboardMarkup:
    st = admin_store.stats_snapshot()
    allow_label = "🔓 Disable allowlist" if st["allowlist_enabled"] else "🔒 Enable allowlist"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Stats", callback_data="admin:stats")],
            [InlineKeyboardButton(f"⏱ Rate limit ({st['rate_limit_per_min']}/min)", callback_data="admin:ratelimit")],
            [InlineKeyboardButton(f"📦 Daily quota ({st['daily_quota_mb']} MB)", callback_data="admin:quota")],
            [InlineKeyboardButton(allow_label, callback_data="admin:allowtoggle")],
            [InlineKeyboardButton("🚫 Ban user", callback_data="admin:ban"),
             InlineKeyboardButton("✅ Unban user", callback_data="admin:unban")],
            [InlineKeyboardButton("➕ Allow user", callback_data="admin:allow"),
             InlineKeyboardButton("➖ Disallow user", callback_data="admin:disallow")],
            [InlineKeyboardButton(f"{COLOR_RED} Close", callback_data="menu:close")],
        ]
    )


def _split_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("2 parts", callback_data="split_parts:2"),
                InlineKeyboardButton("3 parts", callback_data="split_parts:3"),
                InlineKeyboardButton("4 parts", callback_data="split_parts:4"),
            ],
            [InlineKeyboardButton("Custom number of parts...", callback_data="split_parts:custom")],
        ]
    )


def _merge_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"✅ Merge {count} file(s) now", callback_data="merge_done"),
            InlineKeyboardButton("❌ Cancel", callback_data="merge_cancel"),
        ]]
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
    if session.image_batch and session.image_batch.parts:
        lines.append(f"\n**Loose images collected:** {len(session.image_batch.parts)}")
    return "\n".join(lines) if lines else "Your queue is empty."


def _safe_filename(name: str) -> str:
    allowed = set(" -_.()")
    cleaned = "".join(c for c in name if c.isalnum() or c in allowed).strip()
    return cleaned or "comic"


def _safe_folder_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def _opts_from_session(session) -> ConversionOptions:
    s = session.settings
    return ConversionOptions(
        output_format=s.output_format,
        quality=s.quality,
        split_spreads=s.split_spreads,
        webtoon_slice=s.webtoon_slice,
        kindle_optimize=s.kindle_optimize,
        reading_direction=s.reading_direction,
        add_thumbnail=s.add_thumbnail,
        skip_cover=s.skip_cover,
        title=session.pdf_title,
        author=session.pdf_author,
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    await message.reply_text(
        t("start", session.settings.language, ttl=config.FILE_TTL_HOURS),
        reply_markup=_main_menu_keyboard(),
    )


@app.on_message(filters.command("help"))
async def help_cmd(_, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    await message.reply_text(t("help", session.settings.language))


@app.on_message(filters.command("settings"))
async def settings_cmd(_, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    await message.reply_text("⚙️ **Settings** — tap to change:", reply_markup=_settings_keyboard(session.settings))


@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, message: Message):
    user_id = message.from_user.id
    cancelled = cancel_active_job(user_id)
    for key in list(get_session(user_id).volume_groups.keys()):
        _cancel_finalize_task(user_id, key)
    clear_queue_only(user_id)
    msg = "Your queue and any pending parts have been cleared."
    if cancelled:
        msg = "🛑 Running job cancelled. " + msg
    await message.reply_text(msg)


@app.on_message(filters.command("queue"))
async def queue_cmd(_, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    await message.reply_text(_queue_text(session))


@app.on_message(filters.command("convert"))
async def convert_cmd(client: Client, message: Message):
    if not await _access_check(message):
        return
    user_id = message.from_user.id
    session = get_session(user_id)
    did_something = False
    for key in list(session.volume_groups.keys()):
        _cancel_finalize_task(user_id, key)
        await finalize_group(client, user_id, message.chat.id, key)
        did_something = True
    if session.image_batch and session.image_batch.parts:
        await finalize_image_batch(client, user_id, message.chat.id)
        did_something = True
    if not did_something:
        await message.reply_text("Nothing is waiting to be finalized right now.")


@app.on_message(filters.command("merge"))
async def merge_cmd(_, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    session.expect_pdfs_for_merge = True
    session.merge_buffer = getattr(session, "merge_buffer", [])
    save(message.from_user.id)
    await message.reply_text(
        "Send the PDF files you want merged (one by one), then tap **Merge now** when done."
    )


@app.on_message(filters.command("splitpdf"))
async def splitpdf_cmd(_, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    session.expect_pdf_for_split = True
    save(message.from_user.id)
    await message.reply_text("Send the PDF you want to split.")


@app.on_message(filters.command("admin"))
async def admin_cmd(_, message: Message):
    user_id = message.from_user.id
    if not admin_store.is_admin(user_id):
        return
    await message.reply_text("🛠 **Admin panel**", reply_markup=_admin_keyboard())


@app.on_message(filters.command("stats"))
async def stats_cmd(_, message: Message):
    user_id = message.from_user.id
    if not admin_store.is_admin(user_id):
        return
    await message.reply_text(_stats_text())


def _stats_text() -> str:
    st = admin_store.stats_snapshot()
    hours = st["uptime_s"] / 3600
    mb = st["total_bytes_processed"] / (1024 * 1024)
    return (
        "📊 **Bot stats**\n"
        f"Uptime: {hours:.1f}h\n"
        f"Known users: {st['known_users']}\n"
        f"Banned: {st['banned']}\n"
        f"Allowlist: {'ON' if st['allowlist_enabled'] else 'off'} ({st['allowed']} allowed)\n"
        f"Total conversions: {st['total_conversions']}\n"
        f"Total data processed: {mb:.1f} MB\n"
        f"Rate limit: {st['rate_limit_per_min']}/min\n"
        f"Daily quota: {st['daily_quota_mb']} MB"
    )


# --------------------------------------------------------------------------
# Menu / settings callbacks
# --------------------------------------------------------------------------

@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(_, cq: CallbackQuery):
    await cq.answer()


@app.on_callback_query(filters.regex(r"^menu:(settings|help|merge|split|close|pdfmeta)$"))
async def cb_menu(client: Client, cq: CallbackQuery):
    action = cq.matches[0].group(1)
    user_id = cq.from_user.id
    session = get_session(user_id)
    await cq.answer()
    if action == "settings":
        await cq.message.edit_text("⚙️ **Settings** — tap to change:", reply_markup=_settings_keyboard(session.settings))
    elif action == "help":
        await cq.message.edit_text(t("help", session.settings.language))
    elif action == "merge":
        session.expect_pdfs_for_merge = True
        save(user_id)
        await cq.message.edit_text("Send the PDFs to merge (one by one), then use /merge again or tap Merge when ready.")
    elif action == "split":
        session.expect_pdf_for_split = True
        save(user_id)
        await cq.message.edit_text("Send the PDF you want to split.")
    elif action == "pdfmeta":
        session.awaiting_pdf_title = True
        save(user_id)
        await cq.message.edit_text("Send the **title** to embed in future PDFs (or `-` to clear it).")
    elif action == "close":
        await cq.message.delete()


@app.on_callback_query(filters.regex(r"^set:(format|quality|dir|lang):(\w+)$"))
async def cb_set(_, cq: CallbackQuery):
    field_name, value = cq.matches[0].group(1), cq.matches[0].group(2)
    user_id = cq.from_user.id
    session = get_session(user_id)
    if field_name == "format":
        session.settings.output_format = value
    elif field_name == "quality":
        session.settings.quality = value
    elif field_name == "dir":
        session.settings.reading_direction = value
    elif field_name == "lang":
        session.settings.language = value
    save(user_id)
    await cq.answer(f"Set {field_name} = {value}")
    try:
        await cq.message.edit_reply_markup(_settings_keyboard(session.settings))
    except MessageNotModified:
        pass


@app.on_callback_query(filters.regex(r"^toggle:(\w+)$"))
async def cb_toggle(_, cq: CallbackQuery):
    field_name = cq.matches[0].group(1)
    user_id = cq.from_user.id
    session = get_session(user_id)
    current = getattr(session.settings, field_name, False)
    setattr(session.settings, field_name, not current)
    save(user_id)
    await cq.answer("Updated.")
    try:
        await cq.message.edit_reply_markup(_settings_keyboard(session.settings))
    except MessageNotModified:
        pass


# --------------------------------------------------------------------------
# Standalone (.cbz / .zip) and multi-volume (.cbr / .rar) documents
# --------------------------------------------------------------------------

@app.on_message(archive_filter)
async def handle_document(client: Client, message: Message):
    if not await _access_check(message):
        return
    filename = message.document.file_name
    ext = Path(filename).suffix.lower()

    if not await _maybe_confirm_large_file(client, message, filename):
        return

    if ext in ZIP_EXTS:
        task = asyncio.create_task(_download_and_convert_single(client, message, filename))
    else:
        task = asyncio.create_task(_buffer_volume_part(client, message, filename))
    set_active_job(message.from_user.id, task)


async def _maybe_confirm_large_file(client: Client, message: Message, filename: str) -> bool:
    """Returns True if it's fine to proceed immediately. If the file is
    large, asks for confirmation via buttons and returns False (the
    caller should not proceed -- a callback resumes the flow)."""
    size = message.document.file_size or 0
    if size < config.FILE_SIZE_WARNING_MB * 1024 * 1024:
        return True
    cid = uuid.uuid4().hex[:10]
    _pending_confirmations[cid] = {"message": message, "filename": filename}
    mb = size / (1024 * 1024)
    await message.reply_text(
        f"⚠️ `{filename}` is {mb:.0f} MB, above the {config.FILE_SIZE_WARNING_MB} MB warning "
        f"threshold. Continue?",
        reply_markup=_confirm_large_keyboard(cid),
    )
    return False


@app.on_callback_query(filters.regex(r"^biglarge_(yes|no):(\w+)$"))
async def cb_biglarge(client: Client, cq: CallbackQuery):
    decision, cid = cq.matches[0].group(1), cq.matches[0].group(2)
    pending = _pending_confirmations.pop(cid, None)
    await cq.answer()
    if not pending:
        await cq.message.edit_text("This confirmation has expired.")
        return
    if decision == "no":
        await cq.message.edit_text("Cancelled.")
        return
    await cq.message.edit_text("Continuing...")
    message, filename = pending["message"], pending["filename"]
    ext = Path(filename).suffix.lower()
    if ext == PDF_EXT:
        task = asyncio.create_task(_handle_pdf_source(client, message, filename))
    elif ext in ZIP_EXTS:
        task = asyncio.create_task(_download_and_convert_single(client, message, filename))
    else:
        task = asyncio.create_task(_buffer_volume_part(client, message, filename))
    set_active_job(message.from_user.id, task)


async def _download_and_convert_single(client: Client, message: Message, original_name: str):
    user_id = message.from_user.id
    job_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(config.DOWNLOAD_DIR, "_jobs", job_id)
    os.makedirs(work_dir, exist_ok=True)
    archive_path = os.path.join(work_dir, original_name)

    status = await message.reply_text(f"⬇️ Starting download of `{original_name}`...")
    try:
        await _download_with_progress(message, archive_path, status, original_name)
        if not os.path.exists(archive_path):
            return
        err = ratelimit.check_and_consume_quota(user_id, message.document.file_size or 0)
        if err:
            await status.edit_text(err)
            return
        await _convert_and_queue(client, user_id, message.chat.id, archive_path, work_dir, original_name, status)
    except asyncio.CancelledError:
        await status.edit_text("🛑 Cancelled.")
    finally:
        clear_active_job(user_id)


# --------------------------------------------------------------------------
# Multi-volume (.cbr / .rar / .rNN / .partN.rar) documents
# --------------------------------------------------------------------------

def _volume_group_folder(user_id: int, group_key: str) -> str:
    return os.path.join(config.DOWNLOAD_DIR, "_volumes", str(user_id), _safe_folder_name(group_key))


async def _buffer_volume_part(client: Client, message: Message, filename: str):
    user_id = message.from_user.id
    chat_id = message.chat.id
    group_key = volume_group_key(filename) or Path(filename).stem.lower()

    try:
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
    except asyncio.CancelledError:
        pass
    finally:
        clear_active_job(user_id)


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
    return sorted(parts)[0]


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
        chat_id, f"🛠 Converting `{display_name}` ({len(group.parts)} part(s))..."
    )
    await _convert_and_queue(
        client, user_id, chat_id, entry, group.folder, display_name + Path(entry).suffix, status,
        cleanup_paths=group.parts,
    )


# --------------------------------------------------------------------------
# Loose images: buffered into a batch, then bundled into one output
# --------------------------------------------------------------------------

@app.on_message(loose_image_filter)
async def handle_loose_image(client: Client, message: Message):
    if not await _access_check(message):
        return
    # If we're mid split/merge flow, a photo here is unrelated -- ignore silently.
    session = get_session(message.from_user.id)
    if session.expect_pdf_for_split or session.expect_pdfs_for_merge:
        return
    task = asyncio.create_task(_buffer_image(client, message))
    set_active_job(message.from_user.id, task)


async def _buffer_image(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        session = get_session(user_id)
        folder = os.path.join(config.DOWNLOAD_DIR, "_images", str(user_id))
        os.makedirs(folder, exist_ok=True)

        if message.document:
            filename = message.document.file_name
        else:
            filename = f"photo_{uuid.uuid4().hex[:8]}.jpg"

        dest = os.path.join(folder, filename)
        await message.download(file_name=dest)

        batch = session.image_batch or ImageBatch(folder=folder)
        if dest not in batch.parts:
            batch.parts.append(dest)
        batch.last_part_at = time.time()
        session.image_batch = batch
        save(user_id)

        _schedule_image_batch_finalize(client, user_id, chat_id)
        await message.reply_text(
            f"🖼 Got image {len(batch.parts)} (\"{filename}\"). "
            f"Send more, or /convert to bundle now.\n"
            f"Auto-bundling in {config.IMAGE_BATCH_AUTO_FINALIZE_SECONDS}s if nothing else arrives."
        )
    except asyncio.CancelledError:
        pass
    finally:
        clear_active_job(user_id)


def _schedule_image_batch_finalize(client: Client, user_id: int, chat_id: int) -> None:
    key = (user_id, "__images__")
    _cancel_finalize_task(user_id, "__images__")

    async def _runner():
        try:
            await asyncio.sleep(config.IMAGE_BATCH_AUTO_FINALIZE_SECONDS)
            await finalize_image_batch(client, user_id, chat_id)
        except asyncio.CancelledError:
            pass

    _finalize_tasks[key] = asyncio.create_task(_runner())


async def finalize_image_batch(client: Client, user_id: int, chat_id: int) -> None:
    session = get_session(user_id)
    batch = session.image_batch
    session.image_batch = None
    save(user_id)
    if not batch or not batch.parts:
        return

    status = await client.send_message(chat_id, f"🛠 Bundling {len(batch.parts)} image(s)...")
    job_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(config.DOWNLOAD_DIR, "_jobs", job_id)
    os.makedirs(work_dir, exist_ok=True)

    from pathlib import Path as _Path

    opts = _opts_from_session(session)
    stem = f"images_{job_id}"
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: convert_with_options(
                "", work_dir, config.OUTPUT_DIR, stem, opts, source_images=[_Path(p) for p in batch.parts]
            ),
        )
    except ConversionError as e:
        await status.edit_text(f"❌ {e}")
        return
    finally:
        for p in batch.parts:
            try:
                os.remove(p)
            except OSError:
                pass

    admin_store.record_usage(user_id, sum(os.path.getsize(p) for p in batch.parts if os.path.exists(p)))
    await _deliver_result(client, user_id, chat_id, result, "images", status)


# --------------------------------------------------------------------------
# PDFs as a *source* (not going through /merge or /splitpdf): reformat /
# re-process an existing PDF through the settings pipeline.
# --------------------------------------------------------------------------

@app.on_message(pdf_filter)
async def handle_pdf(client: Client, message: Message):
    if not await _access_check(message):
        return
    session = get_session(message.from_user.id)
    filename = message.document.file_name

    if session.expect_pdf_for_split:
        task = asyncio.create_task(_handle_pdf_for_split(client, message, filename))
    elif session.expect_pdfs_for_merge:
        task = asyncio.create_task(_handle_pdf_for_merge(client, message, filename))
    else:
        if not await _maybe_confirm_large_file(client, message, filename):
            return
        task = asyncio.create_task(_handle_pdf_source(client, message, filename))
    set_active_job(message.from_user.id, task)


async def _handle_pdf_source(client: Client, message: Message, filename: str):
    user_id = message.from_user.id
    chat_id = message.chat.id
    session = get_session(user_id)
    if session.settings.output_format == "pdf":
        await message.reply_text(
            "Your output format is already **PDF** — nothing to convert. "
            "Change it in ⚙️ Settings if you want CBZ/CBR/EPUB/Images from this PDF."
        )
        return

    job_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(config.DOWNLOAD_DIR, "_jobs", job_id)
    os.makedirs(work_dir, exist_ok=True)
    pdf_path = os.path.join(work_dir, filename)
    status = await message.reply_text(f"⬇️ Downloading `{filename}`...")
    try:
        await _download_with_progress(message, pdf_path, status, filename)
        if not os.path.exists(pdf_path):
            return
        err = ratelimit.check_and_consume_quota(user_id, message.document.file_size or 0)
        if err:
            await status.edit_text(err)
            return
        await status.edit_text(f"🛠 Converting `{filename}`...")
        opts = _opts_from_session(session)
        stem = Path(filename).stem
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: convert_with_options(pdf_path, work_dir, config.OUTPUT_DIR, stem, opts, is_pdf_source=True)
            )
        except ConversionError as e:
            await status.edit_text(f"❌ {e}")
            return
        admin_store.record_usage(user_id, os.path.getsize(pdf_path))
        await _deliver_result(client, user_id, chat_id, result, stem, status)
    except asyncio.CancelledError:
        await status.edit_text("🛑 Cancelled.")
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass
        clear_active_job(user_id)


# --------------------------------------------------------------------------
# /splitpdf flow
# --------------------------------------------------------------------------

async def _handle_pdf_for_split(client: Client, message: Message, filename: str):
    user_id = message.from_user.id
    session = get_session(user_id)
    session.expect_pdf_for_split = False
    save(user_id)

    job_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(config.DOWNLOAD_DIR, "_jobs", job_id)
    os.makedirs(work_dir, exist_ok=True)
    pdf_path = os.path.join(work_dir, filename)
    status = await message.reply_text(f"⬇️ Downloading `{filename}`...")
    try:
        await _download_with_progress(message, pdf_path, status, filename)
        if not os.path.exists(pdf_path):
            return
        pages = await asyncio.get_running_loop().run_in_executor(None, pdfutil.page_count, pdf_path)
        session.split_pdf_path = pdf_path
        session.split_pdf_workdir = work_dir
        save(user_id)
        await status.edit_text(
            f"`{filename}` has {pages} page(s). How many parts?", reply_markup=_split_keyboard()
        )
    except asyncio.CancelledError:
        await status.edit_text("🛑 Cancelled.")
    finally:
        clear_active_job(user_id)


@app.on_callback_query(filters.regex(r"^split_parts:(\w+)$"))
async def cb_split_parts(client: Client, cq: CallbackQuery):
    value = cq.matches[0].group(1)
    user_id = cq.from_user.id
    session = get_session(user_id)
    await cq.answer()
    pdf_path = getattr(session, "split_pdf_path", None)
    work_dir = getattr(session, "split_pdf_workdir", None)
    if not pdf_path or not os.path.exists(pdf_path):
        await cq.message.edit_text("That PDF is no longer available -- send it again with /splitpdf.")
        return
    if value == "custom":
        session.awaiting_admin_action = None
        session.expect_split_custom = True
        save(user_id)
        await cq.message.edit_text("Send the number of parts as a plain number.")
        return
    await cq.message.edit_text("✂️ Splitting...")
    await _do_split(client, cq.message.chat.id, user_id, pdf_path, work_dir, int(value))


async def _do_split(client: Client, chat_id: int, user_id: int, pdf_path: str, work_dir: str, num_parts: int):
    out_dir = os.path.join(work_dir, "split_out")
    try:
        parts = await asyncio.get_running_loop().run_in_executor(
            None, lambda: pdfutil.split_pdf(pdf_path, out_dir, num_parts=num_parts)
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Split failed")
        await client.send_message(chat_id, f"❌ Split failed: {e}")
        return
    for p in parts:
        await _send_file_with_ttl(client, chat_id, p, os.path.basename(p))
    admin_store.record_usage(user_id, os.path.getsize(pdf_path))


# --------------------------------------------------------------------------
# /merge flow
# --------------------------------------------------------------------------

async def _handle_pdf_for_merge(client: Client, message: Message, filename: str):
    user_id = message.from_user.id
    session = get_session(user_id)
    folder = os.path.join(config.DOWNLOAD_DIR, "_merge", str(user_id))
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, filename)
    status = await message.reply_text(f"⬇️ Downloading `{filename}`...")
    try:
        await _download_with_progress(message, dest, status, filename)
        if not os.path.exists(dest):
            return
        buf = getattr(session, "merge_buffer", None) or []
        buf.append(dest)
        session.merge_buffer = buf
        save(user_id)
        await status.edit_text(
            f"✅ Added `{filename}` ({len(buf)} queued for merge).",
            reply_markup=_merge_keyboard(len(buf)),
        )
    except asyncio.CancelledError:
        await status.edit_text("🛑 Cancelled.")
    finally:
        clear_active_job(user_id)


@app.on_callback_query(filters.regex(r"^merge_(done|cancel)$"))
async def cb_merge(client: Client, cq: CallbackQuery):
    action = cq.matches[0].group(1)
    user_id = cq.from_user.id
    chat_id = cq.message.chat.id
    session = get_session(user_id)
    buf = getattr(session, "merge_buffer", None) or []
    session.expect_pdfs_for_merge = False
    session.merge_buffer = []
    save(user_id)
    await cq.answer()

    if action == "cancel" or not buf:
        for p in buf:
            try:
                os.remove(p)
            except OSError:
                pass
        await cq.message.edit_text("Merge cancelled.")
        return

    await cq.message.edit_text(f"🔗 Merging {len(buf)} PDF(s)...")
    out_dir = os.path.join(config.OUTPUT_DIR, "_jobs", uuid.uuid4().hex[:8])
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "merged.pdf")
    try:
        await asyncio.get_running_loop().run_in_executor(None, pdfutil.merge_pdfs, buf, out_path)
    except Exception as e:  # noqa: BLE001
        logger.exception("Merge failed")
        await client.send_message(chat_id, f"❌ Merge failed: {e}")
        return
    finally:
        for p in buf:
            try:
                os.remove(p)
            except OSError:
                pass
    admin_store.record_usage(user_id, os.path.getsize(out_path))
    await _send_file_with_ttl(client, chat_id, out_path, "merged.pdf")


# --------------------------------------------------------------------------
# Shared download / convert / deliver helpers
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
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress edit skipped: %s", e)

    try:
        await message.download(file_name=dest_path, progress=dl_progress)
    except asyncio.CancelledError:
        raise
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
    cleanup_paths=None,
) -> None:
    session = get_session(user_id)
    opts = _opts_from_session(session)
    stem = Path(original_name).stem

    try:
        await status.edit_text(f"🛠 Converting `{original_name}`, please wait...")
    except MessageNotModified:
        pass

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: convert_with_options(archive_path, work_dir, config.OUTPUT_DIR, stem, opts)
        )
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

    try:
        admin_store.record_usage(user_id, os.path.getsize(archive_path) if os.path.exists(archive_path) else 0)
    except OSError:
        pass

    await _deliver_result(client, user_id, chat_id, result, original_name, status)


async def _deliver_result(client: Client, user_id: int, chat_id: int, result, original_name: str, status: Message) -> None:
    warn = f"\n⚠️ {result.warning}" if result.warning else ""

    if result.output_format == "images":
        n = len(os.listdir(result.output_path))
        await status.edit_text(f"✅ Extracted {n} image(s) from `{original_name}`. Sending...{warn}")
        await _send_images(client, chat_id, result.output_path)
        for v in result.extra_videos:
            await _send_video(client, chat_id, v)
        return

    ext = os.path.splitext(result.output_path)[1]
    session = get_session(user_id)
    session.queue.append(
        PendingFile(message_id=0, original_name=Path(original_name).stem + ext, pdf_path=result.output_path)
    )
    idx = len(session.queue) - 1
    save(user_id)

    await status.edit_text(
        f"✅ `{original_name}` converted to **{result.output_format.upper()}**!{warn}\n\n"
        f"Queued as file #{idx + 1}. What would you like to do with it?",
        reply_markup=_rename_keyboard(idx),
    )
    if result.thumbnail_path and os.path.exists(result.thumbnail_path):
        try:
            await client.send_photo(chat_id, result.thumbnail_path, caption="Preview thumbnail")
        except Exception as e:  # noqa: BLE001
            logger.debug("Thumbnail preview send skipped: %s", e)

    for v in result.extra_videos:
        await _send_video(client, chat_id, v)


async def _send_images(client: Client, chat_id: int, folder: str) -> None:
    from pyrogram.types import InputMediaPhoto

    files = sorted(Path(folder).glob("*"))
    for i in range(0, len(files), 10):
        chunk = files[i : i + 10]
        try:
            if len(chunk) > 1:
                media = [InputMediaPhoto(str(p)) for p in chunk]
                await client.send_media_group(chat_id, media)
            else:
                await client.send_photo(chat_id, str(chunk[0]))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to send image batch chunk: %s", e)
    _schedule_path_delete(folder)


async def _send_video(client: Client, chat_id: int, video_path: str) -> None:
    try:
        await client.send_video(chat_id, video_path, caption=os.path.basename(video_path))
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to send extracted video %s: %s", video_path, e)


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
        sent = await client.send_document(chat_id, final_path, file_name=display_name, progress=up_progress)
        await status.delete()
        _schedule_message_delete(client, chat_id, sent.id)
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
            pass


async def _send_file_with_ttl(client: Client, chat_id: int, path: str, display_name: str):
    try:
        sent = await client.send_document(chat_id, path, file_name=display_name)
        _schedule_message_delete(client, chat_id, sent.id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed sending %s", display_name)
        await client.send_message(chat_id, f"❌ Failed to send `{display_name}`: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _schedule_message_delete(client: Client, chat_id: int, message_id: int) -> None:
    """Files are only meant to live for FILE_TTL_HOURS -- delete the
    bot's own delivered copy from the chat after that, in addition to
    the disk-level cleanup in utils/cleanup.py."""

    async def _runner():
        await asyncio.sleep(config.FILE_TTL_HOURS * 3600)
        try:
            await client.delete_messages(chat_id, message_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("Scheduled delete skipped for %s/%s: %s", chat_id, message_id, e)

    asyncio.create_task(_runner())


def _schedule_path_delete(path: str) -> None:
    async def _runner():
        await asyncio.sleep(config.FILE_TTL_HOURS * 3600)
        try:
            if os.path.isdir(path):
                import shutil

                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    asyncio.create_task(_runner())


# --------------------------------------------------------------------------
# Inline button callbacks: rename / skip / batch / queue / cancel / job
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
    await _send_pdf(client, cq.message.chat.id, pending, pending.original_name)


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
        f"(extension is kept automatically)."
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
        f"Use `{{n}}` for the sequence number, e.g. `Chapter {{n}}`."
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
    for key in list(get_session(user_id).volume_groups.keys()):
        _cancel_finalize_task(user_id, key)
    clear_queue_only(user_id)
    await cq.answer("Queue and pending parts cleared.", show_alert=True)


@app.on_callback_query(filters.regex(r"^job_cancel$"))
async def cb_job_cancel(_, cq: CallbackQuery):
    user_id = cq.from_user.id
    ok = cancel_active_job(user_id)
    await cq.answer("Job cancelled." if ok else "No running job found.", show_alert=True)


# --------------------------------------------------------------------------
# Admin panel callbacks
# --------------------------------------------------------------------------

@app.on_callback_query(filters.regex(r"^admin:(\w+)$"))
async def cb_admin(_, cq: CallbackQuery):
    user_id = cq.from_user.id
    if not admin_store.is_admin(user_id):
        await cq.answer("Admins only.", show_alert=True)
        return
    action = cq.matches[0].group(1)
    session = get_session(user_id)
    await cq.answer()

    if action == "stats":
        await cq.message.edit_text(_stats_text(), reply_markup=_admin_keyboard())
    elif action == "allowtoggle":
        admin_store.set_allowlist_enabled(not admin_store.get_allowlist_enabled())
        await cq.message.edit_reply_markup(_admin_keyboard())
    elif action == "ratelimit":
        session.awaiting_admin_action = "rate_limit"
        save(user_id)
        await cq.message.edit_text("Send the new rate limit (requests/min, 0 = unlimited).")
    elif action == "quota":
        session.awaiting_admin_action = "quota"
        save(user_id)
        await cq.message.edit_text("Send the new daily quota in MB (0 = unlimited).")
    elif action in ("ban", "unban", "allow", "disallow"):
        session.awaiting_admin_action = action
        save(user_id)
        await cq.message.edit_text(f"Send the numeric Telegram user id to {action}.")


# --------------------------------------------------------------------------
# Free-text handler: rename / batch-pattern / pdf-meta / admin-input replies
# --------------------------------------------------------------------------

@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(["start", "cancel", "queue", "convert", "settings", "help", "merge", "splitpdf", "admin", "stats"])
)
async def handle_text(client: Client, message: Message):
    user_id = message.from_user.id
    if not await _access_check(message):
        return
    session = get_session(user_id)

    if getattr(session, "expect_split_custom", False):
        session.expect_split_custom = False
        text = message.text.strip()
        if not text.isdigit() or int(text) < 1:
            await message.reply_text("Please send a positive number.")
            return
        pdf_path = getattr(session, "split_pdf_path", None)
        work_dir = getattr(session, "split_pdf_workdir", None)
        save(user_id)
        if not pdf_path or not os.path.exists(pdf_path):
            await message.reply_text("That PDF is no longer available -- send it again with /splitpdf.")
            return
        await message.reply_text("✂️ Splitting...")
        await _do_split(client, message.chat.id, user_id, pdf_path, work_dir, int(text))
        return

    if session.awaiting_admin_action:
        action = session.awaiting_admin_action
        session.awaiting_admin_action = None
        save(user_id)
        if not admin_store.is_admin(user_id):
            return
        text = message.text.strip()
        if action in ("rate_limit", "quota"):
            if not text.lstrip("-").isdigit():
                await message.reply_text("Please send a plain number.")
                return
            n = int(text)
            if action == "rate_limit":
                admin_store.set_rate_limit(n)
                await message.reply_text(f"Rate limit set to {n}/min.")
            else:
                admin_store.set_daily_quota(n)
                await message.reply_text(f"Daily quota set to {n} MB.")
        elif action in ("ban", "unban", "allow", "disallow"):
            if not text.lstrip("-").isdigit():
                await message.reply_text("Please send a numeric user id.")
                return
            uid = int(text)
            getattr(admin_store, action)(uid)
            await message.reply_text(f"Done: {action} {uid}.")
        return

    if session.awaiting_pdf_title:
        session.awaiting_pdf_title = False
        title = message.text.strip()
        session.pdf_title = "" if title == "-" else title
        session.awaiting_pdf_author = True
        save(user_id)
        await message.reply_text("Now send the **author** to embed (or `-` to clear it).")
        return

    if session.awaiting_pdf_author:
        session.awaiting_pdf_author = False
        author = message.text.strip()
        session.pdf_author = "" if author == "-" else author
        save(user_id)
        await message.reply_text("Saved. New conversions will use this title/author.")
        return

    if session.awaiting_rename_for is not None:
        idx = session.awaiting_rename_for
        session.awaiting_rename_for = None
        if idx >= len(session.queue):
            save(user_id)
            await message.reply_text("That file is no longer queued.")
            return
        pending = session.queue.pop(idx)
        save(user_id)
        ext = os.path.splitext(pending.pdf_path)[1] or os.path.splitext(pending.original_name)[1]
        new_name = _safe_filename(message.text.strip()) + ext
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
            ext = os.path.splitext(pending.pdf_path)[1] or ".pdf"
            new_name = _safe_filename(pattern.replace("{n}", str(i))) + ext
            await _send_pdf(client, user_id, pending, new_name)
        return

    # Not awaiting anything -- normal chat message, nothing to do.


# --------------------------------------------------------------------------
# Startup: background cleanup loop + optional health/webhook server
# --------------------------------------------------------------------------

async def _on_startup(client: Client):
    asyncio.create_task(cleanup_loop())
    if config.ENABLE_HEALTH_SERVER:
        await start_webhook_server(client)


if __name__ == "__main__":
    logger.info("Starting Comic/Ebook converter bot...")

    async def _main():
        await app.start()
        await _on_startup(app)
        logger.info("Bot is running.")
        await asyncio.Event().wait()

    asyncio.get_event_loop().run_until_complete(_main())
