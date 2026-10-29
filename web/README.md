# web — SideCode web app

The browser client for SideCode: devices, sessions, and an instant-messaging style
control surface for Claude Code and Codex sessions running on your machines.
It is a static single-page app; the gateway serves `web/dist` with an SPA fallback.

Everything on the wire follows `protocol/PROTOCOL.md` (protocol version 1). The app never
talks to a device directly — every request and every event goes through the gateway.

## Commands

| Command | What it does |
| --- | --- |
| `npm ci` | Install exactly the pinned dependency tree |
| `npm run dev` | Vite dev server on 5173, proxying `/api` and `/ws` to `http://127.0.0.1:8787` |
| `npm run dev:mock` | The same dev server plus the bundled mock gateway (no gateway or device needed) |
| `npm run mock` | Only the mock gateway, on 8787 |
| `npm run build` | Typecheck, then build to `web/dist` |
| `npm test` | Vitest suite |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint (flat config) |

Point the dev server at a different gateway with `RC_GATEWAY=http://host:port npm run dev`.

## Developing against the mock gateway

```
npm run dev:mock      # then open http://localhost:5173 and sign in with: dev
```

`mock/server.ts` implements the app-facing half of the protocol: the HTTP API, `WS /ws/app`
and `WS /ws/stt`. It ships two devices and five sessions covering the states the UI has to
handle — running, needs approval, idle, terminal-controlled, and a Codex session. Opening the
running session plays a scripted turn: streamed thinking, streamed Markdown, tool rows with a
live output box, a failing shell run, two diffs, an approval, and a question. Answering the
approval and the question drives the turn to completion. `POST /api/devices/pairing` starts a
pairing that walks `waiting → enrolled → online → agents` over about six seconds, and the STT
socket returns scripted partials and a final transcript.

The mock and the tests share `mock/fixtures.ts`, so a fixture change shows up in both.

## Layout

```
src/
  protocol/       Wire types (types.ts) and frame/request shapes (frames.ts)
  lib/            api.ts (HTTP), ws.ts (app socket), gateway.ts (socket handle), formatters
  stores/         zustand stores: auth, connection, devices, sessions, chat, outbox, settings
                  timeline.ts is the pure event -> renderable-items reducer
  components/     Buttons, modal/drawer, popover/menu, context menu, segmented control, switch, status dots
  layout/         Top bar shell and the product mark
  features/
    login/        Password sign-in
    devices/      Device list, rename/revoke, Add device modal with live pairing steps
    sessions/     Session list, New session drawer, directory browser and probe
    chat/         Sidebar, header, timeline and blocks, composer, status line
    voice/        Mic capture, 16 kHz PCM16 conversion, live transcription panel
    live/         Voice assistant: the entry, the panel, the call and its saved history
    settings/     Account, web push, voice, about
  push/           Web Push subscription and service worker registration
  styles/         tokens.css (design tokens) and base.css
mock/             Mock gateway, fixtures, scripted turn
tests/            Vitest suites
public/           manifest, service worker, icons, audio worklet
```

Styling is hand-written CSS with tokens from `styles/tokens.css`; there is no CSS framework.
Global tokens and primitives are imported first in `main.tsx` so feature stylesheets override
them rather than the other way round.

Every user-visible string lives in `src/strings.ts` so the app can be localised later.

## Protocol behaviour worth knowing

- **Reconnect** — the app socket backs off exponentially, capped at 5 s, replies to `ping`, and
  treats 60 s without any frame as a half-open connection (mobile NAT never delivers `onclose`).
  After a `1012` close (the gateway restarting) it asks again every second, spread by 15 %, for
  30 s before the usual backoff takes over. Subscriptions are re-issued after every reconnect
  with the latest `since_seq`.
- **Timeline** — a later event with the same `block_id` replaces the earlier one, except
  streaming `delta` events, which append. An event with a `seq` at or below the last applied one
  is dropped unless it carries a block the timeline does not hold or a newer version of one it
  does: that is an event that arrived behind newer ones, and it is merged where it belongs.
  `status`, `meta`, `queue` and `todos` are session state, not timeline rows, so they are folded
  separately from live frames, from a subscribe replay, and from the newest history page
  (amendment A6 also carries a `queue` snapshot on the subscribe reply).
