"""
Reusable progress-bar math for Pyrogram's download/upload progress callbacks.

Pyrogram calls the progress callback with (current_bytes, total_bytes) many
times per second. ProgressTracker turns that stream into a human-friendly
bar, instantaneous speed (MB/s) and ETA, while `format_progress` renders the
final message text shown to the user.
"""

import time


class ProgressTracker:
    """Stateful helper: one instance per active transfer."""

    def __init__(self) -> None:
        self._start_time = time.time()
        self._last_time = self._start_time
        self._last_current = 0

    def reset(self) -> None:
        self._start_time = time.time()
        self._last_time = self._start_time
        self._last_current = 0

    @staticmethod
    def make_bar(current: int, total: int, length: int = 14) -> str:
        if total <= 0:
            return "░" * length
        filled = max(0, min(length, int(length * current / total)))
        return "█" * filled + "░" * (length - filled)

    def speed(self, current: int) -> float:
        """Instantaneous speed in bytes/sec since the last call."""
        now = time.time()
        elapsed = now - self._last_time
        if elapsed <= 0:
            return 0.0
        spd = max(0.0, (current - self._last_current) / elapsed)
        self._last_current = current
        self._last_time = now
        return spd

    @staticmethod
    def eta(current: int, total: int, speed: float) -> str:
        if speed <= 0 or total <= 0 or current >= total:
            return "--:--"
        remaining = (total - current) / speed
        m, s = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @staticmethod
    def human(size: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"


def format_progress(label: str, current: int, total: int, tracker: ProgressTracker) -> str:
    percent = (current / total * 100) if total else 0.0
    bar = tracker.make_bar(current, total)
    speed = tracker.speed(current)
    eta = tracker.eta(current, total, speed)
    return (
        f"{label}\n"
        f"`[{bar}]` {percent:5.1f}%\n"
        f"{tracker.human(current)} / {tracker.human(total)}\n"
        f"⚡ {tracker.human(speed)}/s   ⏳ ETA {eta}"
    )
