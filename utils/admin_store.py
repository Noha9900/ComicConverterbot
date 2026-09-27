"""
Persisted admin-controlled state: banlist/allowlist, the runtime-adjustable
rate limit and daily quota, and running usage stats for /stats.

Same single-JSON-file-with-atomic-replace pattern as utils/session.py.
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Set

import config

_LOCK = threading.Lock()
_PATH = os.path.join(config.DATA_DIR, "admin_state.json")


@dataclass
class AdminState:
    banned: Set[int] = field(default_factory=set)
    allowlist_enabled: bool = False
    allowed: Set[int] = field(default_factory=set)
    rate_limit_per_min: int = config.DEFAULT_RATE_LIMIT_PER_MIN
    daily_quota_mb: int = config.DEFAULT_DAILY_QUOTA_MB
    total_conversions: int = 0
    total_bytes_processed: int = 0
    known_users: Set[int] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)


_state = AdminState()


def _load() -> None:
    if not os.path.exists(_PATH):
        return
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    _state.banned = set(raw.get("banned", []))
    _state.allowlist_enabled = raw.get("allowlist_enabled", False)
    _state.allowed = set(raw.get("allowed", []))
    _state.rate_limit_per_min = raw.get("rate_limit_per_min", config.DEFAULT_RATE_LIMIT_PER_MIN)
    _state.daily_quota_mb = raw.get("daily_quota_mb", config.DEFAULT_DAILY_QUOTA_MB)
    _state.total_conversions = raw.get("total_conversions", 0)
    _state.total_bytes_processed = raw.get("total_bytes_processed", 0)
    _state.known_users = set(raw.get("known_users", []))
    _state.started_at = raw.get("started_at", time.time())


def _save() -> None:
    d = asdict(_state)
    d["banned"] = list(_state.banned)
    d["allowed"] = list(_state.allowed)
    d["known_users"] = list(_state.known_users)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _PATH)


_load()


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def is_banned(user_id: int) -> bool:
    with _LOCK:
        return user_id in _state.banned


def is_allowed(user_id: int) -> bool:
    with _LOCK:
        if is_admin(user_id):
            return True
        if not _state.allowlist_enabled:
            return True
        return user_id in _state.allowed


def ban(user_id: int) -> None:
    with _LOCK:
        _state.banned.add(user_id)
        _save()


def unban(user_id: int) -> None:
    with _LOCK:
        _state.banned.discard(user_id)
        _save()


def allow(user_id: int) -> None:
    with _LOCK:
        _state.allowed.add(user_id)
        _save()


def disallow(user_id: int) -> None:
    with _LOCK:
        _state.allowed.discard(user_id)
        _save()


def set_allowlist_enabled(value: bool) -> None:
    with _LOCK:
        _state.allowlist_enabled = value
        _save()


def get_allowlist_enabled() -> bool:
    with _LOCK:
        return _state.allowlist_enabled


def set_rate_limit(per_min: int) -> None:
    with _LOCK:
        _state.rate_limit_per_min = max(0, per_min)
        _save()


def get_rate_limit() -> int:
    with _LOCK:
        return _state.rate_limit_per_min


def set_daily_quota(mb: int) -> None:
    with _LOCK:
        _state.daily_quota_mb = max(0, mb)
        _save()


def get_daily_quota() -> int:
    with _LOCK:
        return _state.daily_quota_mb


def record_usage(user_id: int, num_bytes: int) -> None:
    with _LOCK:
        _state.known_users.add(user_id)
        _state.total_conversions += 1
        _state.total_bytes_processed += max(0, num_bytes)
        _save()


def touch_user(user_id: int) -> None:
    with _LOCK:
        if user_id not in _state.known_users:
            _state.known_users.add(user_id)
            _save()


def stats_snapshot() -> dict:
    with _LOCK:
        uptime_s = time.time() - _state.started_at
        return {
            "known_users": len(_state.known_users),
            "banned": len(_state.banned),
            "allowlist_enabled": _state.allowlist_enabled,
            "allowed": len(_state.allowed),
            "total_conversions": _state.total_conversions,
            "total_bytes_processed": _state.total_bytes_processed,
            "rate_limit_per_min": _state.rate_limit_per_min,
            "daily_quota_mb": _state.daily_quota_mb,
            "uptime_s": uptime_s,
        }
