"""``bark_notify`` tool: rich one-shot push from inside a session.

The model should reach for this when the user asked to be pinged, or when a
long-running job finished and the user is away — not for every turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:  # package import
    from . import alert as alert_mod
    from . import events
    from .bark_client import build_payload, dedupe_id, push, validate_image_url
    from .settings import TargetError, load_settings
except ImportError:  # direct-path import (tests)
    import alert as alert_mod
    import events
    from bark_client import build_payload, dedupe_id, push, validate_image_url
    from settings import TargetError, load_settings

logger = logging.getLogger(__name__)

_LEVELS = ["active", "timeSensitive", "critical", "passive"]


def _schema(name: str, description: str, properties: dict[str, Any], required=None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


BARK_NOTIFY_SCHEMA = _schema(
    "bark_notify",
    "Send an iOS push notification through Bark. Write the notification itself here: a title that "
    "says what this is and how it ended, and a body whose FIRST sentence is the verdict (the rest is "
    "only numbers that matter). This is not a place to paste a report -- the lock screen shows about "
    "four lines (about 140 display units, roughly 70 Chinese characters) and the rest only "
    "after a long-press. Returns 'queued', which means Bark accepted the payload for APNs, NOT that "
    "the user has seen it. `image` must be a public http(s) URL the phone can reach.",
    {
        "body": {"type": "string",
                 "description": "The notification text: one verdict sentence first, then the numbers "
                                "that matter. Budget ~160 units (CJK counts 2), hard cap 1800 bytes."},
        "title": {"type": "string",
                  "description": "Short label for the notification: what it is plus the outcome, e.g. "
                                 "'Nightly job · 1 item to review'. No trailing punctuation."},
        "device": {"type": "string",
                   "description": "Device alias from config (omit for the default device)."},
        "level": {"type": "string", "enum": _LEVELS,
                  "description": "Delivery urgency. 'timeSensitive' pierces Focus; 'critical' also "
                                 "needs the Bark app's critical-alert permission."},
        "sound": {"type": "string", "description": "Notification sound name."},
        "group": {"type": "string", "description": "Notification group (threads on iOS)."},
        "image": {"type": "string", "description": "Public http(s) image URL shown in the notification."},
        "markdown": {"type": "string",
                     "description": "Optional markdown body. The banner shows it stripped to plain "
                                    "text; the Bark app's history keeps the markdown and renders the "
                                    "styles. When set it replaces `body` in the app history."},
        "url": {"type": "string", "description": "URL opened when the notification is tapped."},
        "ttl": {"type": "integer", "minimum": 0,
                "description": "Seconds the notification stays deliverable; 0 = no expiry."},
    },
    required=["body"],
)


def _extra() -> dict:
    from hermes_cli.config import load_config_readonly

    cfg = load_config_readonly() or {}
    platform = ((cfg.get("platforms") or {}).get("bark") or {})
    return dict(platform.get("extra") or {})


async def handle_bark_notify(args: dict[str, Any], **_kw: Any) -> str:
    settings = load_settings(_extra())
    if not settings.configured:
        return json.dumps({"ok": False,
                           "error": "Bark is not configured: set BARK_TOKEN via `hermes config set`"})
    try:
        alias, key = settings.resolve(args.get("device") or None)
    except TargetError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    image = str(args.get("image") or "").strip()
    if image:
        problem = validate_image_url(image, allow_private=settings.allow_private_image_urls)
        if problem:
            return json.dumps({"ok": False, "error": problem})

    try:
        alert_obj = alert_mod.from_mapping(
            {"title": args.get("title") or settings.title, "body": args.get("body") or ""},
            source="tool", limits=settings.limits)
    except alert_mod.AlertError as exc:
        return json.dumps({"ok": False, "error": str(exc), "error_kind": "bad_format"})

    fields = dict(settings.defaults or {})
    fields.update({k: v for k, v in args.items()
                   if k in ("level", "sound", "group", "image", "url", "ttl", "markdown")
                   and v is not None})
    fields["title"] = alert_obj.title
    fields["body"] = alert_obj.body
    if settings.dedupe and "id" not in fields:
        fields["id"] = alert_mod.dedupe_key(alert_obj) or dedupe_id(alias, alert_obj.body)

    try:
        payload = build_payload(**fields)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    result = await push(settings.server, key, payload, timeout=8.0, attempts=2, proxy=settings.proxy)
    events.record(
        source="tool", ok=not result.get("error"), alias=alias,
        body=alert_obj.body, title=alert_obj.title,
        error=str(result.get("error") or ""), status=result.get("status"),
        latency_ms=result.get("latency_ms"), message_id=str(result.get("message_id") or ""),
        alert_source=alert_obj.source, alert_weight=alert_obj.weight,
    )
    if result.get("error"):
        return json.dumps({"ok": False, "device": alias, "error": result["error"],
                           "error_kind": result.get("error_kind")})
    return json.dumps({"ok": True, "queued": True, "device": alias,
                       "message_id": result.get("message_id") or "",
                       "alert": {"title": alert_obj.title, "body": alert_obj.body,
                                 "weight": alert_obj.weight},
                       "warnings": list(alert_obj.warnings)})


def register_tools(ctx) -> None:
    ctx.register_tool(
        name="bark_notify",
        toolset="bark",
        schema=BARK_NOTIFY_SCHEMA,
        handler=handle_bark_notify,
        is_async=True,
        emoji="🔔",
        description="Push a Bark notification to the user's phone (one-way).",
    )
