"""Unit + in-process integration tests for the Bark plugin (stdlib unittest).

Run:  <hermes venv>/bin/python tests/test_bark.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path

# Hermetic: settings.secret() reads Hermes' credential scope and, with no scope
# bound (the case here), the process environment — so a test run must never see
# the developer's real HOME or a real BARK_* key. Isolate before importing.
_SANDBOX = tempfile.mkdtemp(prefix="bark-test-home-")
os.environ["HOME"] = _SANDBOX
os.environ["HERMES_HOME"] = str(Path(_SANDBOX) / "hermes")
for _name in [key for key in os.environ if key.startswith("BARK_")]:
    os.environ.pop(_name, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import bark_client as bc  # noqa: E402
import settings as settings_mod  # noqa: E402
from settings import Settings, TargetError, load_settings  # noqa: E402
from fake_bark import FakeBark  # noqa: E402

DEVICE = "AbCdEfGhIjKlMnOpQrStUv"


class PayloadTests(unittest.TestCase):
    def test_minimal(self):
        self.assertEqual(bc.build_payload(body="hi"), {"body": "hi"})

    def test_defaults_then_override(self):
        payload = bc.build_payload(defaults={"level": "active", "sound": "bell", "title": "T"},
                                   body="hi", title="Override")
        self.assertEqual(payload["level"], "active")
        self.assertEqual(payload["sound"], "bell")
        self.assertEqual(payload["title"], "Override")

    def test_rejects_bad_level(self):
        with self.assertRaises(ValueError):
            bc.build_payload(body="x", level="urgent")

    def test_ttl_and_badge_coerced(self):
        payload = bc.build_payload(body="x", ttl="60", badge="3")
        self.assertEqual(payload["ttl"], 60)
        self.assertEqual(payload["badge"], 3)
        with self.assertRaises(ValueError):
            bc.build_payload(body="x", ttl=-1)

    def test_bools_and_id(self):
        payload = bc.build_payload(body="x", auto_copy=True, is_archive="0", dedupe_id=7)
        self.assertEqual(payload["autoCopy"], 1)
        self.assertEqual(payload["isArchive"], 0)
        self.assertEqual(payload["id"], "7")

    def test_markdown_survives_the_whitelist(self):
        payload = bc.build_payload(body="plain", markdown="**bold**")
        self.assertEqual(payload["markdown"], "**bold**")

    def test_unknown_keys_ignored(self):
        self.assertNotIn("nope", bc.build_payload(body="x", nope="y"))

    def test_empty_values_dropped(self):
        self.assertEqual(bc.build_payload(body="x", title="", sound=None), {"body": "x"})


class TargetTests(unittest.TestCase):
    def settings(self):
        return Settings(devices={"iphone": "k" * 22, "ipad": "p" * 22}, default_device="iphone")

    def test_default(self):
        self.assertEqual(self.settings().resolve("")[0], "iphone")
        self.assertEqual(self.settings().resolve("home")[0], "iphone")

    def test_alias(self):
        alias, key = self.settings().resolve("ipad")
        self.assertEqual((alias, key), ("ipad", "p" * 22))

    def test_raw_key(self):
        alias, key = self.settings().resolve(DEVICE)
        self.assertEqual(key, DEVICE)
        self.assertTrue(alias.startswith("key:"))

    def test_unknown_alias_lists_choices(self):
        with self.assertRaises(TargetError) as ctx:
            self.settings().resolve("watch")
        self.assertIn("iphone", str(ctx.exception))
        self.assertIn("ipad", str(ctx.exception))

    def test_no_devices(self):
        with self.assertRaises(TargetError):
            Settings().resolve("")

    def test_literal_key_in_config_is_refused(self):
        # A key typed into config.yaml must never be used as a credential; it is
        # reported so the page/CLI can point at it, and the alias keeps no device.
        loaded = load_settings({"devices": {"phone": DEVICE, "tab": {"key": "x" * 22}}})
        self.assertEqual(loaded.devices, {})
        self.assertEqual(sorted(loaded.misplaced_keys), ["phone", "tab"])
        self.assertNotIn(DEVICE, json.dumps(loaded.redacted()))

    def test_device_from_named_key_env(self):
        os.environ["BARK_KEY_TAB"] = "T" * 22
        try:
            loaded = load_settings({"devices": {"tab": {"key_env": "BARK_KEY_TAB"}}})
            self.assertEqual(loaded.devices["tab"], "T" * 22)
            self.assertEqual(loaded.default_device, "tab")
        finally:
            os.environ.pop("BARK_KEY_TAB", None)

    def test_token_survives_alias_env(self):
        # BARK_TOKEN + BARK_KEY_* used to leave `default` uncreated (setdefault
        # ordering bug), so `default_device` silently jumped to the alias.
        os.environ["BARK_TOKEN"] = DEVICE
        os.environ["BARK_KEY_IPHONE"] = "I" * 22
        try:
            loaded = load_settings({})
            self.assertEqual(loaded.devices["default"], DEVICE)
            self.assertEqual(loaded.devices["iphone"], "I" * 22)
            self.assertEqual(loaded.default_device, "default")
        finally:
            os.environ.pop("BARK_TOKEN", None)
            os.environ.pop("BARK_KEY_IPHONE", None)

    def test_env_name_spec_is_not_treated_as_key(self):
        # No Hermes runtime -> unresolved env var -> device dropped, not a bogus key.
        loaded = load_settings({"devices": {"phone": "BARK_KEY_PHONE"}})
        self.assertEqual(loaded.devices, {})


class ScopedSecretTests(unittest.TestCase):
    """F1: a bound profile secret scope is authoritative — os.environ never wins."""

    def _bind(self, scope: dict) -> None:
        pkg = types.ModuleType("agent")
        mod = types.ModuleType("agent.secret_scope")
        mod.current_secret_scope = lambda: dict(scope)  # type: ignore[attr-defined]
        mod.get_secret = lambda name, default=None: scope.get(name, default)  # type: ignore[attr-defined]
        pkg.secret_scope = mod  # type: ignore[attr-defined]
        self._saved = {name: sys.modules.get(name) for name in ("agent", "agent.secret_scope")}
        sys.modules["agent"] = pkg
        sys.modules["agent.secret_scope"] = mod

    def tearDown(self):
        for name, module in getattr(self, "_saved", {}).items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_scope_wins_over_environment(self):
        os.environ["BARK_TOKEN"] = "E" * 22
        self.addCleanup(os.environ.pop, "BARK_TOKEN", None)
        self._bind({"BARK_TOKEN": "S" * 22})
        self.assertEqual(settings_mod.secret("BARK_TOKEN"), "S" * 22)
        self.assertEqual(settings_mod.secret_source("BARK_TOKEN"), "scope")
        self.assertEqual(load_settings({}).devices["default"], "S" * 22)

    def test_scoped_miss_is_fail_closed(self):
        # The launch profile's key sitting in os.environ must not be borrowed.
        os.environ["BARK_TOKEN"] = "E" * 22
        os.environ["BARK_KEY_IPHONE"] = "I" * 22
        self.addCleanup(os.environ.pop, "BARK_TOKEN", None)
        self.addCleanup(os.environ.pop, "BARK_KEY_IPHONE", None)
        self._bind({"BARK_SERVER": "https://example.test"})
        self.assertEqual(settings_mod.secret("BARK_TOKEN"), "")
        self.assertEqual(settings_mod.secret_source("BARK_TOKEN"), "")
        self.assertEqual(settings_mod.secret_names("BARK_"), {"BARK_SERVER"})
        self.assertEqual(settings_mod.env_aliases(), {})
        self.assertEqual(load_settings({}).devices, {})


class SafetyTests(unittest.TestCase):
    def test_redact(self):
        text = f"POST https://api.day.app/{DEVICE} failed for {DEVICE}"
        self.assertNotIn(DEVICE, bc.redact(text, DEVICE))

    def test_image_url(self):
        self.assertIsNone(bc.validate_image_url("https://example.com/a.png"))
        self.assertIsNone(bc.validate_image_url("http://example.com/a.png"))
        self.assertIn("http", bc.validate_image_url("file:///etc/passwd"))
        self.assertIn("private", bc.validate_image_url("http://127.0.0.1/a.png"))
        self.assertIn("private", bc.validate_image_url("http://192.168.1.10/a.png"))
        self.assertIn("private", bc.validate_image_url("http://nas.local/a.png"))
        self.assertIsNone(bc.validate_image_url("http://127.0.0.1/a.png", allow_private=True))

    def test_dedupe_id_stable_within_bucket(self):
        self.assertEqual(bc.dedupe_id("iphone", "hello"), bc.dedupe_id("iphone", "hello"))
        self.assertNotEqual(bc.dedupe_id("iphone", "hello"), bc.dedupe_id("ipad", "hello"))

    def test_quiet_hours(self):
        from notify import in_quiet_hours

        night = datetime(2026, 11, 22, 23, 45)
        morning = datetime(2026, 11, 22, 7, 30)
        noon = datetime(2026, 11, 22, 12, 0)
        self.assertTrue(in_quiet_hours("23:30-08:00", night))
        self.assertTrue(in_quiet_hours("23:30-08:00", morning))
        self.assertFalse(in_quiet_hours("23:30-08:00", noon))
        self.assertTrue(in_quiet_hours(["12:00-13:00"], noon))


class PushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeBark().start()
        self.addCleanup(self.fake.stop)

    async def test_success_payload_shape(self):
        result = await bc.push(self.fake.server, DEVICE, {"body": "hello", "title": "T"})
        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        sent = self.fake.requests[-1]["payload"]
        self.assertEqual(sent["device_key"], DEVICE)
        self.assertEqual(sent["body"], "hello")
        self.assertTrue(result["message_id"].startswith("fake-"))

    async def test_retry_on_500_then_success(self):
        self.fake.fail_queue.append((500, {}))
        result = await bc.push(self.fake.server, DEVICE, {"body": "x"}, attempts=2)
        self.assertTrue(result["success"])
        self.assertEqual(len(self.fake.requests), 2)

    async def test_4xx_is_not_retried(self):
        self.fake.fail_queue.append((401, {}))
        result = await bc.push(self.fake.server, DEVICE, {"body": "x"}, attempts=3)
        self.assertIn("error", result)
        self.assertEqual(result["error_kind"], "forbidden")
        self.assertEqual(len(self.fake.requests), 1)
        # Answered by the server: replaying it could double-buzz the phone.
        self.assertFalse(result["never_left"])

    async def test_429_is_retried(self):
        self.fake.fail_queue.append((429, {"Retry-After": "0"}))
        result = await bc.push(self.fake.server, DEVICE, {"body": "x"}, attempts=2)
        self.assertTrue(result["success"])
        self.assertEqual(len(self.fake.requests), 2)

    async def test_exhausted_429_stays_retryable_but_not_never_left(self):
        self.fake.fail_queue.extend([(429, {"Retry-After": "0"}), (429, {"Retry-After": "0"})])
        result = await bc.push(self.fake.server, DEVICE, {"body": "x"}, attempts=2)
        self.assertEqual(result["error_kind"], "rate_limited")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["never_left"])

    async def test_exhausted_500_is_not_never_left(self):
        self.fake.fail_queue.extend([(500, {}), (500, {})])
        result = await bc.push(self.fake.server, DEVICE, {"body": "x"}, attempts=2)
        self.assertTrue(result["retryable"])
        self.assertFalse(result["never_left"])

    async def test_connection_refused_is_reported_and_redacted(self):
        dead = FakeBark().start()
        url = dead.server
        dead.stop()
        result = await bc.push(url, DEVICE, {"body": "x"}, timeout=1.0, attempts=1)
        self.assertIn("error", result)
        self.assertNotIn(DEVICE, result["error"])
        # The request provably never left, so cron may replay it.
        self.assertTrue(result["never_left"])

    async def test_ping(self):
        result = await bc.ping(self.fake.server)
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], "2.2.5")

    async def test_bark_code_error(self):
        self.fake.fail_queue.append((400, {}))
        result = await bc.push(self.fake.server, DEVICE, {"body": "x"}, attempts=1)
        self.assertEqual(result["error_kind"], "bad_format")
        self.assertIn("injected 400", result["error"])


class ExplainProbeTests(unittest.TestCase):
    """Doctor must name the failure class instead of echoing an exception."""

    def test_ok_is_silent(self):
        self.assertEqual(bc.explain_probe({"ok": True, "version": "2.2.5"}), "")

    def test_http_status_classes(self):
        self.assertIn("401/403", bc.explain_probe({"ok": False, "status": 401}))
        self.assertIn("404", bc.explain_probe({"ok": False, "status": 404}))
        self.assertIn("5xx", bc.explain_probe({"ok": False, "status": 502}))
        self.assertIn("not a Bark server", bc.explain_probe({"ok": False, "status": 200, "message": "hi"}))

    def test_transport_exceptions(self):
        cases = {
            "ConnectError: [Errno 8] nodename nor servname provided": "DNS failure or nothing listening",
            "ProxyError: 502 Bad Gateway": "proxy",
            "ConnectTimeout: timed out": "timed out",
            "SSLError: certificate verify failed": "TLS handshake failed",
        }
        for err, expected in cases.items():
            with self.subTest(err=err):
                self.assertIn(expected, bc.explain_probe({"ok": False, "error": err}))

    def test_unknown_error_falls_back_to_text(self):
        self.assertEqual(bc.explain_probe({"ok": False, "error": "Weird: boom"}), "Weird: boom")


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    """A stale local proxy in the environment must not make Bark look unreachable."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
                        "NO_PROXY", "no_proxy")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _dead_proxy(self):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens here now
        dead = f"http://127.0.0.1:{port}"
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            os.environ[key] = dead
        # 127.0.0.1 is normally in NO_PROXY, which would bypass the dead proxy and hide the case.
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        return dead

    async def test_dead_env_proxy_falls_back_direct(self):
        self._dead_proxy()
        fake = FakeBark().start()
        self.addCleanup(fake.stop)
        result = await bc.push(fake.server, DEVICE, {"body": "hi"}, attempts=1)
        self.assertTrue(result.get("success"), result)
        self.assertTrue(result.get("proxy_fallback"))
        self.assertEqual(fake.requests[-1]["payload"]["body"], "hi")

    async def test_explicit_direct_skips_env_proxy(self):
        self._dead_proxy()
        fake = FakeBark().start()
        self.addCleanup(fake.stop)
        result = await bc.push(fake.server, DEVICE, {"body": "hi"}, attempts=1, proxy="direct")
        self.assertTrue(result.get("success"), result)
        self.assertFalse(result.get("proxy_fallback"))

    async def test_explicit_proxy_is_honoured(self):
        dead = self._dead_proxy()
        fake = FakeBark().start()
        self.addCleanup(fake.stop)
        # No silent direct retry when the operator pinned a proxy: the failure is the answer.
        result = await bc.push(fake.server, DEVICE, {"body": "hi"}, attempts=1, proxy=dead)
        self.assertFalse(result.get("success"))

    async def test_ping_falls_back_direct(self):
        self._dead_proxy()
        fake = FakeBark().start()
        self.addCleanup(fake.stop)
        probe = await bc.ping(fake.server)
        self.assertTrue(probe.get("ok"), probe)
        self.assertTrue(probe.get("proxy_fallback"))


