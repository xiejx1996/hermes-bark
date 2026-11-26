---
name: bark-notify
description: Use when the user asks to be pinged on their phone, or a long task finishes while they are away. Sends Bark iOS push notifications.
---

# Bark notifications

Use the `bark_notify` tool, or `hermes bark push` from a script. Do not shell out to
`curl` — the tool resolves the device alias, applies configured defaults, retries
transport failures, and keeps the device key out of the transcript.

## Write the notification when you have one

An alert is optional: with no title and no body written for the phone, the text you
send is the body, exactly like a plugin that has no alert support. Writing one buys
a title and an opening line that carries the point, which is what the reader sees.

The person reads a lock screen: title in bold, four lines of body without touching
the phone, more after a long-press. A report opens with structure and ends with the
conclusion, so pasting it as the body hides the point at the bottom.

| field | what goes in it | budget |
|---|---|---|
| `title` | what this is plus how it ended: `Nightly job · 1 item to review` | one line, no full stop |
| `body` | verdict in the first sentence, then only the numbers that matter | four lines before a long-press; total length is soft |

Good: `All checks passed; one item waits for your decision.`
Bad: the report's opening paragraph, or `(none) ... nightly maintenance ... 10:00`, which
is a fragment rather than a sentence.

Long bodies are allowed; the notification expands. What is not allowed is burying
the verdict: the tool returns `warnings` when the first sentence runs past the
four-line collapsed view, when the body carries markdown markers (rendered as plain text, so
they only cost width), or when the title ends in a full stop. It also returns the
alert it actually sent, so you can see the phone-side text.

## When to send

- The user explicitly asked to be notified ("ping my phone", "tell me when it's done").
- A job you started ran for a long time and the user is away. Send **once**, at the end.
- Something needs attention and the user is not watching this session.

Do **not** send a notification per turn, for routine confirmations, or to summarize a
conversation the user is actively reading.

## Scripts and cron jobs

Three ways, pick by what the producer can do:

- `hermes bark push --title "…" --body "…"` — a script.
- `--alert-file` holding `{"title": …, "body": …, "level": …}` — one file per run,
  written to a temp name and `os.replace()`d into place. Carry `created_at` and
  `run_id`; the run id becomes the notification's collapse id, so a retry replaces
  the first push, and a file older than 15 minutes is refused as stale.
- a `标题：…` / `摘要：…` block at the top of the text — for a job whose stdout is
  delivered verbatim. The block must come first and ends at the first blank line;
  rest of the text is left untouched, so whatever else receives it is unchanged.

Send the push **after** the text it points at has been delivered: an alert that says
"details in <channel>" must not arrive when that delivery failed.

A `no_agent` cron job cannot call a tool, so it goes through `hermes bark push` or
the in-band block. A cron job that runs an agent can call `bark_notify` itself and
should then leave `bark` out of `deliver`, so the phone does not get the report a
second time.

Text delivered with `deliver=…,bark` and no alert goes out as-is (upstream
behaviour). Installations that would rather refuse that can set
`missing_alert: reject`.

## Parameters

| field | notes |
|---|---|
| `body` | required; verdict in the first sentence, then the numbers that matter |
| `title` | short label, no trailing punctuation; defaults to the configured title |
| `device` | alias from config (`iphone`, `ipad`); omit for the default |
| `level` | `active` (default), `passive` (list only, no screen wake), `timeSensitive` (pierces Focus), `critical` (needs the app's critical-alert permission) |
| `sound` | sound name |
| `group` | iOS notification threading |
| `image` | **public http(s) URL only** — the Bark app on the phone downloads it (10 s timeout); the server never fetches it, and local files cannot be attached |
| `url` | opened when the notification is tapped |
| `ttl` | seconds the notification stays deliverable; `0` = no expiry |

## What the result means

`{"ok": true, "queued": true}` means the Bark server accepted the payload and handed
it to APNs. It does **not** mean the phone displayed it: the device may be offline,
the app may be killed, or Focus may have suppressed it. Say "sent" or "queued", never
"the user has seen it". The result also echoes the title and body that were sent.

A failure returns `{"ok": false, "error": ..., "error_kind": ...}`. `error_kind` is one
of `bad_format`, `forbidden` (wrong or rotated key), `rate_limited`, `transient`,
`server`. Do not retry `forbidden` — the key is wrong. The only length refusal is the
payload guard (3000 bytes, below APNs' 4096): that means the content belongs in the
channel that can show it, not in a smaller notification.

## Images

Local files cannot ride along. If the user wants a screenshot on their phone, either
give a public URL the phone can reach or say plainly that it cannot be attached.
A LAN URL only works while the phone is on that LAN.
