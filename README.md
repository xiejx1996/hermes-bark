# hermes-bark

Bark iOS push notifications as a **send-only** delivery platform for
[Hermes Agent](https://hermes-agent.nousresearch.com). One-way by design: Bark
has no inbound stream, no reply channel, and a server `200` means *queued to
APNs*, not *seen on the phone*.

Plugin id: `hermes-bark` · kind: `platform` · requires Hermes `>=0.23.0`

## What you get

| Surface | Use |
|---|---|
| `cron` → `--deliver bark` | job results land on your phone, no model call spent |
| `hermes send --to bark[:device]` | shell / scripts / agent CLI |
| `bark_notify` tool | the agent pushes a link, image URL or delivery notice mid-turn |
| hooks `on_session_end` / `subagent_stop` | optional, off by default — failure/interruption alerts only |
| Desktop page (`Bark notifications`) | channel status, device map, notification rules, test push, delivery log |

Multi-device aliases, Bark fields `level / sound / group / ttl / url / image /
icon / autoCopy / isArchive / id`, retry with never-left-the-process-only
semantics, and profile-scoped secret reads.

## Writing the alert

An alert is optional, and nothing here invents a summary. With no alert a push
behaves exactly like the plain plugin: whatever text arrived is the body. With
one, the phone gets a title and a body that starts with the point.

* **Tool** — `bark_notify` takes `title` + `body`; the agent writes them while it
  still knows what happened. The result reports the alert that was sent, plus any
  warnings about how it will look.
* **Script** — `hermes bark push --title … --body …`, or `--alert-file` with
  `{"title": …, "body": …}` (one file per run, written to a temp name and
  `os.replace()`d into place; `run_id` becomes the APNs collapse id so a retry
  replaces instead of buzzing twice, and a `created_at` older than 15 minutes is
  refused as stale unless `--allow-stale` is passed).
* **In-band block** — a script whose stdout is delivered verbatim can start it
  with `标题：…` / `摘要：…` (`title:` / `body:` also work). The block becomes the
  alert; the text itself is left untouched, so whatever else receives it is
  unchanged. The block
  must be at the top and ends at the first blank line, so a report that mentions
  "title:" inside is never mistaken for one.

Budgets are display width (CJK = 2). The title is one line, 36 units (~18 Chinese
characters) — measured, iOS ellipsises it around 38. The body is soft: the collapsed
view holds four lines (~140 units, ~70 characters; measured 168 on a current phone),
and a long-press shows the rest, so a longer body is sent as written. What matters is
the opening: the conclusion belongs in the first sentence, and a first sentence that
runs past the collapsed budget is reported back as a warning, never refused. The only
hard limit is the payload guard (3000 bytes, below APNs' 4096), where the answer is
not a smaller notification but a different channel.

Push after the referenced text has actually been delivered. An alert that points at
a channel whose delivery failed is worse than no alert; the plugin cannot check that,
so the producer sequences it.

`missing_alert` decides what happens to text that arrives with no alert:
`passthrough` (default) sends it as the body; `reject` refuses anything that does
not fit the collapsed view, so an unwritten alert cannot pass silently.

See `docs/push-mechanics.md` for the delivery chain and the measurements behind
those numbers.

## Install

```bash
hermes plugins install hermes-bark     # once listed in the catalog
# or, from a checkout:
cp -R . ~/.hermes/profiles/<profile>/plugins/hermes-bark
hermes plugins enable hermes-bark
```

Set the device key through Hermes — Dashboard → Secret on the plugin page, or
`hermes config set`:

```dotenv
BARK_TOKEN=<device key from the Bark app>     # alias "default"
BARK_KEY_IPHONE=<key>                          # extra device, alias "iphone"
BARK_SERVER=https://api.day.app                # optional, self-hosted is fine
BARK_HOME_CHANNEL=default                      # cron / `--to bark` target
```

Device keys are **routed by Hermes**, never read from disk by this plugin: a read
goes through `agent.secret_scope.get_secret`, i.e. the profile's
secret scope (with a process-env fallback in CLI contexts). That is also why a
key saved on the page is usable immediately. `config.yaml` never holds a key;
non-secret defaults (`server`, `level`, `group`, `ttl`, notification rules, …)
live in `plugins.entries.hermes-bark.settings` and are editable both in the
native Settings form and on the plugin page.

**Desktop half.** This package ships `desktop/plugin.js`. A package-managed
desktop half is opt-in: after install, enable the *Desktop* switch for the
plugin under Settings → Capabilities → Plugins (the renderer trusts plugin code
with the app's authority, so it asks once). The page then appears in the sidebar
and at `hermes://open/bark`.

## Behaviour notes

- **One-way.** Nothing is ever received. `send_image()` accepts an `http(s)` URL
  only — the Bark app downloads it in its notification extension, so a local file
  cannot be delivered (it is reported as `media_skipped`, not as a failure).
- **Alert optional.** A written alert gets a title (one line, 40 display units) and
  a body; without one the text is sent as-is. The body length is soft — the
  notification expands — but a first sentence that runs past the collapsed view is
  reported back as a warning. Only the 3000-byte payload guard refuses.
- **Retries** cover "the request never left" (connect/DNS/proxy), `429` and
  `5xx`. A read timeout is *not* retried — the payload may already be queued.
- **Automatic pushes are off by default.** When enabled they fire only for the
  statuses you list (default `failed,interrupted`), never for a short successful
  turn, at most once per session per 60 s, and never inside your quiet hours.
  Hooks are for incidents; delivery receipts belong to explicit sends.
- **Proxy.** `proxy: ""` follows `HTTPS_PROXY`; `direct` forces a direct
  connection; a URL pins the proxy. A dead environment proxy that points at
  loopback is retried once directly (`proxy_fallback: true`).
- **Dedupe** sets Bark's `id` field for a 5 s window so a retry or a hook racing
  `hermes send` does not double-buzz. Bark needs app ≥ 1.5.2 for `id`.

## Disclosure

- Outbound requests go to your Bark server only: `https://api.day.app` by
  default, or whatever `BARK_SERVER` points at. Plain `http://` is accepted for
  a LAN you control and warned about, never silently upgraded. Notification
  titles/bodies, plus any `url` and `image` URL you pass, are sent there and
  reach APNs through it. If you self-host, that server sees them.
- No telemetry, no analytics, no self-updater, no outbound calls anywhere else.
- Secrets are read via Hermes (`agent.secret_scope.get_secret` →
  `gateway.platforms._shared.get_scoped_secret`, `external_fallback` only when no
  profile scope is bound); the plugin never parses `.env` itself, and a bound
  scope is authoritative — a scoped miss returns nothing rather than borrowing
  another profile's value. A literal key in `config.yaml` is refused and reported
  as `ignored`, never used.
- Settings saved from the desktop page are written to
  `plugins.entries.hermes-bark.settings` in your `config.yaml` through
  `save_plugin_settings` (atomic, schema-validated). Adding or removing a device
  writes the device key through Hermes' credential lifecycle
  (`save_provider_env_credential` / `remove_provider_env_credential`, i.e. the
  profile `.env` plus the live secret scope) — the plugin does not touch that
  file itself.
- Delivery records (last 400 attempts, 250 kept) are written locally to
  `<hermes home>/plugin-data/hermes-bark/deliveries.jsonl`. Every path that pushes
  records one: hooks, the `bark_notify` tool, the adapter, cron/`hermes send`
  (standalone), the CLI test, and the page's test button.
- The desktop page calls only the plugin's own routes under
  `/api/plugins/hermes-bark/`.
- UI copy: English is the source language; the Chinese bundle is a data file
  (`dashboard/locales/zh.json`) the backend ships to the page, so the same
  strings serve the API messages and the rendered page. Contribution labels
  (sidebar, palette) are English — the host registers them before the first
  snapshot arrives.

## Development

```bash
python tests/test_bark.py             # 36 client/settings/notify tests, no network
python tests/test_dashboard_api.py    # 14 router tests against a fake bark-server
node  tests/render_page.mjs --locale=en   # renders plugin.js headless, asserts no CJK leak
node  tests/render_page.mjs --locale=zh   # same page with the Chinese bundle
HERMES_SRC=~/path/to/hermes-agent python tests/e2e_adapter_contract.py
python scripts/serve-local.py --port 18123   # same router, own process, no gateway restart
```

`tests/fake_bark.py` is a bark-server stand-in that records JSON payloads and can
return `500`/`429`/slow responses on demand.

## License

MIT — see `LICENSE`.
