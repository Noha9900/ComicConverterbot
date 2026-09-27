"""
Per-user rate limiting (sliding 60s window) and a daily data quota.
Both limits are 0 (disabled) by default and are admin-adjustable at
runtime through utils.admin_store, without a restart.
"""

import json
import os
import threading
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Dict, Optional

import config
import utils.admin_store as admin_store

_LOCK = threading.Lock()
_calls: Dict[int, deque] = defaultdict(deque)

_QUOTA_PATH = os.path.join(config.DATA_DIR, "quota.json")
_quota_usage: Dict[str, dict] = {}


def _load_quota() -> None:
    if not os.path.exists(_QUOTA_PATH):
        return
    try:
        with open(_QUOTA_PATH, "r", encoding="utf-8") as f:
            _quota_usage.update(json.load(f))
    except (json.JSONDecodeError, OSError):
        pass


def _save_quota() -> None:
    tmp = _QUOTA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_quota_usage, f)
    os.replace(tmp, _QUOTA_PATH)


_load_quota()


def check_rate_limit(user_id: int) -> Optional[str]:
    """Returns an error message if the user must wait, else None (and
    records this call toward the limit)."""
    limit = admin_store.get_rate_limit()
    if limit <= 0 or admin_store.is_admin(user_id):
        return None
    now = datetime.now().timestamp()
    with _LOCK:
        dq = _calls[user_id]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= limit:
            wait = 60 - (now - dq[0])
            return f"⏳ Rate limit reached ({limit}/min). Try again in {int(wait) + 1}s."
        dq.append(now)
    return None


def check_and_consume_quota(user_id: int, num_bytes: int) -> Optional[str]:
    """Returns an error message if today's quota is exhausted, else None
    (and books `num_bytes` against today's usage)."""
    quota_mb = admin_store.get_daily_quota()
    if quota_mb <= 0 or admin_store.is_admin(user_id):
        return None
    today = date.today().isoformat()
    key = str(user_id)
    with _LOCK:
        entry = _quota_usage.get(key)
        if not entry or entry.get("date") != today:
            entry = {"date": today, "bytes": 0}
        used_mb = entry["bytes"] / (1024 * 1024)
        if used_mb >= quota_mb:
            return f"📊 Daily quota reached ({quota_mb} MB/day). Try again tomorrow."
        entry["bytes"] += max(0, num_bytes)
        _quota_usage[key] = entry
        _save_quota()
    return None


def quota_remaining_mb(user_id: int) -> Optional[float]:
    quota_mb = admin_store.get_daily_quota()
    if quota_mb <= 0:
        return None
    today = date.today().isoformat()
    entry = _quota_usage.get(str(user_id))
    used_mb = (entry["bytes"] / (1024 * 1024)) if entry and entry.get("date") == today else 0.0
    return max(0.0, quota_mb - used_mb)
