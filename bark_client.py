"""Bark push client: payload construction, retry policy, redaction.

Deliberately free of Hermes imports so it can be unit-tested outside the agent
runtime and reused by the CLI, the tool, and the hooks.

Bark API (bark-server):
    POST {server}/push   body: {"device_key": "...", "title": ..., ...}
    GET  {server}/ping   -> {"code": 200, "message": "pong", ...}

A 200 means "the server accepted the payload and handed it to APNs" — NOT that
the device displayed it. Callers must report delivery as *queued*.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import random
import re
import time
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

DEFAULT_SERVER = "https://api.day.app"
DEFAULT_TITLE = "Hermes"
DEFAULT_TIMEOUT = 10.0
DEFAULT_ATTEMPTS = 3
MAX_BODY = 3800

LEVELS = ("active", "timeSensitive", "critical", "passive")

# Keys accepted by ``build_payload`` (Bark's own names, camelCase included).
_PAYLOAD_KEYS = (
    "title", "subtitle", "body", "level", "badge", "sound", "icon", "image",
    "group", "ttl", "url", "autoCopy", "copy", "isArchive", "id", "markdown",
)

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Values for the ``proxy`` setting that mean "never touch a proxy".
_DIRECT = frozenset({"direct", "off", "none", "no", "noproxy", "no-proxy"})

# Transport failures raised before the request left the socket: safe to retry.
# Matched by class name to stay independent of the httpx import.
_NEVER_LEFT = frozenset({
    "ConnectError", "ConnectTimeout", "PoolTimeout", "ConnectionRefusedError",
    "ClientConnectorError", "ClientConnectorDNSError", "ClientProxyConnectionError",
    "ConnectionTimeoutError",
})


def looks_like_key(token: str) -> bool:
    """True when *token* plausibly is a Bark device key (not an alias)."""
    return bool(_KEY_RE.match((token or "").strip()))


def _proxy_kwargs(proxy: str = "") -> dict:
    """httpx client kwargs for the ``proxy`` setting.

    ``""``       → inherit ``HTTPS_PROXY``/``HTTP_PROXY`` (httpx default);
    ``"direct"`` → ignore the environment entirely (also: off/none/no/noproxy);
    anything else is used as the proxy URL.
    """
    value = (proxy or "").strip()
    if not value:
        return {}
    if value.lower() in _DIRECT:
        return {"trust_env": False}
    return {"proxy": value, "trust_env": False}


def _loopback_proxy() -> str:
    """The env proxy, but only when it points at this machine.

    A stale local MITM proxy (dead port left in ``HTTPS_PROXY``) is common enough
    that Bark must not report the server down because of it.
    """
    import os

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value and any(host in value for host in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")):
            return value
    return ""


def redact(text: str, *secrets: str) -> str:
    """Strip device keys from any text headed for a log, error, or transcript."""
    out = str(text)
    for secret in secrets:
        secret = (secret or "").strip()
        if len(secret) >= 8:
            out = out.replace(secret, "***")
    return out


def dedupe_id(chat_id: str, body: str, *, bucket_seconds: int = 5) -> str:
    """Stable id for a time bucket: a retry replaces instead of stacking."""
    bucket = int(time.time()) // max(1, bucket_seconds)
    raw = f"{chat_id}\x00{body}\x00{bucket}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def validate_image_url(url: str, *, allow_private: bool = False) -> Optional[str]:
    """None when *url* is usable as a Bark ``image``/``icon``; else the reason.

    Bark's SERVER fetches the URL. On a self-hosted server inside a LAN that is
    an SSRF pivot, so private/loopback/link-local targets are refused unless the
    operator explicitly opts in.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        return "image must be an http(s) URL"
    if not parts.hostname:
        return "image URL has no host"
    if parts.username or parts.password:
        return "image URL must not carry credentials"
    if allow_private:
        return None
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return f"refusing private host {host!r} (set allow_private_image_urls: true to override)"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return f"refusing private address {host} (set allow_private_image_urls: true to override)"
    return None


