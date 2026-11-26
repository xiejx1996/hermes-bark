"""Configuration + secret resolution for the Bark plugin.

Two configuration surfaces, one merged view:

- ``plugins.entries.hermes-bark.settings`` — the knobs declared in ``plugin.yaml``
  ``config_schema``. Written by Settings → Plugins (``plugins.manage``) and by
  this plugin's own dashboard page, always through Hermes' atomic
  ``save_plugin_settings`` writer (validated against the schema, refuses
  secrets). This is the surface the desktop page edits.
- ``platforms.bark.extra`` — the platform-shaped config (``devices``, nested
  ``notify`` rules, ``defaults``). Still honoured so an existing config or a
  platform-only install keeps working; settings win over it key by key.

Secrets never live in either: they come from ``BARK_TOKEN`` / ``BARK_KEY_<ALIAS>``,
and a literal key typed into ``config.yaml`` is refused (reported as
``misplaced_keys``, never used). Hermes owns credential routing and the plugin
never parses ``.env`` itself (catalog rule 11): a read goes through the *installed
profile* secret scope when one is bound — including keys saved moments ago,
because a credential write publishes into that same scope — and only a process
with no scope at all (bare host, default-profile startup, unit test) reads
``os.environ``. A scoped miss is fail-closed: another profile's key is never
borrowed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

try:  # package import (plugin loader)
    from .bark_client import DEFAULT_SERVER, DEFAULT_TITLE, MAX_BODY, looks_like_key
    from .alert import Limits as AlertLimits
except ImportError:  # direct-path import (tests)
    from bark_client import DEFAULT_SERVER, DEFAULT_TITLE, MAX_BODY, looks_like_key
    from alert import Limits as AlertLimits

PLUGIN_ID = "hermes-bark"

#: Every knob the desktop page and the native Settings → Plugins form can write.
#: Keep in sync with ``config_schema`` in ``plugin.yaml`` (types and defaults).
SETTING_DEFAULTS: dict[str, Any] = {
    "server": DEFAULT_SERVER,
    "proxy": "",
    "title": DEFAULT_TITLE,
    "default_device": "",
    "level": "timeSensitive",
    "group": "Hermes",
    "sound": "",
    "icon": "",
    "ttl": 0,
    "notify_turn_end": False,
    "notify_turn_end_min_duration_s": 90,
    "notify_turn_end_statuses": "failed,interrupted",
    "notify_subagent_stop": False,
    "notify_subagent_stop_min_duration_ms": 30000,
    "notify_quiet_hours": "",
    "dedupe": True,
    "allow_private_image_urls": False,
    "alert_max_title_weight": 36,
    "alert_collapsed_weight": 140,
    "missing_alert": "passthrough",
}

SETTING_KEYS = tuple(SETTING_DEFAULTS)


class TargetError(ValueError):
    """Unresolvable delivery target; the message is shown to the model verbatim."""


# -- secrets -----------------------------------------------------------------


def _profile_home() -> Optional[Path]:
    """The Hermes home that owns this plugin copy (``…/plugins/<id>/settings.py``)."""
    here = Path(__file__).resolve()
    if len(here.parents) >= 3 and here.parents[1].name == "plugins":
        return here.parents[2]
    return None


def _active_scope() -> Optional[Mapping[str, str]]:
    """The bound profile secret scope, or ``None`` when this process has none."""
    try:
        from agent.secret_scope import current_secret_scope
    except Exception:  # noqa: BLE001 — no Hermes runtime (bare host, unit tests)
        return None
    try:
        scope = current_secret_scope()
    except Exception:  # noqa: BLE001
        return None
    return scope if isinstance(scope, Mapping) else None


def secret(name: str, default: str = "") -> str:
    """Profile-scoped credential read — Hermes routes it, the plugin never reads files.

    An installed profile secret scope is authoritative (core's contract:
    ``agent/secret_scope.get_secret`` returns the active profile's value and a
    scoped miss is fail-closed, ``UnscopedSecretError`` included). Reading
    ``os.environ`` on a scoped miss would hand a routed profile the launch
    profile's key, so that fallback is only allowed when this process has no
    scope at all — a bare host, the default profile's unscoped startup, or a
    unit test. ``gateway.platforms._shared.get_scoped_secret`` is the second
    rung because it encodes that same rule and adds the documented
    unscoped-startup scope build.
    """
    scope = _active_scope()
    if scope is not None:
        return str(scope.get(name) or "").strip() or default
    value: Optional[str] = None
    try:
        from gateway.platforms._shared import get_scoped_secret

        value = get_scoped_secret(name, None, external_fallback=True)
    except Exception:  # noqa: BLE001
        value = None
    if value is None:
        value = os.environ.get(name)
    return str(value or "").strip() or default


def secret_names(prefix: str = "BARK_") -> set[str]:
    """Credential names this profile can see — scope-only when one is bound."""
    scope = _active_scope()
    if scope is not None:
        return {str(key) for key in scope if str(key).startswith(prefix)}
    return {key for key in os.environ if key.startswith(prefix)}


def secret_source(name: str) -> str:
    """``"scope"`` / ``"env"`` / ``""`` for display only — never the value.

    ``"env"`` covers everything a process without a bound scope resolves through
    (process environment, or the profile's own secret mapping behind
    ``get_scoped_secret``), so the page never says "not set" while ``secret()``
    happily returns a value.
    """
    scope = _active_scope()
    if scope is not None:
        return "scope" if str(scope.get(name) or "").strip() else ""
    return "env" if secret(name) else ""


def alias_env(alias: str) -> str:
    """``iphone`` -> ``BARK_KEY_IPHONE``."""
    return "BARK_KEY_" + re.sub(r"[^A-Z0-9]+", "_", str(alias).upper()).strip("_")


def alias_from_env(name: str) -> str:
    """``BARK_KEY_IPAD_PRO`` -> ``ipad-pro`` (round-trips :func:`alias_env`)."""
    return name[len("BARK_KEY_"):].lower().replace("_", "-")


def env_aliases() -> dict[str, str]:
    """Aliases that exist as ``BARK_KEY_<ALIAS>`` in this profile's credential scope."""
    out: dict[str, str] = {}
    for name in sorted(secret_names("BARK_KEY_")):
        alias = alias_from_env(name)
        if alias and alias not in out:
            out[alias] = secret(name)
    return {alias: key for alias, key in out.items() if key}


