"""Alerts: an optional title + body a producer writes; absent one, the text goes through.

Why this module exists: a notification is read on a lock screen, where the title is
bold and about four lines of body show without a long-press. Text written
for a reader ("# Report ... ## Summary ...") therefore opens with structure and puts
the point at the bottom, exactly where nobody looks. The fix is not to slice the
text -- a fragment ("(none) ... hygiene ...") says even less -- but to let whoever
knows what happened write the notification.

An alert is **optional**. With none supplied, a push behaves like the plain
upstream plugin: whatever text arrived is the body. Supplying one buys a real title
and a body that starts with the point; the plugin only checks that it is sendable
and that its opening line survives the collapsed view.

Three ways an alert reaches the plugin:

* ``bark_notify`` tool -- an agent, in-session or inside a cron turn.
* ``hermes bark push --title/--body/--alert-file`` -- scripts and shell paths.
* an in-band block at the top of the text (``标题：…`` / ``摘要：…``), for a script
  whose stdout is delivered verbatim.

Long bodies are fine: the notification expands on a long-press, so the budget is
soft. What is *not* fine is putting the conclusion at the end, where expansion
hides it -- that is reported back as a warning, never blocked.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

#: Title budget: ~18 Chinese characters. Measured: a 42-unit title was cut with an
#: ellipsis around 37-39 units, and narrower devices fit less.
DEFAULT_MAX_TITLE_WEIGHT = 36
#: What the collapsed view shows: four lines. Measured at ~21 Chinese characters per
#: line (84 characters, ~168 units) on a current iPhone at the default text size;
#: 140 leaves room for the narrower screens and larger text sizes.
COLLAPSED_WEIGHT = 140
#: Payload guard only. APNs refuses a payload over 4096 bytes; this leaves room for
#: the title and the other fields. A body this large is a bug in the producer.
HARD_BODY_BYTES = 3000
DEFAULT_TITLE = "Hermes"

_SENTENCE_RE = re.compile(r"[。！？；!?;\n]|\.(?=\s|$)")
_COMMA_RE = re.compile(r"[，,]")
_LABEL_RE = re.compile(
    r"^\s*(?P<label>标题|摘要|正文|title|subtitle|summary|body)\s*[:：]\s*(?P<value>.*)$",
    re.IGNORECASE)
_LABELS = {
    "标题": "title", "title": "title", "subtitle": "subtitle",
    "摘要": "body", "正文": "body", "summary": "body", "body": "body",
}


def display_weight(text: str) -> int:
    """Width of *text* as a notification renders it: full-width chars count 2."""
    total = 0
    for char in str(text or ""):
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


class AlertError(ValueError):
    """An alert the caller must fix. The message is shown to the caller verbatim."""


@dataclass(frozen=True)
class Limits:
    max_title_weight: int = DEFAULT_MAX_TITLE_WEIGHT
    collapsed_weight: int = COLLAPSED_WEIGHT
    body_bytes: int = HARD_BODY_BYTES


@dataclass(frozen=True)
class Alert:
    """A notification someone wrote, or the text that stands in for one."""

    title: str
    body: str
    source: str = ""            # tool | cli | block | file | send
    clipped: bool = False       # True when the title had to be cut
    extras: Mapping[str, Any] = field(default_factory=dict)  # level/sound/group/ttl/url/...
    warnings: tuple[str, ...] = ()   # advisory only: the alert still goes out

    @property
    def weight(self) -> int:
        return display_weight(self.body)


def clip(text: str, max_weight: int) -> tuple[str, bool]:
    """Cut *text* to *max_weight*, preferring a sentence boundary over a hard cut."""
    raw = str(text or "").strip()
    if max_weight <= 0 or display_weight(raw) <= max_weight:
        return raw, False

    kept, used = "", 0
    for char in raw:
        cost = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + cost > max_weight:
            break
        kept += char
        used += cost

    # Prefer a sentence end, but never return a stub: the boundary has to keep at
    # least 40% of the budget. A Latin full stop only counts before whitespace or at
    # the end, so 1.2 GB is not read as a sentence break. A comma is the second choice.
    boundary = ""
    for pattern in (_SENTENCE_RE, _COMMA_RE):
        cuts = [m.end() for m in pattern.finditer(kept)]
        if cuts:
            candidate = kept[: cuts[-1]]
            if display_weight(candidate) >= max_weight * 0.4:
                boundary = candidate
                break
    if boundary:
        return boundary.strip(), True
    while kept and display_weight(kept) > max(max_weight - 1, 1):
        kept = kept[:-1]
    return (kept.rstrip() + "…").strip(), True


def parse_block(text: str) -> Optional[Alert]:
    """Read a leading ``标题：…`` / ``摘要：…`` block out of text; ``None`` when absent.

    For producers whose only output channel is stdout: a script can print the alert
    first and the report after it. Only the first dozen lines are searched, and the
    block ends at the first blank line, so a body that merely mentions "title:" deep
    inside a report is never mistaken for one.
    """
    lines = str(text or "").splitlines()
    start = None
    for index, line in enumerate(lines[:12]):
        if _LABEL_RE.match(line):
            start = index
            break
    if start is None:
        return None

    fields: dict[str, str] = {}
    current = ""
    for line in lines[start:]:
        match = _LABEL_RE.match(line)
        if match:
            label = match.group("label")
            current = _LABELS.get(label.lower(), _LABELS.get(label, ""))
            if not current:
                continue
            value = match.group("value").strip()
            fields[current] = (fields.get(current, "") + "\n" + value).strip() if fields.get(current) else value
            continue
        if not line.strip():
            break                      # a blank line closes the block
        if not current:
            break                      # prose before any label: not a block
        fields[current] = (fields[current] + "\n" + line.strip()).strip()

    body = (fields.get("body") or "").strip()
    if not body:
        return None
    extras = {"subtitle": fields["subtitle"]} if fields.get("subtitle") else {}
    return Alert(title=(fields.get("title") or DEFAULT_TITLE).strip(), body=body,
                 source="block", extras=extras)


def _first_sentence_weight(body: str) -> int:
    first = re.split(r"(?<=[。！？!?；;\n])", body, maxsplit=1)[0]
    return display_weight(first)


def advisories(alert: Alert, *, limits: Optional[Limits] = None) -> tuple[str, ...]:
    """Non-blocking notes for the producer: things that will look worse on a phone.

    A long body is not one of them -- it expands. A conclusion that only appears
    after the first four lines is: the reader decides from the collapsed view.
    """
    limits = limits or Limits()
    notes: list[str] = []
    first = _first_sentence_weight(alert.body)
    if first > limits.collapsed_weight:
        notes.append(
            f"first sentence is {first} units wide; the collapsed lock screen shows about "
            f"{limits.collapsed_weight} (~{limits.collapsed_weight // 2} Chinese characters). "
            f"Put the point in that opening sentence, not at the end of the body.")
    if alert.body.count("\n") >= 4:
        notes.append(f"body has {alert.body.count(chr(10)) + 1} lines; the collapsed view shows four, "
                     f"the rest needs a long-press.")
    if re.search(r"(\*\*|__|`|^#{1,6}\s|^\s*[-*+]\s)", alert.body, re.MULTILINE):
        notes.append("body carries markdown markers; the banner renders plain text, so they only "
                     "cost width. Put the formatted version in the markdown field instead.")
    if alert.title.endswith(("。", ".", "！", "!", "？", "?")):
        notes.append("title ends with punctuation; iOS titles read better as a label, without a full stop.")
    return tuple(notes)


def validate(alert: Alert, *, limits: Optional[Limits] = None,
             allow_empty: bool = False) -> Alert:
    """Check an alert is sendable; raise :class:`AlertError` when it is not.

    Only two things are refused: an empty body (nothing to show) and a body large
    enough to make the APNs payload invalid. Length beyond the collapsed view is a
    warning, because the notification expands.
    """
    limits = limits or Limits()
    title = str(alert.title or "").strip() or DEFAULT_TITLE
    body = str(alert.body or "").strip()
    if not body and not allow_empty:
        raise AlertError("alert body is empty")
    size = len(body.encode("utf-8"))
    if size > limits.body_bytes:
        raise AlertError(
            f"alert body is {size} bytes; the payload guard is {limits.body_bytes} "
            f"(APNs refuses a payload over 4096 bytes). Send the report through the chat "
            f"channel and keep this to the point plus the numbers that matter.")

    clipped = False
    if display_weight(title) > limits.max_title_weight:
        title, clipped = clip(title, limits.max_title_weight)

    final = Alert(title=title, body=body, source=alert.source, clipped=clipped or alert.clipped,
                  extras=dict(alert.extras or {}))
    return replace(final, warnings=advisories(final, limits=limits))


def from_mapping(data: Mapping[str, Any], *, source: str = "tool",
                 limits: Optional[Limits] = None) -> Alert:
    """Build an alert from a tool call or an alert file."""
    if not isinstance(data, Mapping):
        raise AlertError("alert must be an object with 'title' and 'body'")
    body = data.get("body") or data.get("text") or ""
    title = data.get("title") or DEFAULT_TITLE
    extras = {key: value for key, value in data.items()
              if key not in ("title", "body", "text") and value is not None}
    alert = Alert(title=str(title), body=str(body), source=source, extras=extras)
    return validate(alert, limits=limits)


def load_file(path: str | Path, *, source: str = "cli",
              limits: Optional[Limits] = None, max_age_s: float = 900.0) -> Alert:
    """Read an alert from a JSON file: ``{"title": ..., "body": ..., "level": ...}``.

    ``max_age_s`` guards against a stale file: a script that writes ``created_at``
    and dies before pushing must not have its alert sent an hour later, when the
    phone would show a conclusion that no longer holds. ``0`` disables the check
    (the CLI's ``--allow-stale``).
    """
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AlertError(f"alert file is not valid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise AlertError("alert file must hold a JSON object")
    if max_age_s and data.get("created_at") is not None:
        age = time.time() - _epoch(data["created_at"])
        if age > max_age_s:
            raise AlertError(
                f"alert file is {int(age)}s old (limit {int(max_age_s)}s); it was written for an "
                f"earlier run. Re-send it deliberately with --allow-stale if that is intended.")
    return from_mapping(data, source=source, limits=limits)


def _epoch(value: Any) -> float:
    """Accept an epoch number or an ISO-8601 timestamp."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if text.replace(".", "", 1).isdigit():
        return float(text)
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise AlertError(f"created_at is neither epoch seconds nor ISO-8601: {value!r}") from exc


def dedupe_key(alert: Alert) -> str:
    """A stable id derived from the producer's run id, or ``""``.

    Bark's ``id`` is APNs' collapse id: a re-send with the same value replaces the
    earlier notification instead of stacking a duplicate. Deriving it from the run
    id makes a retry idempotent without suppressing a genuinely new run.
    """
    run_id = str((alert.extras or {}).get("run_id") or "").strip()
    if not run_id:
        return ""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