def _as_int(name: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {value!r}")


_PAYLOAD_ALIASES = {"auto_copy": "autoCopy", "is_archive": "isArchive", "dedupe_id": "id"}


def build_payload(*, defaults: Optional[Mapping[str, Any]] = None, **fields: Any) -> dict:
    """Bark request payload without ``device_key``.

    ``defaults`` (config ``platforms.bark.extra.defaults``) supplies values for
    keys *fields* leaves as None. Unknown keys in either mapping are ignored so a
    typo in config degrades to "not set" instead of a TypeError at send time.
    """
    fields = {_PAYLOAD_ALIASES.get(k, k): v for k, v in fields.items()}
    merged: dict[str, Any] = {}
    for source in (defaults or {}), fields:
        for key, value in source.items():
            if key in _PAYLOAD_KEYS and value is not None and value != "":
                merged[key] = value

    if "level" in merged and merged["level"] not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}; got {merged['level']!r}")
    if "ttl" in merged:
        ttl = _as_int("ttl", merged["ttl"])
        if ttl < 0:
            raise ValueError("ttl must be >= 0 seconds")
        merged["ttl"] = ttl
    if "badge" in merged:
        merged["badge"] = _as_int("badge", merged["badge"])
    for key in ("autoCopy", "isArchive"):
        if key in merged:
            merged[key] = 1 if merged[key] in (True, 1, "1", "true", "True", "yes") else 0
    if "id" in merged:
        merged["id"] = str(merged["id"])
    return merged


def explain_probe(probe: dict) -> str:
    """One-line diagnosis for a failed ``/ping`` probe; ``""`` when it succeeded.

    Doctor's whole job is telling the operator *which* of the five common
    misconfigurations they hit, so the mapping lives here (pure, testable)
    instead of in CLI print statements.
    """
    if probe.get("ok"):
        return ""
    err = str(probe.get("error") or "")
    status = probe.get("status")
    if status in (401, 403):
        return "server answered 401/403 — a proxy or the wrong URL sits in front of Bark (check BARK_SERVER)"
    if status == 404:
        return "/ping is 404 — not a Bark server, or the base URL carries a path suffix"
    if status and 500 <= int(status) < 600:
        return "bark-server itself returned 5xx — check its logs"
    if status == 200:
        return "200 but not a Bark pong — that URL is not a Bark server"
    exc_name = err.split(":", 1)[0]
    if "Proxy" in exc_name:
        return "proxy refused the request — check HTTPS_PROXY and the local proxy"
    if "ConnectError" in exc_name:
        return "cannot connect — DNS failure or nothing listening on that host:port"
    if "Timeout" in exc_name:
        return "timed out — host reachable but silent (firewall, or a slow proxy)"
    if "SSL" in exc_name:
        return "TLS handshake failed — self-signed certificate, or an HTTPS-intercepting proxy"
    return err or "unknown failure"


def classify_status(status: int) -> str:
    """Platform-neutral error kind for a non-2xx HTTP status."""
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "forbidden"
    if status in (400, 413, 422):
        return "bad_format"
    if 500 <= status < 600:
        return "server"
    return "transient"