# -- settings surfaces -------------------------------------------------------


def plugin_settings() -> dict[str, Any]:
    """``plugins.entries.hermes-bark.settings`` (empty on any failure)."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins import _plugin_settings_entry

        entry = _plugin_settings_entry(load_config_readonly() or {}, PLUGIN_ID) or {}
        values = entry.get("settings") or {}
        return {str(k): v for k, v in values.items()} if isinstance(values, Mapping) else {}
    except Exception:  # noqa: BLE001 — settings must never break a send
        return {}


def platform_extra() -> dict:
    """``platforms.bark.extra`` from the merged config (read-only; no deepcopy)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:  # noqa: BLE001
        return {}
    platform = (cfg.get("platforms") or {}).get("bark") or {}
    return dict(platform.get("extra") or {})


def _device_key(alias: str, spec: Any) -> tuple[str, bool]:
    """Resolve one ``devices`` entry -> ``(key, misplaced)``.

    Keys are only ever read from a credential — ``BARK_KEY_<ALIAS>`` by default,
    or the name in an explicit ``key_env``. A literal key typed into
    ``config.yaml`` (as the entry's value, or under ``key:``) is refused and
    reported as ``misplaced`` so ``doctor`` and the settings page can tell the
    operator where it belongs; it is never used.
    """
    if isinstance(spec, Mapping):
        env_name = str(spec.get("key_env") or alias_env(alias)).strip()
        return secret(env_name), bool(str(spec.get("key") or "").strip())
    if isinstance(spec, str) and spec.strip():
        value = spec.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            return secret(value), False
        return secret(alias_env(alias)), True  # literal key in YAML: refused
    return secret(alias_env(alias)), False


