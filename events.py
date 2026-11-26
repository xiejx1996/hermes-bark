"""Bark delivery log — one JSON object per attempted push.

The desktop page reads this to answer "did the last thing actually go out?".
State lives in ``<hermes home>/plugin-data/hermes-bark/`` (the documented plugin
storage location, profile-scoped), never in the install dir: ``hermes plugins
update`` replaces that directory.

Recording is best-effort by contract. A notification path must never fail, and
never slow down, because a log file is unwritable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:  # package import
    from .settings import _profile_home
except ImportError:  # direct-path import (tests)
    try:
        from settings import _profile_home  # type: ignore
    except ImportError:  # pragma: no cover - tests that stub settings
        def _profile_home():  # type: ignore
            return None

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-bark"
MAX_LINES = 400
KEEP_LINES = 250


def data_dir() -> Optional[Path]:
    """``<hermes home>/plugin-data/hermes-bark`` for the home owning this copy."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID)
    except Exception:  # noqa: BLE001 — older Hermes or a bare test process
        home = os.environ.get("HERMES_HOME")
        root = Path(home) if home else _profile_home()
        if root is None:
            return None
        path = Path(root) / "plugin-data" / PLUGIN_ID
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return path


def log_path() -> Optional[Path]:
    root = data_dir()
    return (root / "deliveries.jsonl") if root is not None else None


def record(*, source: str, ok: bool, alias: str = "", device: str = "", body: str = "",
           title: str = "", error: str = "", status: Optional[int] = None,
           latency_ms: Optional[float] = None, message_id: str = "",
           alert_source: str = "", alert_weight: Optional[int] = None) -> None:
    """Append one delivery attempt. Never raises."""
    path = log_path()
    if path is None:
        return
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "epoch": round(time.time(), 3),
        "source": source,
        "ok": bool(ok),
        "alias": alias,
        "device": device,
        "title": title[:120],
        "body": body[:280],
        "error": error[:400],
        "status": status,
        "latency_ms": None if latency_ms is None else round(float(latency_ms), 1),
        "message_id": message_id,
        "alert_source": alert_source,
        "alert_weight": alert_weight,
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _rotate(path)
    except OSError:
        logger.debug("[bark] delivery log unwritable", exc_info=True)


def _rotate(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    kept = lines[-KEEP_LINES:]
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Newest first; unparseable lines are skipped, not fatal."""
    path = log_path()
    if path is None or not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(raw):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
        if len(out) >= max(1, min(int(limit or 50), 400)):
            break
    return out


def clear() -> bool:
    path = log_path()
    if path is None:
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def stats() -> dict[str, Any]:
    """Counts the page's hero card can show without re-scanning the file."""
    entries = recent(400)
    today = datetime.now().astimezone().date().isoformat()
    sent = sum(1 for item in entries if item.get("ok"))
    failed = sum(1 for item in entries if not item.get("ok"))
    today_count = sum(1 for item in entries if str(item.get("ts", ""))[:10] == today)
    last = entries[0] if entries else None
    return {
        "total": len(entries),
        "sent": sent,
        "failed": failed,
        "today": today_count,
        "last": last,
    }