class NotifyRuleTests(unittest.TestCase):
    """Rules read from YAML: ``on:`` is a YAML 1.1 boolean, so ``statuses`` is canonical."""

    def test_status_filter_accepts_canonical_keys(self):
        import notify

        self.assertEqual(notify._status_filter({"statuses": ["completed"]}), ["completed"])
        self.assertEqual(notify._status_filter({"statuses": "failed"}), ["failed"])
        self.assertEqual(notify._status_filter({"only_status": ["interrupted"]}), ["interrupted"])
        # YAML `on: [completed]` arrives as the key True (bool), not the string "on"
        self.assertEqual(notify._status_filter({True: ["completed"]}), [])
        self.assertEqual(notify._status_filter({}), [])

    def test_send_keeps_configured_level_when_rule_has_none(self):
        """A rule without ``level`` must not downgrade ``defaults.level`` to 'active'."""
        import asyncio
        import notify

        fake = FakeBark().start()
        self.addCleanup(fake.stop)
        os.environ["BARK_KEY_TEST"] = DEVICE
        self.addCleanup(os.environ.pop, "BARK_KEY_TEST", None)
        settings = load_settings({
            "server": fake.server,
            "default_device": "test",
            "defaults": {"level": "timeSensitive", "group": "HermesE2E"},
        })
        asyncio.run(notify._send(settings, "no rule level"))
        sent = fake.requests[-1]["payload"]
        self.assertEqual(sent["level"], "timeSensitive")
        self.assertEqual(sent["group"], "HermesE2E")

    def test_turn_end_hook_pushes_once_and_debounces(self):
        import notify

        fake = FakeBark().start()
        self.addCleanup(fake.stop)
        os.environ["BARK_KEY_TEST"] = DEVICE
        self.addCleanup(os.environ.pop, "BARK_KEY_TEST", None)
        settings = load_settings({
            "server": fake.server,
            "default_device": "test",
            "notify": {"turn_end": {"enabled": True, "statuses": ["completed"], "min_duration_s": 0}},
        })
        original = notify.load_from_config
        notify.load_from_config = lambda: settings
        self.addCleanup(setattr, notify, "load_from_config", original)

        notify.on_pre_llm_call(session_id="S-test")
        notify.on_session_end(session_id="S-test", completed=True, failed=False,
                              interrupted=False, turn_exit_reason="completed", platform="cli")
        notify.on_pre_llm_call(session_id="S-test")
        notify.on_session_end(session_id="S-test", completed=True, failed=False,
                              interrupted=False, turn_exit_reason="completed", platform="cli")

        deadline = time.time() + 3
        while not fake.requests and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.4)  # let a second push surface if the debounce failed

        self.assertEqual(len(fake.requests), 1, fake.requests)
        self.assertIn("completed", fake.requests[0]["payload"]["body"])
        self.assertIn("cli", fake.requests[0]["payload"]["body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