def _never_left(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _NEVER_LEFT:
        return True
    cause = getattr(exc, "__cause__", None)
    return bool(cause is not None and type(cause).__name__ in _NEVER_LEFT)


def _backoff(attempt: int) -> float:
    return min(0.4 * (3 ** attempt), 3.0) * (0.8 + random.random() * 0.4)


def _retry_after(resp: Any) -> Optional[float]:
    try:
        value = float(resp.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    return min(value, 5.0) if value > 0 else None


async def _push_http(
    httpx: Any, url: str, body: Mapping[str, Any], *,
    timeout: float, attempts: int, client_kwargs: dict, device_key: str,
) -> dict:
    """One push attempt sequence with a fixed transport configuration."""
    last: dict = {"error": "bark push not attempted", "error_kind": "transient",
                  "retryable": False, "never_left": True}
    started = time.monotonic()

    def _timing(extra: dict, status: Optional[int] = None) -> dict:
        extra["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        extra["status"] = status
        return extra

    async with httpx.AsyncClient(timeout=timeout, **client_kwargs) as client:
        for attempt in range(max(1, attempts)):
            try:
                resp = await client.post(url, json=body)
            except Exception as exc:  # noqa: BLE001 — every transport error becomes a result
                retryable = _never_left(exc)
                last = _timing({
                    "error": redact(f"Bark request failed: {type(exc).__name__}: {exc}", device_key),
                    "error_kind": "transient", "retryable": retryable,
                    "never_left": retryable,
                })
                if attempt + 1 < attempts and retryable:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                return last

            status = resp.status_code
            if status == 429 or 500 <= status < 600:
                kind = classify_status(status)
                last = _timing({
                    "error": redact(f"Bark HTTP {status}: {resp.text[:200]}", device_key),
                    "error_kind": kind, "retryable": True,
                    # The server answered, so the request provably left this host.
                    # Retryable is not the same claim as never-left.
                    "never_left": False,
                }, status)
                if attempt + 1 < attempts:
                    wait = _retry_after(resp)
                    await asyncio.sleep(wait if wait is not None else _backoff(attempt))
                    continue
                return last

            if status >= 300:
                return _timing({
                    "error": redact(f"Bark HTTP {status}: {resp.text[:200]}", device_key),
                    "error_kind": classify_status(status), "retryable": False,
                    "never_left": False,
                }, status)

            try:
                data = resp.json()
            except Exception:  # noqa: BLE001 — a proxy/captive portal answering 200 with HTML
                return _timing({"error": redact(f"Bark returned non-JSON: {resp.text[:200]}", device_key),
                                "error_kind": "bad_format", "retryable": False,
                                "never_left": False}, status)

            code = data.get("code", 200) if isinstance(data, dict) else 200
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                code_int = 200
            if code_int != 200:
                return _timing({"error": redact(f"Bark rejected: {str(data.get('message') or data)[:200]}", device_key),
                                "error_kind": "bad_format" if 400 <= code_int < 500 else "server",
                                "retryable": False, "never_left": False}, status)

            message_id = ""
            if isinstance(data, dict):
                message_id = str(data.get("id") or data.get("timestamp") or "")
            return _timing({"success": True, "message_id": message_id, "queued": True}, status)

    return last


async def push(
    server: str, device_key: str, payload: Mapping[str, Any], *,
    timeout: float = DEFAULT_TIMEOUT, attempts: int = DEFAULT_ATTEMPTS, proxy: str = "",
) -> dict:
    """POST one notification. Returns a result dict, never raises.

    ``{"success": True, "message_id": ..., "queued": True}``
    ``{"error": str, "error_kind": ..., "retryable": bool, "never_left": bool}``

    ``never_left`` is the strict provenance claim: True only when the request
    provably never reached the server (the connection was never obtained). An
    HTTP status — 429 and 5xx included — means the server answered, so
    ``never_left`` is False even though ``retryable`` may be True: retryable is
    our own retry decision, never-left is what core may replay on.

    ``proxy`` follows :func:`_proxy_kwargs`. When the environment proxy points at
    this machine and every attempt failed, one direct retry is made: a stale local
    MITM proxy (the classic "leftover HTTPS_PROXY" case) must not make Bark look
    unreachable when the server is fine.
    """
    try:
        import httpx
    except ImportError:
        return {"error": "httpx is not installed", "error_kind": "transient",
                "retryable": False, "never_left": True}

    if not device_key:
        return {"error": "no device key resolved", "error_kind": "forbidden",
                "retryable": False, "never_left": True}

    url = (server or DEFAULT_SERVER).rstrip("/") + "/push"
    body = {"device_key": device_key, **dict(payload)}

    result = await _push_http(httpx, url, body, timeout=timeout, attempts=attempts,
                              client_kwargs=_proxy_kwargs(proxy), device_key=device_key)
    # Only a transport failure that never produced an HTTP status can be the proxy's
    # fault; a 401/500 is the server's answer and must stand.
    if (not result.get("success") and not (proxy or "").strip() and _loopback_proxy()
            and result.get("status") is None and result.get("error_kind") == "transient"):
        direct = await _push_http(httpx, url, body, timeout=timeout, attempts=attempts,
                                  client_kwargs={"trust_env": False}, device_key=device_key)
        if direct.get("success"):
            direct["proxy_fallback"] = True
            return direct
    return result


async def ping(server: str, *, timeout: float = 5.0, proxy: str = "") -> dict:
    """``GET /ping`` for ``hermes bark doctor`` and the settings page.

    Same proxy rules (and the same one-shot direct retry) as :func:`push`.
    """
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx is not installed"}

    url = (server or DEFAULT_SERVER).rstrip("/") + "/ping"

    async def _get(client_kwargs: dict) -> dict:
        async with httpx.AsyncClient(timeout=timeout, **client_kwargs) as client:
            resp = await client.get(url)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        ok = resp.status_code == 200 and str(data.get("message", "")).lower().startswith("pong")
        return {"ok": ok, "status": resp.status_code, "message": data.get("message"),
                "version": data.get("version"), "server": server}

    try:
        return await _get(_proxy_kwargs(proxy))
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        if not (proxy or "").strip() and _loopback_proxy():
            try:
                direct = await _get({"trust_env": False})
                direct["proxy_fallback"] = True
                return direct
            except Exception as exc2:  # noqa: BLE001
                error = f"{type(exc2).__name__}: {exc2}"
        return {"ok": False, "error": error, "server": server}
