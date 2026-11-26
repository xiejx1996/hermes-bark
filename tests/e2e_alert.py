"""End-to-end check of the alert contract against a fake bark-server.

Proves what actually leaves the plugin for the shapes that matter:
plain text (upstream parity), an in-band 标题/摘要 block, the tool path,
a missing alert under ``missing_alert=reject``, and the payload guard.

Run:
    HERMES_SRC=~/.hermes/hermes-agent <venv>/bin/python tests/e2e_alert.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ.get("TMPDIR", "/tmp")) / "bark-e2e"
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(SANDBOX)
os.environ["HERMES_HOME"] = str(SANDBOX / "hermes")
for name in [key for key in os.environ if key.startswith("BARK_")]:
    os.environ.pop(name, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

HERMES_SRC = Path(os.environ.get("HERMES_SRC", "~/.hermes/hermes-agent")).expanduser()
sys.path.insert(0, str(HERMES_SRC))

from fake_bark import FakeBark  # noqa: E402

REPORT = """Cronjob Response: nightly maintenance
(job_id: 0a1b2c3d)

# Nightly report (2026-01-01 00:00 UTC)

## Brief

- **Next**: one item waits for your decision: item-7.
- Items: 1 total, 0 done, 1 pending.

## Details
- Nothing else happened.
"""

BLOCKED = """标题：Nightly job · 1 item to review
摘要：All checks passed; one item waits for your decision.

# Nightly report (2026-01-01 00:00 UTC)
Details omitted from the phone.
"""

LONG = ("The gateway restart finished normally and every scheduled job ran, but the process check "
        "found three orphaned python processes holding 1.2 GB, 800 MB and 300 MB; all were cleared "
        "at 10:04, and 290 GB of disk remain.")

def build_adapter(server: str, **extra):
    from gateway.config import Platform, PlatformConfig
    from adapter import BarkAdapter

    Platform._add_pseudo_member("bark")  # what register_platform() does at plugin load
    os.environ["BARK_TOKEN"] = "test-device-key-0123456789"
    cfg = PlatformConfig(enabled=True, extra={"server": server, "title": "Hermes", **extra})
    return BarkAdapter(cfg)


async def main() -> int:
    import importlib.util

    spec = importlib.util.spec_from_file_location("bark_plugin_tools", ROOT / "tools.py")
    plugin_tools = importlib.util.module_from_spec(spec)
    sys.modules["bark_plugin_tools"] = plugin_tools
    spec.loader.exec_module(plugin_tools)
    handle_bark_notify = plugin_tools.handle_bark_notify

    results = []
    with FakeBark() as fake:
        os.environ["BARK_SERVER"] = fake.server
        adapter = build_adapter(fake.server)
        await adapter.connect()

        # 1. No alert: passthrough, exactly what the upstream plugin does.
        before = len(fake.requests)
        sent = await adapter.send("default", REPORT)
        payload = fake.requests[before]["payload"] if len(fake.requests) > before else {}
        results.append({"case": "no-alert (passthrough)", "ok": sent.success,
                        "title": payload.get("title"),
                        "body_head": (payload.get("body") or "")[:40],
                        "body_is_input": (payload.get("body") or "").strip() == REPORT.strip()})

        # 2. In-band block: title and body taken from it, report left to the chat channel.
        before = len(fake.requests)
        sent = await adapter.send("default", BLOCKED)
        payload = fake.requests[before]["payload"] if len(fake.requests) > before else {}
        results.append({"case": "in-band block", "ok": sent.success,
                        "title": payload.get("title"), "body": payload.get("body")})

        # 3. Tool: the alert is the tool arguments, verbatim.
        before = len(fake.requests)
        raw = await handle_bark_notify({
            "title": "Nightly job · 1 item to review",
            "body": "All checks passed; one item waits for your decision.",
        })
        payload = fake.requests[before]["payload"] if len(fake.requests) > before else {}
        results.append({"case": "tool", "result": json.loads(raw),
                        "title": payload.get("title"), "body": payload.get("body")})

        # 4. missing_alert=reject: text that does not fit the collapsed view is refused.
        strict = build_adapter(fake.server, missing_alert="reject")
        await strict.connect()
        before = len(fake.requests)
        blocked = await strict.send("default", LONG)
        results.append({"case": "missing_alert=reject", "ok": blocked.success,
                        "error": blocked.error, "requests_made": len(fake.requests) - before})

        # 5. Payload guard: a body past the APNs-safe size never leaves.
        before = len(fake.requests)
        raw = await handle_bark_notify({"title": "too long", "body": "a" * 4000})
        results.append({"case": "payload guard", "result": json.loads(raw),
                        "requests_made": len(fake.requests) - before})

    for item in results:
        print(json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
