# How a Bark push actually reaches the lock screen

Written while designing the alert contract. Every claim below is either read out of
the Bark app / bark-server source at the pinned versions, from Apple's documentation,
or measured by a third party; the source is named next to it. Anything not
verified here is marked as such.

## 1. The chain

```
producer ──HTTP POST /push──▶ bark-server ──HTTP/2──▶ APNs ──▶ iPhone
                                                              │
                                              Notification Service Extension (Bark app)
                                                              │
                                                    system notification UI
```

**bark-server** (`Finb/bark-server`, `apns/apns.go`) turns the request into an APNs
payload:

* `pl := payload.NewPayload().MutableContent()` — *every* push is sent with
  `mutable-content: 1`, so the app's Notification Service Extension (NSE) runs before
  the system displays anything.
* `AlertTitle(...)`, `AlertSubtitle(...)`, `AlertBody(...)` come straight from the
  request's `title` / `subtitle` / `body`.
* `ThreadID(group)` — the `group` parameter becomes APNs `thread-id`, which is what
  makes notifications stack into one thread on the device.
* `CollapseID(msg.Id)` — the `id` parameter is APNs' collapse id: a later push with
  the same id **replaces** the earlier notification instead of adding a second one.
  (This is why "dedupe by id" works: it is replacement, not filtering.)
* Every other parameter (`level`, `image`, `icon`, `markdown`, `url`, `copy`,
  `autoCopy`, `isArchive`, `ttl`, `call`, `action`, …) is copied verbatim as a custom
  key next to `aps`, where the NSE reads it.
* Expiration is 24 h; the HTTP answer is only "APNs accepted it".

**The app's NSE** (`Finb/Bark`, `NotificationServiceExtension/NotificationService.swift`)
runs a fixed processor chain:

`ciphertext → markdown → soundUrl → level → badge → autoCopy → archive → mute → call → setImage → setIcon`

Consequences worth knowing:

* `image` is downloaded **on the phone** (`ImageProcessor` + `ImageDownloader`,
  Kingfisher, 10 s timeout) and attached as a `UNNotificationAttachment`. The Bark
  server never fetches it — so a plugin-side "SSRF guard" on image URLs protects
  nothing server-side; it only decides whether the phone will be asked to load it.
* `markdown` replaces `body` with the markdown **stripped to plain text**
  (`MarkdownParser(configuration: .clear)`), then collapses blank lines. Formatting
  does not survive into the notification; it only affects the in-app history view.
* `level` maps to `UNNotificationInterruptionLevel`: `passive`, `active`,
  `timeSensitive`, `critical`. `critical` additionally needs the Bark app's critical
  alert permission; it is the only level that bypasses the mute switch.