- **Holes and resync** — a live event that skips a `seq` leaves a hole: the timeline keeps the
  last contiguous `seq` beside `lastSeq`, and the chat store asks the device for everything after
  it (`session.history` with `after_seq`, amendment A9) and merges the pages by block id, the
  newer copy of a block winning. A `resync` keeps the transcript the same way: the rows stay
  while the device is asked for what follows the cursor. A jump inside a subscribe reply is not a
  hole, because the gateway may answer from the device's history.
- **Unreadable conversations** — a conversation that could not be read shows why, with a Retry,
  and never "No messages yet", which is only said after a read succeeded and found none. It is
  asked for again by itself when its device comes online and, while it is online, after 2, 4, 8,
  15 and 15 s.
- **Sending** — the composer always sends `mode:"auto"` and lets the device decide: send now
  when idle, steer when the agent advertises `steer`, queue otherwise. The button label follows
  that decision. "Interrupt & send" is a separate, explicit action. The one exception is a queued
  message taken back from the Up next list to be edited, which goes back with `mode:"queue"` and
  its `queue_ts` (amendment A43).
- **Uncertain delivery** — a `session.send` whose outcome is unknown is never resent
  automatically. The composer shows "Delivery unconfirmed" with a Retry that reuses the same
  request id.
- **Terminal control** — the composer is gated on `control`, never on `state`: a mirrored
  session reports `running` while the terminal drives its turn and `readonly` only when idle
  (amendment A7), so `control: "terminal"` disables the composer, hides Stop and offers
  "Take over" in both cases.
- **Block order** — blocks sort by `first_seq ?? seq` (amendment A8), so a tool call that
  finishes long after it started keeps its place, live and after a reload.
- **Truncated output** — rows fold beyond 20 lines; `output_truncated` adds "Open full output",
  which fetches the untruncated block with `session.block`.
- **Close codes** — 4401 and 4403 end the session and return to login (amendment A4); 1012
  retries every second for 30 s; every other close code reconnects with backoff.
- **Failed actions** — stop, approve, answer, expand, dequeue and setting changes all report
  their failure in a dismissible banner above the composer instead of rejecting silently.

## Voice

Holding <kbd>⌥</kbd>+<kbd>Space</kbd> (or tapping the mic) opens `WS /ws/stt`, captures the
microphone through an AudioWorklet (with a ScriptProcessor fallback), downsamples to 16 kHz
mono PCM16LE with a box filter, and sends ~120 ms binary frames. Partial transcripts stream back
into an editable field; "Stop & send" sends `stt.stop`, waits for `stt.final`, then submits. The
mic is hidden when the gateway reports `stt.enabled: false`.

## Reading while an agent works

The timeline auto-follows the bottom until the reader scrolls away. From then on it counts new
blocks (not streaming deltas) behind a "Back to latest" button, and only a prepended history
page moves the scroll anchor, so content appended below never drags the viewport down.

## PWA

`public/manifest.webmanifest` and `public/sw.js` are served as-is. The service worker is
registered in production builds only. It is network-first for navigations, cache-first for
hashed assets under `/assets/`, and bypasses `/api`, `/ws`, `/install.sh` and `/dist/`. Only
successful same-origin responses are cached, so a gateway error page never becomes the offline
shell. Push payloads carry only `{rc: {kind, device_id, session_id, device_name}}`; the
notification text is generic and a click focuses or opens
`/sessions/<device_id>/<session_id>` — an already-open tab is routed by the `rc.navigate`
message the app listens for.

A tab opened before a deploy keeps its old entry file, and the first lazy chunk it asks for
afterwards is gone: the gateway answers 404 for a missing `/assets/` file, `vite:preloadError`
and the `ChunkBoundary` around every lazy part keep the page standing, and a floating notice asks
for a reload. Nothing reloads by itself, because drafts live in memory only.

## Responsive

At 1024 px and above the chat is a two-pane layout with the session sidebar. Below that the
sidebar collapses (the Sessions page takes its place), the chat runs full width with a back
button, and the composer sticks to the bottom above the keyboard.