def _statuses(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


@dataclass(frozen=True)
class Settings:
    server: str = DEFAULT_SERVER
    proxy: str = ""                            # "" = env, "direct" = ignore env, else proxy URL
    title: str = DEFAULT_TITLE
    default_device: str = ""
    devices: dict = field(default_factory=dict)   # alias -> device key
    defaults: dict = field(default_factory=dict)  # Bark payload defaults
    notify: dict = field(default_factory=dict)    # hook rules
    allow_private_image_urls: bool = False
    dedupe: bool = False
    misplaced_keys: tuple = ()                    # aliases whose YAML held a literal key (refused)
    values: dict = field(default_factory=dict)    # merged scalar knobs
    provenance: dict = field(default_factory=dict)  # key -> settings|extra|default

    @property
    def configured(self) -> bool:
        return bool(self.devices)

    @property
    def limits(self) -> AlertLimits:
        """Lock-screen budgets from the settings surface (tolerant of bad values)."""
        def _int(key: str, default: int) -> int:
            try:
                value = int(self.values.get(key) or default)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return AlertLimits(
            max_title_weight=_int("alert_max_title_weight", 36),
            collapsed_weight=_int("alert_collapsed_weight", 140),
        )

    def resolve(self, target: Optional[str]) -> tuple[str, str]:
        """``("iphone", "<key>")`` for a target ref; raises :class:`TargetError`."""
        token = (target or "").strip()
        if not token or token in ("home", "default"):
            token = self.default_device
        if not token:
            raise TargetError("no Bark device configured (set BARK_TOKEN or platforms.bark.extra.devices)")
        key = self.devices.get(token)
        if key:
            return token, key
        if looks_like_key(token):
            return f"key:{token[:4]}…", token
        known = ", ".join(sorted(self.devices)) or "(none)"
        raise TargetError(f"unknown Bark device {token!r}; known: {known}")

    def rule(self, name: str) -> dict:
        value = (self.notify or {}).get(name)
        return dict(value) if isinstance(value, Mapping) else {}

    def redacted(self) -> dict:
        """Operator-facing view: never leaks keys."""
        return {
            "server": self.server,
            "title": self.title,
            "default_device": self.default_device,
            "devices": {alias: f"***{key[-4:]}" if len(key) > 4 else "***" for alias, key in self.devices.items()},
            "dedupe": self.dedupe,
            "allow_private_image_urls": self.allow_private_image_urls,
            "notify": dict(self.notify or {}),
            "misplaced_keys": list(self.misplaced_keys),
        }


def _build_notify(values: dict[str, Any], provenance: dict[str, str], extra: dict) -> dict:
    """Flat settings knobs → the nested rule shape the hooks read.

    ``platforms.bark.extra.notify`` is the base so hand-written nested rules keep
    working; a knob explicitly saved through the settings surface overrides them.
    """
    notify = dict(extra.get("notify") or {}) if isinstance(extra.get("notify"), Mapping) else {}

    def _set(rule_name: str, key: str, value: Any, *, setting: str) -> None:
        if provenance.get(setting) not in ("settings", "default"):
            return  # extra's nested rule wins unless the knob was saved here
        if provenance.get(setting) == "default" and rule_name in notify:
            return  # an existing nested rule beats a schema default
        rule = notify.get(rule_name)
        rule = dict(rule) if isinstance(rule, Mapping) else {}
        rule[key] = value
        notify[rule_name] = rule

    turn_end = notify.get("turn_end")
    turn_end = dict(turn_end) if isinstance(turn_end, Mapping) else {}
    _set("turn_end", "enabled", bool(values.get("notify_turn_end")), setting="notify_turn_end")
    _set("turn_end", "min_duration_s", float(values.get("notify_turn_end_min_duration_s") or 0),
         setting="notify_turn_end_min_duration_s")
    statuses = _statuses(values.get("notify_turn_end_statuses"))
    if statuses:
        _set("turn_end", "statuses", statuses, setting="notify_turn_end_statuses")
    if turn_end:
        notify["turn_end"] = turn_end

    subagent = notify.get("subagent_stop")
    subagent = dict(subagent) if isinstance(subagent, Mapping) else {}
    _set("subagent_stop", "enabled", bool(values.get("notify_subagent_stop")), setting="notify_subagent_stop")
    _set("subagent_stop", "min_duration_ms", float(values.get("notify_subagent_stop_min_duration_ms") or 0),
         setting="notify_subagent_stop_min_duration_ms")
    if subagent:
        notify["subagent_stop"] = subagent

    quiet = values.get("notify_quiet_hours") or ""
    if str(quiet).strip():
        notify["quiet_hours"] = [part.strip() for part in str(quiet).split(",") if part.strip()]
    elif provenance.get("notify_quiet_hours") == "settings":
        notify.pop("quiet_hours", None)
    return notify


def load_settings(extra: Optional[Mapping[str, Any]] = None) -> Settings:
    """Merged view: ``plugins.entries.hermes-bark.settings`` over ``platforms.bark.extra``."""
    extra = dict(platform_extra() if extra is None else extra)
    stored = plugin_settings()

    values: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for key, default in SETTING_DEFAULTS.items():
        if key in stored and stored[key] is not None:
            values[key] = stored[key]
            provenance[key] = "settings"
        elif key in extra and extra[key] is not None:
            values[key] = extra[key]
            provenance[key] = "extra"
        else:
            values[key] = default
            provenance[key] = "default"

    server = str(values["server"] or "").strip()
    if provenance.get("server") == "default":
        # The schema default is the public server; an env-only install
        # (BARK_SERVER, no config.yaml entry) must still win over it.
        server = secret("BARK_SERVER") or server
    server = (server or DEFAULT_SERVER).rstrip("/")

    devices: dict[str, str] = {}
    misplaced: list[str] = []
    for alias, spec in (extra.get("devices") or {}).items():
        alias = str(alias).strip()
        if not alias:
            continue
        key, refused = _device_key(alias, spec)
        if refused:
            misplaced.append(alias)
        if key:
            devices[alias] = key

    # ``BARK_TOKEN`` is the single-token setup the docs lead with, so it is read
    # unconditionally: an extra ``BARK_KEY_<ALIAS>`` must never push it out and
    # leave the ``default`` device missing.
    token = secret("BARK_TOKEN")
    if token:
        devices.setdefault("default", token)

    for alias, key in env_aliases().items():  # env-only aliases, never overwrite explicit entries
        devices.setdefault(alias, key)

    default_device = str(values.get("default_device") or "").strip()
    if not default_device:
        default_device = "default" if "default" in devices else next(iter(devices), "")

    defaults: dict[str, Any] = dict(extra.get("defaults") or {})
    # Precedence: an explicit plugin setting > platforms.bark.extra.defaults > schema
    # default. ``setdefault`` is what encodes the middle line: a hand-written extra
    # keeps its value unless the user saved that knob from the settings surface.
    for key in ("level", "group", "sound", "icon"):
        value = values.get(key)
        if provenance.get(key) == "default":
            if value not in (None, ""):
                defaults.setdefault(key, value)
        elif value in (None, ""):
            defaults.pop(key, None)
        else:
            defaults[key] = value
    try:
        ttl = int(values.get("ttl") or 0)
    except (TypeError, ValueError):
        ttl = 0
    if provenance.get("ttl") == "default":
        if ttl > 0:
            defaults.setdefault("ttl", ttl)
    elif ttl > 0:
        defaults["ttl"] = ttl
    else:
        defaults.pop("ttl", None)
    title = str(values.get("title") or DEFAULT_TITLE)

    return Settings(
        server=server,
        proxy=str(values.get("proxy") or ""),
        title=title,
        default_device=default_device,
        devices=devices,
        defaults=defaults,
        notify=_build_notify(values, provenance, extra),
        allow_private_image_urls=bool(values.get("allow_private_image_urls")),
        dedupe=bool(values.get("dedupe")),
        misplaced_keys=tuple(misplaced),
        values=values,
        provenance=provenance,
    )


def load_from_config() -> Settings:
    return load_settings(platform_extra())
