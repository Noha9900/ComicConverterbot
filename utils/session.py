"""
Persistent per-user state: the conversion queue, output/quality/
language settings, in-progress volume/image batches, and whatever the
bot is currently waiting on (a rename reply, a batch pattern, an admin
value, ...).

Backed by a single JSON file under config.DATA_DIR instead of living
only in memory, so a container restart, redeploy, or crash does not
silently drop comics a user already converted but hasn't renamed yet.
For a single-instance bot this is enough; swap the load/save functions
for a Redis or SQLite backend if you ever run multiple replicas behind
a shared queue.
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import config

_LOCK = threading.Lock()
_STORE_PATH = os.path.join(config.DATA_DIR, "sessions.json")


@dataclass
class PendingFile:
    message_id: int
    original_name: str
    pdf_path: str
    added_at: float = field(default_factory=time.time)


@dataclass
class VolumeGroup:
    """Tracks parts of a multi-volume .rar/.cbr archive as they arrive."""

    group_key: str
    folder: str
    parts: List[str] = field(default_factory=list)
    last_part_at: float = field(default_factory=time.time)


@dataclass
class ImageBatch:
    """Tracks loose (non-archived) images sent one-by-one, so several
    images can be bundled into a single CBZ/PDF/EPUB output."""

    folder: str
    parts: List[str] = field(default_factory=list)
    last_part_at: float = field(default_factory=time.time)


@dataclass
class Settings:
    output_format: str = "pdf"          # pdf | cbz | cbr | epub | images
    quality: str = "medium"             # low | medium | high
    reading_direction: str = "ltr"      # ltr | rtl
    language: str = field(default_factory=lambda: config.DEFAULT_LANGUAGE)
    split_spreads: bool = False
    webtoon_slice: bool = False
    kindle_optimize: bool = False
    add_thumbnail: bool = False
    skip_cover: bool = False


@dataclass
class UserSession:
    queue: List[PendingFile] = field(default_factory=list)
    awaiting_rename_for: Optional[int] = None
    awaiting_batch_pattern: bool = False
    awaiting_pdf_title: bool = False
    awaiting_pdf_author: bool = False
    awaiting_admin_action: Optional[str] = None  # e.g. "rate_limit", "quota", "ban", "allow"
    volume_groups: Dict[str, VolumeGroup] = field(default_factory=dict)
    image_batch: Optional[ImageBatch] = None
    settings: Settings = field(default_factory=Settings)
    expect_pdf_for_split: bool = False
    expect_pdfs_for_merge: bool = False
    expect_split_custom: bool = False
    split_pdf_path: Optional[str] = None
    split_pdf_workdir: Optional[str] = None
    merge_buffer: List[str] = field(default_factory=list)
    pdf_title: str = ""
    pdf_author: str = ""


_sessions: Dict[str, UserSession] = {}  # keyed by str(user_id) for clean JSON round-trips

# In-memory only (not persisted): the running asyncio.Task for a user's
# current download/convert job, so /cancel or a button can cancel it.
_active_jobs: Dict[int, "asyncio.Task"] = {}


def _load() -> None:
    if not os.path.exists(_STORE_PATH):
        return
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    for uid, data in raw.items():
        image_batch_data = data.get("image_batch")
        session = UserSession(
            queue=[PendingFile(**pf) for pf in data.get("queue", [])],
            awaiting_rename_for=data.get("awaiting_rename_for"),
            awaiting_batch_pattern=data.get("awaiting_batch_pattern", False),
            awaiting_pdf_title=data.get("awaiting_pdf_title", False),
            awaiting_pdf_author=data.get("awaiting_pdf_author", False),
            awaiting_admin_action=data.get("awaiting_admin_action"),
            volume_groups={
                k: VolumeGroup(**v) for k, v in data.get("volume_groups", {}).items()
            },
            image_batch=ImageBatch(**image_batch_data) if image_batch_data else None,
            settings=Settings(**data.get("settings", {})),
            expect_pdf_for_split=data.get("expect_pdf_for_split", False),
            expect_pdfs_for_merge=data.get("expect_pdfs_for_merge", False),
            expect_split_custom=data.get("expect_split_custom", False),
            split_pdf_path=data.get("split_pdf_path"),
            split_pdf_workdir=data.get("split_pdf_workdir"),
            merge_buffer=data.get("merge_buffer", []),
            pdf_title=data.get("pdf_title", ""),
            pdf_author=data.get("pdf_author", ""),
        )
        session.queue = [pf for pf in session.queue if os.path.exists(pf.pdf_path)]
        _sessions[uid] = session


def _save() -> None:
    serializable = {
        uid: {
            "queue": [asdict(pf) for pf in s.queue],
            "awaiting_rename_for": s.awaiting_rename_for,
            "awaiting_batch_pattern": s.awaiting_batch_pattern,
            "awaiting_pdf_title": s.awaiting_pdf_title,
            "awaiting_pdf_author": s.awaiting_pdf_author,
            "awaiting_admin_action": s.awaiting_admin_action,
            "volume_groups": {k: asdict(v) for k, v in s.volume_groups.items()},
            "image_batch": asdict(s.image_batch) if s.image_batch else None,
            "settings": asdict(s.settings),
            "expect_pdf_for_split": s.expect_pdf_for_split,
            "expect_pdfs_for_merge": s.expect_pdfs_for_merge,
            "expect_split_custom": s.expect_split_custom,
            "split_pdf_path": s.split_pdf_path,
            "split_pdf_workdir": s.split_pdf_workdir,
            "merge_buffer": s.merge_buffer,
            "pdf_title": s.pdf_title,
            "pdf_author": s.pdf_author,
        }
        for uid, s in _sessions.items()
    }
    tmp_path = _STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f)
    os.replace(tmp_path, _STORE_PATH)


_load()


def get_session(user_id: int) -> UserSession:
    key = str(user_id)
    with _LOCK:
        if key not in _sessions:
            _sessions[key] = UserSession()
        return _sessions[key]


def save(user_id: int) -> None:
    """Call after mutating a session's queue/state to persist it immediately."""
    with _LOCK:
        _save()


def clear_session(user_id: int) -> None:
    with _LOCK:
        _sessions.pop(str(user_id), None)
        _save()


def clear_queue_only(user_id: int) -> None:
    """Used by /cancel when we want to drop the queue/volume groups but
    keep the user's settings."""
    session = get_session(user_id)
    session.queue = []
    session.volume_groups = {}
    session.image_batch = None
    session.awaiting_rename_for = None
    session.awaiting_batch_pattern = False
    session.expect_pdf_for_split = False
    session.expect_pdfs_for_merge = False
    save(user_id)


# --------------------------------------------------------------------------
# Active-job registry (cancellation support) -- process memory only
# --------------------------------------------------------------------------

def set_active_job(user_id: int, task) -> None:
    _active_jobs[user_id] = task


def get_active_job(user_id: int):
    return _active_jobs.get(user_id)


def clear_active_job(user_id: int) -> None:
    _active_jobs.pop(user_id, None)


def cancel_active_job(user_id: int) -> bool:
    task = _active_jobs.get(user_id)
    if task and not task.done():
        task.cancel()
        return True
    return False