* Apple's NSE contract gives the extension ~30 s to call the completion handler,
  otherwise the system shows the original payload (Apple, "Modifying content in newly
  delivered notifications"). A slow image download therefore cannot block the alert —
  it just arrives without the image.

## 2. What iOS actually shows

Apple's payload reference (`Generating a remote notification`) defines three text
fields — `title`, `subtitle`, `body` — plus `thread-id` for grouping and
`interruption-level` for urgency. The APNs payload limit is 4 KB (4096 bytes); VoIP
pushes get 5 KB.

On the lock screen the notification renders as: app icon and app name, the **title**
(bold, one line, ellipsised), an optional **subtitle** line, and the **body**.

Measured limits for the body, in the default text size:

* Airship's device measurements: ~150 characters visible on the lock screen of an
  iPhone 13 Pro Max in portrait; longer bodies need a long-press to expand.
* Truncly's measurement: four lines, ~1084 px, ≈149 characters before iOS cuts the
  body.

Both counts are for the Latin alphabet. A CJK character is full-width, so the same
four lines hold roughly **70–80 Chinese characters**, which is what the collapsed
view (the state you see without touching the phone) shows; a device measurement
below lands at 84. This is the number the alert budget is built on.

Other display behaviour that shapes the design:

* If the user hides notification previews, iOS shows only the app name and
  "Notification" — nothing an app sends can survive that, so the body must also be
  safe to lose.
* **The notification is a window, not the message.** Tapping it opens the Bark app,
  whose history keeps the whole body — so the four-line cut costs a glance, not the
  text. `isArchive=1` forces retention (`ttl` then expires it). A body sent through
  the `markdown` field is archived verbatim with `bodyType=markdown` and parsed by the
  message list, so the banner shows stripped text while the app history shows the
  styles (source: `ArchiveProcessor` stores the raw `markdown`; `MessageItemModel`
  parses it when `type == .markdown`). Checked on a device with a probe push:
  the banner dropped every marker, while the app history kept **bold**, *italic*,
  inline `code` (monospaced, tinted), bullet lists and tappable links — a `##`
  heading came through as bold but no larger than body text. So the app keeps the
  emphasis, not the layout. This plugin dropped the field until now — it was
  missing from `build_payload`'s whitelist, so no push could reach that path.

Apple's HIG adds the editorial rules that match these mechanics: keep the title short
(title-style, no ending punctuation), write complete sentences, do not pre-truncate
(the system truncates for you), never send several notifications for the same thing,
and avoid instructing the reader to do multi-step things they will have forgotten
after dismissing the banner.

## 3. What this means for an agent's push

1. **The title is the only line that always survives.** It must carry what the push is
   about and how it ended: `Nightly job · 1 item to review`, not `Hermes`.
2. **The first line decides whether the alert is useful, the first four are all that
   show.** The verdict goes first; numbers second; nothing else.
3. **No date stamp.** The lock screen already prints the arrival time.
4. **No markdown, no headings, no bullets at the start.** They either get stripped or
   eat the visible width.
5. **One push per event.** A stack hides everything but the newest; a second push is a
   replacement opportunity (same `id`) or noise, never a continuation.
6. **A full report cannot be "pushed shorter" by cutting it.** Cutting produces the
   fragment problem this repo exists to fix; the only real fix is that whoever wrote
   the report writes the two sentences as well.
7. **Longer is not lost.** The body can run past four lines: the banner shows the
   first four and the rest waits in the app's history. What still matters is that the
   first line carries the point, because that is what decides whether anyone opens it.

## 4. Measured on a device

Two probe pushes to a current iPhone at the system default text size. The first
carried a single-sentence body; the second added a deliberately over-long title, a
subtitle, and seven labelled body lines so the cut points were visible.

| probe | what was sent | what the screen showed |
|---|---|---|
| 1, `passive` -> Notification Center | title 23 units, body 70 units (35 Chinese characters), one sentence | body in exactly two lines, 21 + 14 characters, nothing cut |
| 2, `active` -> Lock Screen | title 42 units with a tail marker, subtitle "副标题测试 SUBTITLE", body 7 labelled lines | title cut with an ellipsis after the 12th circled numeral (~37-39 units); **subtitle shown on its own line**; body cut after **4 lines** with an ellipsis |

What this fixes:

* **The collapsed body is four lines, not two.** Probe 1 looked like two lines only
  because the text was shorter than the capacity. At ~21 characters per line that is
  roughly 84 characters, ~168 display units, before the ellipsis.
* **The title is one line and cut around 38 units**, close to the 40-unit guess but
  not above it. The default is 36 to leave margin.
* **The subtitle is visible in the collapsed state**, on its own line between title
  and body, and it does not cost a body line.

Caveats that keep the defaults below these numbers: narrower phones fit fewer
characters per line (an iPhone SE is ~13% narrower than the model measured), and
Larger Text / Display Zoom shrink capacity further. `alert.COLLAPSED_WEIGHT` therefore
defaults to 140 units (~70 characters) rather than the measured 168.

## 5. Not verified

* Whether the subtitle line is visible in the collapsed state. It is documented as
  "additional information that explains the purpose of the notification", and
  preview tools render it under the title, but it has not been measured.
* Per-device truncation for CJK at non-default text sizes (Larger Text, Display Zoom),
  and how much the four-line expanded cut moves with them.

