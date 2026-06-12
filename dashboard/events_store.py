"""File-based store for doorbell press events.

The MQTT listener appends each incoming press to `events.json`; the Streamlit
dashboard polls that same file. Reads and writes are guarded by an fcntl
advisory lock on POSIX (best-effort elsewhere).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

try:
    import fcntl  # POSIX
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import dashboard_config as cfg

_LOCK = threading.Lock()


class _FileLock:
    """Context manager for fcntl.flock with a graceful fallback."""

    def __init__(self, fh):
        self.fh = fh

    def __enter__(self):
        if _HAS_FCNTL:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self.fh

    def __exit__(self, exc_type, exc, tb):
        if _HAS_FCNTL:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        return False


def _ensure_file() -> None:
    if not os.path.exists(cfg.EVENTS_PATH):
        os.makedirs(os.path.dirname(cfg.EVENTS_PATH), exist_ok=True)
        with open(cfg.EVENTS_PATH, "w", encoding="utf-8") as fh:
            json.dump([], fh)


def _load(fh) -> List[Dict[str, Any]]:
    fh.seek(0)
    raw = fh.read()
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def append_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Append a press event. Returns the stored record (with an id/ts filled in)."""
    event = dict(payload) if isinstance(payload, dict) else {}
    event.setdefault("id", uuid.uuid4().hex)
    event.setdefault("ts", "")
    event["received_at"] = _utcnow_iso()

    with _LOCK:
        _ensure_file()
        with open(cfg.EVENTS_PATH, "r+", encoding="utf-8") as fh:
            with _FileLock(fh):
                events = _load(fh)
                events.append(event)
                if len(events) > cfg.MAX_EVENTS:
                    events = events[-cfg.MAX_EVENTS:]
                fh.seek(0)
                fh.truncate()
                json.dump(events, fh, indent=2)
    return event


def read_events(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return events in chronological order (oldest first)."""
    with _LOCK:
        _ensure_file()
        with open(cfg.EVENTS_PATH, "r", encoding="utf-8") as fh:
            with _FileLock(fh):
                events = _load(fh)
    if limit is not None:
        return events[-limit:]
    return events


def latest_event() -> Optional[Dict[str, Any]]:
    events = read_events()
    return events[-1] if events else None


def _utcnow_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat() + "Z"
