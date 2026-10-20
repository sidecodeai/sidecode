# The web app

A static single-page app the gateway serves from `web/dist`. React 19, Vite, TypeScript, and
hand-written CSS with no framework. It talks only to the gateway and follows
`protocol/PROTOCOL.md` exactly.

## Screens

| Route | What it does |
| --- | --- |
| `/` | Decides where an open lands and goes there; the `*` fallback does the same |
| `/login` | Username and password sign-in against the gateway, and **Create an account** when the gateway takes registrations (A24) |
| `/devices` | Device list: a computer glyph, the name, a status line of online state, platform, session count and reach, the agents as logos, and — only while an update runs or has failed — "Updating…" or "Update failed · <reason>"; Rename and Revoke on every row, **Retry update** on a failed one; **Add device** with the copyable one-liner, the pairing code, its expiry, live handshake steps, and the scan flow beside them. The row itself opens the device |
| `/devices/:deviceId` | One device: the machine's own facts, including the hostname and the architecture the row drops, then a card per agent it found, how each is signed in, and a meter per rate-limit window (A33) |
| `/pair` | Claims the token a host printed as a QR code and shows the same handshake (A23) |
| `/sessions` | Every session across every device: one collapsible group per device, its active rows and then its own collapsed **Archive**, a search, an agent filter and a device filter, and **New session** in a right-hand drawer |
| `/sessions/:deviceId/:sessionId` | The chat: sidebar, timeline, composer, status line |
| `/settings` | The Settings screen (`docs/DESIGN.md` § "The Settings screen"): an identity header — initials, username, `role · connection dot · host` — then four groups of two-line rows, **Account** (Users for the admin role, Change password for every account but `admin`, Sign out, which asks first), **While you're away** (Notify me, "Resume after the limit resets", A35), **Voice** (Transcribe, which names the gateway and offers nothing to choose, A44; polish and its model and strength) and **Reading** (language, timeline detail), and one caption line with the gateway and protocol versions |
| `/users` | The admin's accounts screen: the registration switch, one row per account, Reset password / Disable / Delete, and **Add user** (A24). A member who types it lands on Sessions |

## Commands

```sh
cd web && npm ci
```

| Command | What it does |
| --- | --- |
| `npm run dev` | Vite on 5173, proxying `/api` and `/ws` to `http://127.0.0.1:8787` |
| `npm run dev:mock` | The same server plus the bundled mock gateway. Sign in as `admin` / `dev`, or as the member `alice` / `devdevdev` |
| `npm run mock` | Only the mock gateway, on 8787 |
| `npm run build` | Typecheck, then build to `web/dist` |
| `npm test` | Vitest |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint, flat config |

`RC_GATEWAY=http://host:port npm run dev` points the proxy somewhere else.

## The mock gateway

`mock/server.ts` implements the app-facing half of the protocol — the HTTP API, `WS /ws/app` and
`WS /ws/stt` — so the whole UI can be developed with no gateway and no device. It ships two devices
and twenty sessions covering running, needs-approval, needs-input, errored, idle, paused by the
usage limit,
terminal-controlled, shared through the Claude channel, shared through the Codex daemon, Codex,
terminal sessions on a device that has neither the shim nor the Codex daemon, three sessions whose
CLI exited (`control: "none"`) and two archived by hand, spread over both devices so every device
group has both halves of an Archive under it. Between them they show all five status-dot tones.
One of the two devices advertises all four agents (A25, A26) — the two newest copied from
`protocol/fixtures/objects/agent.grok.json` and `agent.pi.json`, which `tests/agents.test.tsx`
asserts they still equal — and carries one session of each: a Grok Build session a terminal holds
inside Grok's leader, which the device has joined (`control: "shared"`, A28), a second Grok Build
session an app started, so its command list can actually be opened, and a pi session on the
`on-request` permission mode its extension enforces. The other device carries a Grok Build agent
whose leader mode is off (`attach_ready: false`) and a terminal Grok session under it, which is
where the leader hint renders. `ses-limit` is the A35 scenario: a Claude Code turn that ran into
the five-hour window, so its history ends with the vendor's sentence as an `error`, a
`turn_completed` carrying `limit`, and the `resume {scheduled}` the device wrote, and its summary
carries the pending `Session.resume` the notice above the transcript reads. Change and Cancel there
reach `session.resume_set` and `session.resume_cancel`, which the mock answers with the session and
a fresh `resume` row; a message sent into that session cancels the resume the way §6.3 says a device
does. `GET` and `PATCH /api/preferences` are served per account, `hello` carries `preferences`, and
a `PATCH` fans `preferences.updated` out to every app socket of the account, so two browser tabs
signed in as the same person follow each other.
Opening the running session plays a scripted turn: streamed thinking, streamed Markdown, tool rows
with a live output box, a failing shell run, two diffs, an approval and a question. Both drive the
turn to completion. The shared session plays the A10 path end to end: a send while the terminal is
idle is injected at once and answers with a relayed Allow/Deny approval, a send during that turn is
held as a queue entry and becomes a bubble only when the turn ends and the CLI takes it (A19), and
`session.set`, `session.stop` and `session.takeover` answer with the errors the contract specifies.
Its history also carries a question the terminal answered first, so the card reads "Answered in the
terminal" (A20). The shared
Codex session plays the A11 path: it opens on a turn the terminal started, a send steers that turn
(`accepted: "steered"`), `session.set` applies, Stop interrupts, and a command approval offers all
four daemon decisions. Its history carries a request the TUI answered first, so the card reads
"Answered in the terminal". A send is answered at once and the `user_message` echoed 400 ms later under the
request id, the way a device behaves (A12), so the pending bubble is visible in development; a
message queued during a turn is dequeued when that turn ends and keeps its id, while a turn the mock
starts itself, such as a `first_message`, mints its own block id and exercises the app's fallback.
Queues follow the device's A43 rule (`mock/queue.ts`): `ts` order with no two entries alike, a send
carrying `queue_ts` held under it, anything else at the end, an `attachments` count on an entry
sent with files, `bad_request` for a malformed `queue_ts`, and `not_found` from
`session.queue_remove` for an id the mock no longer holds. `ses-vite`, which waits on an approval
and so queues every send, starts with three queued messages, the middle one with two files, so the
Up next chip, its list and an edit can be tried at once. It also starts with the red dot of A47
(`unseen: true`, the only session that does), and every scripted turn marks the session it plays in
when it stops to ask or ends, the way the gateway does (`mock/unseen.ts`, below).
It knows two accounts (A24), `admin` / `dev` and the member `alice` / `devdevdev`, with the
registration switch closed and the account routes of 3.9 behind the admin's role, so the sign-in
form, registration, the Users screen and a member's Settings can all be driven with no gateway. The
scripted devices are the admin's: a member's `hello`, device list and session list come back empty,
a subscribe naming one of the admin's sessions answers `not_found`, and a pushed frame reaches only
the account that owns the device it is about. Pairing walks
`waiting → enrolled → online → agents` over about six seconds, and the speech socket returns
scripted partials and a final transcript. One of the two devices runs the build `/api/config`
reports and the other the one before it, so the list shows a row with nothing to do beside a row
offering Update; `device.update` is accepted, flips that device to `updating`, and brings it back on
the new build six seconds later, which is what its `hello` would report (A22). Both halves of
pairing by scanning are there (A23): `POST /api/pairing/requests` mints a claim token for a host,
`POST /api/pairing/requests/{token}/claim` mints its pairing code and starts the same progress, and
the host's status poll answers at once instead of holding a connection for 25 s the way the gateway
does — nothing in the app polls it. Every agent carries the accounts of A33, and the two devices
between them hold each shape a device page can meet: a Claude Code Max account with a tier and three
windows, a Codex `pro` account whose five-hour window is 84 % spent, so one meter is drawn in the
attention colour, an xAI account the leader exposes no windows for, pi's two providers — an
Anthropic subscription and an *OpenAI API key · api.relay.example* — and on the other device a
Claude Code signed in nowhere, a Codex account whose windows failed to read, and a Grok Build the
machine's older client never looked at. `withoutLimits` in `mock/fixtures.ts` is what divides them:
the device list and `hello` carry the accounts alone, and only the reply to `device.agents` carries
`limits`, `limits_error` and `limits_checked_at`, 900 ms later, so the page's *Checking…* state is
really seen. The mock and the tests share
`mock/fixtures.ts`, so a fixture change shows up in both.

Two gateway behaviours the mock used to be missing are in `mock/replay.ts`, so that a development
run can reveal the bugs they guard against. `session.send` is idempotent under its request id
(§8 rule 7, A12): the mock remembers what it answered per session and answers a repeat with the
same result without playing the message a second time, which is the whole reason Retry is safe.
And `session.subscribe` has a replay bound — 200 events here, where the gateway keeps 2 000 or
4 MiB, deliberately small so any session with history can reach it — answering `resync: true` with
no events for a cursor the buffer no longer covers, which is what drives the app's resync branch:
blank the timeline, keep the unconfirmed sends, reload from history.

## Structure

```
src/
  protocol/    wire types and frame shapes
  lib/         HTTP client, app socket, formatters
  stores/      zustand stores; timeline.ts is the pure event -> renderable-items reducer
  components/  buttons, modal, drawer, popover, segmented control, status dots
  layout/      the signed-in shell, the mark, and the landing rule
  features/    login, devices, sessions, chat, voice, settings, users
  push/        Web Push subscription and service worker registration
  styles/      tokens.css and base.css
mock/          the mock gateway
tests/         vitest suites
```

Every user-visible string lives in `src/strings.ts`, the English table and the type; the Chinese
table beside it is `src/strings.zh-Hans.ts`.

The `session.subscribe` summary owns the current session state, turn, metadata and counters.
Buffered events rebuild the transcript and chat details without projecting an older state back
onto that summary. History also leaves the summary untouched; its first page restores chat
details only if no live event arrived while the request was pending. Live events continue to
update the summary. `last_seq` is an event cursor, not a revision of the summary: after a device
reconnect the gateway may have a fresh idle summary and an older running event in its buffer.

## Language

The app speaks English or 中文 (`Settings → Language`), and it starts in English whatever
`navigator.language` says — a developer whose system is Chinese still reads the agent in English,
and a surprise translation at first launch reads as a different product. The choice is
`language` in the settings store, persisted with the other preferences under the signed-in
account's key (`rc.settings.<username>`, A24), and `<html lang>` follows it.

`strings` is not a table but a view on one: a proxy that reads `stringTables[language]` on every
property access, so a component keeps writing `strings.composer.send` and needs to know nothing.
Two things follow from that. A change re-renders every open screen because `App` carries the
language as the key of its `<Routes>` — the socket and the session probe sit above it and are not
disturbed. And **no module may read a string at import time**: a table built at module scope
(`const TABS = [{ label: strings.nav.devices }]`) keeps the language the app booted in.
`src/layout/AppLayout.tsx` and `src/lib/errors.ts` build theirs inside a function for exactly that
reason.

`src/strings.zh-Hans.ts` is typed `StringTable`, so the compiler refuses it the moment a key is
added or renamed in English, and `tests/language.test.tsx` walks both tables to catch a missing
nested key that the type cannot see. The functions in the table (`archiveGroup(n)`,
`status.workingQueued(agent)`, `format.minutesAgo(n)`) are translated as functions, which is how
"3m ago" becomes "3 分钟前" while the number stays where it is.

Only the app's own words are translated. Everything the device reported — agent output, device
names, paths, branches, and the model, effort, permission and speed **labels** the agent's own
lists carry — is drawn as it arrived, so the composer's chip reads "Sonnet 4.5 High" in both
languages. The product is SideCode in English and 随码 in Chinese; "Claude Code", "Codex", "Grok
Build" and "pi" stay in Latin script, and each of the two interface languages is always named in
its own script.

## The icon

`public/icon.svg` is the source: a near-black rounded square with three white dots at the corners of
a downward-pointing triangle, joined by grey bars that stop short of the dots. It matches the iOS
`AppIcon` and `src/layout/Mark.tsx`, the same drawing inlined for the topbar, the login card and the
chat sidebar. `icon-192.png`, `icon-512.png` and `icon-maskable-512.png` are rendered from it in a
headless Chrome; the maskable one is full bleed with the artwork inside the central 80 %. Change the
SVG and re-render all three together.

## Voice

Tapping the mic opens `WS /ws/stt` with no query — the gateway's provider detects the language (A44,
below) — captures the microphone through an AudioWorklet with a ScriptProcessor fallback,
downsamples to 16 kHz mono PCM16LE, and sends roughly 120 ms binary frames. Partial transcripts
stream into the composer's own field. The control row then holds a waveform, an elapsed timer and
exactly one thing in Send's slot, at Send's size: the button **Done**, disabled until the socket is
listening. Done sends `stt.stop` and leaves the transcript in the field — nothing is ever sent by
the act of stopping the recording — and the click is answered in that same slot at once. The capsule
gives way to Send's pill holding a spinner (`WorkingPill`, the class `.working-pill`), which is not
a button and not a disabled one either, so nothing in the row takes a click while `stt.final` is on
its way; the status line reads "Finishing the transcript" until it lands, and the elapsed clock
stops at the click, its interval running only while the state is `listening`. There is no Cancel — a
dictation you do not want is Done and then edited or cleared like any draft — and no time limit; a
long dictation is cut into segments whose transcripts are joined in order. Reaching for the field
takes it back and stops listening, keeping the words recognised so far: a keystroke does it, and so
does a pointer down on the field itself, which is how a person stops the dictation to read what was
said. The field's own programmatic focus — taking a command row, say — is not a pointer and ends
nothing. The mic is hidden entirely when the gateway reports `stt.enabled: false`.

**While dictation runs, the field follows the words** (`docs/DESIGN.md` § "The composer"). The
field grows with its content to 220 px and scrolls inside after that, and a dictated write brings
no caret with it, so a long transcript used to sit on its first screen until Done. Each transcript
write — every partial, and the final one that lands as the run ends — now sets `followTail` in
`Composer.tsx`, and the effect that measures the field reads the flag once and puts the last line
in view by setting `scrollTop`, never animating it. Typing sets nothing, because a caret keeps
itself visible, and neither does a polished answer or an Undo: those are the person's draft again.

## Dictation polish (A29)

A finished dictation can go through the model the gateway operator configured before the person
reads it, on the person's own switch. The gateway says whether it can (`hello.polish.enabled`,
kept in the connection store, `{enabled: false}` by default and on an older gateway), and the
Settings page's Voice group offers three things only when it can: the switch "Polish dictation
with AI", a model menu filled from `GET /api/polish/models` when the rows render with the switch on
(the first model is chosen when none was; a failed fetch keeps the menu and puts "The model list
could not be loaded." in the Model row's own line), and a Moderate / Strong control. The switch's
row says what leaves the browser and when: what was dictated and the last few messages, sent to
the gateway's model only while the switch is on. A gateway without the feature shows the switch
disabled with "This gateway has no polish model configured" as that row's sentence. The three
values live in the settings store per account
(`polishEnabled`, `polishModel`, `polishStrength`).

In the composer the words the recogniser produced land the instant dictation ends, exactly as
before; the status line then reads "Polishing…" while `POST /api/polish` is out with the dictated
span alone — the `dictation` bookkeeping already knows where it starts — the model, the strength,
`language: "auto"` (the gateway transcribed the words and detected their language, A44) and the open
session's last twenty `user_message` / `assistant_text` blocks, oldest first, each trimmed to 4000
characters (`src/features/voice/polish.ts`, pure). The answer replaces only that span, never a
character the person typed, and "Polished · Undo" sits under the field until the next edit or send;
Undo puts the dictated words back. A failure leaves the words and shows "Polishing failed, your
words are unchanged" for a few seconds. While the request is out the spinner Done became stays in
Send's slot: Send is not drawn, Enter does nothing — it is Send, so it waits with it — and the `⋯`
menu beside Send, whose only item is a send, is not drawn either. The slot becomes Send the moment
the field holds what will be sent: the polished words when the answer lands, the dictated ones when
the request fails, and the person's own the instant they type over the wait, which drops the
request. Which of the three the slot holds is one pure function of the dictation state and the
polish phase (`src/features/voice/primarySlot.ts`), read by the listening row and the ordinary row
alike. Nothing is ever sent by itself: polished text is a draft like any other.

**The mock** reports `polish.enabled: true`, serves two models, and polishes with a 600 ms delay by
dropping "um"/"uh", merging doubled words, capitalising and closing the sentence, so the whole flow
can be watched; its speech-to-text final transcript carries fillers on purpose. Tests cover the pure
helpers, the slot over every pair of phases, the composer flow (success, failure, the slot and the
Enter key while the request is out, a typed edit that drops it, undo, both disabled cases), the
settings group in both gateway states, the per-account keys and the three protocol fixtures.

## No dictation language on the web (A44)

The ruling is `docs/DESIGN.md` § "The control row" and the Voice row of § "The Settings screen";
this is what it changed here. The web only ever transcribes on the gateway, and the gateway's
provider detects the language of every transcription, so there is nothing to choose:

- The composer's control row is, from the leading edge, the Up next chip (only while something is
  queued), the model card and the permission-mode picker (`ComposerBottomRow` in
  `src/features/chat/Composer.tsx`). The language menu that followed the permission mode is gone,
  with the `sttLanguages` prop chain from `ChatPage` down to it. The web keeps the words; the phone
  draws the same row as icons.
- `SttSocket` opens `WS /ws/stt` with no query at all, and `useVoice` takes no language. The polish
  request always says `language: "auto"`.
- Settings → Voice starts with **Transcribe**: the word "Gateway" at the trailing edge, drawn as
  state (`pill quiet`) rather than as a control, over the sentence "Your gateway transcribes and
  recognises the language itself.", or "This gateway has no transcription service configured."
  while `stt.enabled` is false. The row is not a target; there is nothing to open.
- The settings store has no dictation language: `sttLanguage`, `setSttLanguage` and the
  `stt_language` entry of the synced keys (`src/stores/preferenceFields.ts`) are gone, so the web
  never reads `stt_language` from `hello` or `preferences.updated` and never writes it up, not even
  once for an account that has not set it. It is the language an iPhone's own recogniser listens
  for, and the iPhone owns it; `Preferences.stt_language` stays in the wire types. The connection
  store keeps `stt.enabled` alone, since `stt.languages` is always `["auto"]`.
- The mock reports `stt.languages: ["auto"]` in `/api/config` and `hello`, and its `/ws/stt` reads
  no query, so a `language` an older app sends is ignored as the gateway ignores it. It still keeps
  an `stt_language` a `PATCH` carries, as the gateway does.

A browser that stored a dictation language before A44 keeps the old key in its settings entry in
`localStorage`; nothing reads it.

## Messages from other agents (A30, A34)

A `user_message` whose `source` is `agent` — a teammate's report or a task notification the Claude
CLI filed as a user turn, reduced by the device to who said what — is not the person's side of the
conversation, so it is not drawn in their bubble at all. `Timeline`'s dispatch sends it to
`blocks/AgentMessageRow.tsx` instead of `UserMessageRow`: a muted block on the quiet surface, at the
full width of the content and on the left where the agent's own prose is, captioned "from another
agent" above the text (`chat.fromAgent`; the terminal caption is `chat.fromTerminal`, "terminal",
and both are translated). `UserMessageRow` keeps only the person's own messages and has no variant
left for an agent to borrow. `Trigger` is one exported union for `source` and
`turn_started.trigger` — `remote | terminal | queue | agent` — and nothing in the stores switches on
it beyond the type, so an `agent`-triggered turn behaves as a terminal one. The mock's terminal
Claude session carries one such message, so the block and the bubble can be compared on one screen;
the optimistic-send reconciliation only ever matches `source: "remote"`, so an agent row is never
mistaken for the browser's own echo.

These are the agent's workings rather than something written to the person, so **Simple does not
draw them**, and the jump-to-latest count does not count them there. `drawnAt` in
`src/stores/timeline.ts` was keyed by `kind` alone; it now reads `source` as well, through
`isAgentMessage`, and both the rows and the count follow from that one function.

## Paused by the usage limit (A35)

A Claude Code or Codex turn the vendor's five-hour or weekly window ended closes with
`stop_reason: "error"` and `limit {window_minutes, resets_at}`, and the device schedules a resume for
a minute after the reset when the account has asked for one. Four surfaces carry it, and one small
pure module carries every word.

**The switch is the account's, not the browser's.** `src/stores/preferences.ts` holds
`Preferences | undefined`: `hello.preferences` seeds it, a `preferences.updated` frame replaces it,
and a change is written with `PATCH /api/preferences` and rolled back if the gateway refuses.
`undefined` means a gateway older than A35 — not "off" — and Settings then draws the switch disabled
with "Your gateway does not offer this yet." as its row's sentence rather than as a choice that could
be made. The store
joins `signOut()` like every other store that holds something of an account's. Everything else in
Settings is still local to the browser in `stores/settings.ts`; this one row is the exception, which
is why it lives in its own store rather than in that one.

**The notice sits above the transcript.** `ChatPage` draws `ResumeNotice` between the header and the
timeline whenever `session.resume` is set, and nothing else changes: the status dot still reads the
session's state, which is `idle`, because the pause is what the notice is for. It reads "Paused by
the usage limit · resumes 3:50 PM", with "about" when the device estimated the time and "second
try" / "third try" appended from `attempts`. **Change** opens a popover holding one
`datetime-local` field prefilled with `at` and bounded to between a minute and eight days away; the
form is `noValidate`, so the refusal is this app's sentence rather than the browser's own bubble,
which no two browsers word the same. **Cancel** sends `session.resume_cancel` at once, with no
confirmation. Both answer with the session, and nothing is guessed in between: a failure leaves the
notice where it was and says why.

**The timeline says what happened.** `TurnEndRow` draws a `limit` end as a notice — "Ended at the
usage limit · resets 3:50 PM", and without the time when `resets_at` is null — rather than as the
red "Turn failed" an ordinary `error` stop gets. `ResumeRow` draws the device's steps in the same
voice, and `fired` draws nothing at all: `isRenderable` in `stores/timeline.ts` drops it before it
becomes a block, because the moment of resuming is the prompt in the person's bubble. That prompt is
a `user_message` with `source: "resume"`, drawn by `UserMessageRow` under the caption "Sent for you
after the limit reset", and a `turn_started` with `trigger: "resume"` moves the session exactly as a
remote turn does. None of these are the agent's workings, so **Simple keeps all of them**.

`src/features/chat/resume.ts` is the one place the words and the bounds live: `timeText` (the
viewer's zone, the clock face of the interface language, the date added when it is not today),
`resumeNoticeText`, `limitEndText`, `resumeRowText`, `resumeBoundError` and the two
`datetime-local` conversions, which go through the local calendar rather than `toISOString` so the
field never shows a UTC minute. All of it is pure and takes the clock as an argument, so
`tests/resume.test.ts` reads a fixed afternoon rather than whatever time the suite runs at.

## A red dot and the app icon badge (A47)

The ruling is `docs/DESIGN.md` § "A red dot for a session that stopped and waits for you"; the wire
is `Session.unseen`, `session.seen` and `rc.badge` (PROTOCOL §4.4, §6.2, §3.7). The mark is the
gateway's. The app never sets or clears it: it draws what the last `hello`, `session.updated` or
subscribe reply said, and it tells the gateway when the person has looked.

**The dot.** `src/components/UnseenDot.tsx` draws it on the row of every session whose `unseen` is
true — `SessionRow` on the Sessions page and `SidebarItem` in the chat sidebar, which became a
component of its own because each row needs its own id. It is the `.unseen-dot` primitive in
`ui.css`: 8 px (`--unseen-dot`), `--danger`, and absolutely positioned inside the title's line, so it
is centred on that line and moves nothing when it comes or goes. Each list says where its gutter's
centre is with `--unseen-reach`, the distance back from where the title's line starts: half the
Sessions row's leading padding (20 px, 16 px at 640 px and below), and in the sidebar half of a new
20 px leading padding plus the status dot (`--status-dot`, 7 px) and the gap after it. The sidebar's
rows keep that gutter whether or not a dot is in it, so their status dot and text sit 12 px further
in than before A47 and the dot touches neither the row's edge nor its status dot. The dot itself is
`aria-hidden`; the row's button names a hidden sentence beside it with `aria-describedby`, so a
screen reader hears the row and then "not yet opened" / "未查看" (`strings.sessions.unseen`), never
the words in the middle of the row's name.

**Telling the gateway.** `src/features/chat/useFrontConversation.ts`, which `ChatPage` calls, knows
when the conversation on screen is in front of the person: `src/lib/useInFront.ts` is true while
`document.visibilityState` is `visible` and `document.hasFocus()`, read again on `visibilitychange`,
`focus` and `blur`. While it is, the hook calls the sessions store's `markSeen`, which holds the
protocol's rule: `session.seen {session_id}` goes only while this tab's copy says `unseen`, once per
session at a time (a module-level set that `reset()` empties), and a failure is swallowed. The hook
asks on every occasion the ruling names — the conversation opening on a page in front, the page
coming to the front with it open, a `session.updated` marking it while it is on screen — and when the
socket comes back, since a `hello` may bring the mark. Nothing clears the copy but the gateway's own
`session.updated`, so a request that failed goes again the next time one of those moments comes;
there is no retry timer.

**The conversation in front draws no dot.** `src/stores/front.ts` holds the key of the conversation
in front, or null: `useFrontConversation` sets it while the page is in front and takes it back when
the page leaves the front or closes. The sidebar leaves that row's dot out and the badge does not
count it, so a turn that ends while the person watches never draws a dot or moves the icon's number,
not even for the round trip of its `session.seen` — which is what "a session already on screen when
its turn ends never gets one" asks of the screen. With the window behind another, the same row shows
the dot until the window comes back. This is the one place the web draws less than the copy says;
the copy itself stays the gateway's.

**The badge.** `src/push/appBadge.ts` is the one place that calls the Badging API: `showAppBadge(n)`
is `navigator.setAppBadge(n)`, or `clearAppBadge()` at zero, and nothing at all where the browser
offers neither; a refusal — a page that is not installed, badges switched off — is swallowed.
`src/push/useAppBadge.ts`, called from `App`, keeps the badge at `countUnseen(sessions, front)`, the
unarchived sessions with the mark less the conversation in front, from the moment the sessions store
is `loaded`, so a badge the service worker set from a push stands until the page has the account's
list. `signOut()` clears it, and the service worker sets it from every push's `rc.badge` (§ "Push and
the service worker").

**The mock** behaves as the gateway. `mock/unseen.ts` holds the rule — working is `starting` or
`running`; waiting is `needs_approval` or `needs_input`, or `idle` or `readonly` with a control other
than `none` — and `moveState` is the only way `server.ts` changes a session's state, so every
scripted `status`, the takeover and the close go through it: working to waiting marks, working again
clears, any other move leaves the mark as it was. `clearMark` serves `session.seen` and the close,
which archives. The field travels only while it is true, as the protocol's own fixtures carry it.
`session.seen` answers `{}` and then, when there was a mark, sends `session.updated` to every socket
of the account; another account's session and an unknown one are `not_found`. `ses-vite` starts
marked, so the dot and a badge of 1 are there on the first sign-in.

Tests: `tests/unseen-dot.test.tsx` (the dot on both lists, its line, its description in both
languages, the conversation in front, the CSS that places it), `tests/session-seen.test.tsx` (when
`session.seen` is sent and when not: opening, focus, visibility, a mark arriving on screen, the
socket coming back, one at a time, a silent failure and the next occasion, the front store),
`tests/app-badge.test.tsx` (the count, the Badging API through a fake `navigator`, the hook, the
conversation in front, sign-out), `tests/service-worker.test.ts` (the badge from a push, the `badge`
kind), `tests/mock-unseen.test.ts` (the mock's rule over the whole state table),
`tests/ws.test.ts` (the request's frame against its fixture), `tests/signout.test.tsx` (the
conversation in front forgotten) and `tests/protocol-fixtures.test.ts` (the A47 fixtures).

## Push and the service worker

`public/sw.js` is registered in production builds only. It is network-first for navigations,
cache-first for hashed assets under `/assets/`, and bypasses `/api`, `/ws`, `/install.sh` and
`/dist/`. Only successful same-origin responses are cached, so a gateway error page never becomes
the offline shell. A push payload carries only a device name and a reason; clicking the notification
focuses an open tab or opens `/sessions/<device_id>/<session_id>`.

The line the notification shows is `rc.title`, which the gateway writes in full; the worker's own
`TITLES` table is the fallback for a payload that carries none, and it knows the three A35 kinds —
`limit_reached`, `resumed` and `resume_dropped` — alongside the four older ones. A kind this build
has never seen still says a device needs attention and still deep-links to its session, so a tab
left open across a gateway upgrade keeps working. `tests/service-worker.test.ts` evaluates `sw.js`
against a stand-in `self` and drives both handlers, because the file is plain JavaScript the browser
loads on its own and nothing else in the suite would reach it.

Every push also carries `rc.badge` (A47), how many of the account's unarchived sessions have the red
dot, and the worker sets the installed app's icon from it (`self.navigator.setAppBadge`, cleared at
zero) beside the notification, which is how the number holds while no page of the app is open. A
payload without a whole, non-negative count leaves the badge alone, a browser without the API shows
the notification as before, and a refusal never stops it. A push of kind `badge` changes the count
and shows nothing; the gateway sends that kind to APNs only, but it means the same here.

## Accounts

Every person on a gateway has an account (A24), and what an account sees is its own: its devices,
their sessions, its pairing codes. The app never has to filter anything — the gateway answers only
what belongs to the caller — so the work here is the three screens the amendment adds and one rule
about what this browser remembers.

**Signing in** asks for a username and a password. The username is remembered in `localStorage`
under `rc.username` and prefilled next time, so the second sign-in is the password alone;
`localStorage` is already scoped to the gateway's origin, which is what makes one key enough.
A `403` reads "This account is disabled." and a `401` says only that one of the two was wrong.
`LoginPage` is the only screen that reads `GET /api/health`'s `auth.registration_open`, because it
is the only screen that offers **Create an account**: the link appears under the button when the
gateway takes registrations, swaps the card for username, password and **Create account**, and puts
"Sign in instead" beneath it. Registering is a sign-in. A refusal is worded from its status —
`409` taken, `400` the username and password rules, `403` registration closed, which also removes
the link.

**The Settings header and the Account group.** The header (`features/settings/IdentityHeader.tsx`)
says who is signed in and where: the initials and the gateway host come from `lib/identity.ts`,
the one rule the top bar uses too, and the connection dot's tone from `connectionTone.ts` (green
for an open socket, pulsing amber while it connects or reconnects, grey otherwise; a browser the
gateway refuses is signed out, so the ruling's red never arises here). The Account group
(`AccountGroup.tsx`) holds the rows that account has, and the two are independent because the
gateway's own rules are: **Users** appears for the admin role, and **Change password** (a modal
asking the current password and the new one; `401` reads "That is not your current password.")
appears for every account except the built-in `admin`, whose password is the gateway's
`RC_PASSWORD` and which `POST /api/password` refuses by username. A second account created with the
admin role therefore gets both rows. **Sign out** stays last and asks first (`ConfirmDialog`), with
the row's own sentence as the dialog's body. Every row on the screen is `SettingsRow` or
`SettingsActionRow` (`SettingsRow.tsx`): a title, one sentence, the control at the trailing edge,
no hairline; a state — push blocked or unsupported, no polish model, no transcription, a refused
preference — replaces the sentence and disables the control rather than adding an element.

**Signing out empties the tab.** `signOut` in `src/stores/signOut.ts` is the one place that knows
the list: it closes the socket, puts the connection store back to what no `hello` has confirmed
(`stt`, `polish`, the gateway version and the protocol number included, so the composer never
offers a capability this account has not been told about), and calls `reset()` on every store that
holds something of an account's — chat, drafts, outbox, answers, commands, sessions, the
conversation in front (A47), devices, users — and clears the app icon's badge, which counted that
account's sessions (A47). `App` calls it both for the Sign out button and for a `4401`/`4403` close. Before this, all
of it stayed in memory until the page was reloaded, including the value typed into a question field
whose own placeholder says it is not stored; on a shared browser the next person had it. The
device and session lists go with the rest, so the landing rule waits for the new account's `hello`
instead of deciding from the previous account's list.

**`/users`** is `src/features/users/`, gated on the role in the auth store: a member who types the
address is sent to `/sessions` before anything is fetched. The registration switch sits at the top
and answers the tap before the `PATCH /api/registration` lands, going back if the gateway refuses
it. Each row carries the username, `role · state`, the device count and the last sign-in as a
relative time or "never"; the `admin` row is given no menu, because none of its three actions is
allowed. Delete names what goes with the account ("…and its 2 devices"). Every dialog surfaces its
own refusal, read from `error.code` by `userErrorText` in `src/lib/accountErrors.ts`; `conflict`
means a taken username on `POST /api/users` and a refusal to touch `admin` everywhere else, so each
caller supplies that one sentence. The list is `src/stores/users.ts`: nothing pushes accounts over
the socket, so it is whatever the last `GET /api/users` said, and every write re-reads it.

**The app's own preferences belong to the account, not to the browser.** The settings store
persists under `rc.settings.<username>`, and `readSettingsFor` in `src/stores/settings.ts` points
it at the signed-in account whenever that changes — `App` calls it from the auth store's username.
The persist `merge` lays the defaults under whatever was stored, so an account that has chosen
nothing reads the defaults rather than inheriting the last person's language, polish choices or
timeline detail. The login screen, where nobody is signed in, keeps its own key, `rc.settings`.
Nothing of this reaches the gateway.

## Behaviour worth knowing

- **A draft belongs to its session** (`docs/DESIGN.md` § "The composer"). The words and the files
  live in `src/stores/drafts.ts`, keyed by `<device>/<session>`, for the tab's life and no longer;
  the composer reads the entry its session names and writes back to it. The chat route keeps one
  `ChatPage` across a switch, so `ChatPage` also gives `<Composer>` the session key as its React
  `key`: that is what remounts it, which is what ends a dictation with the session it was spoken
  for — `useVoice` tears the microphone and its sockets down on unmount, and the words it had
  already recognised are in that session's draft. Nothing typed for one session can reach another,
  and a question waiting in the session being opened never sees the words meant for the one being
  left. A refused send hands the words and the files back to the session they were meant for.
- **The Enter that confirms an input method's text is not Send** (`docs/DESIGN.md` § "The
  composer"). `src/features/chat/useImeGuard.ts` owns the composition state the composer's two
  Enter handlers ask before sending. Chrome and Firefox mark the keydown itself (`isComposing`),
  and the composition events bracket it. WebKit — Safari on the Mac and on iOS — fires
  `compositionend` *before* the keydown of the Enter that confirmed the text, with `isComposing`
  already false (WebKit bug 165004), so that keydown alone cannot tell; both events come out of
  one key press, dispatched synchronously in one task, so the hook lets a composition that ended
  in the current task keep the Enter that follows it and drops the claim on a zero timer before
  the next task. A microtask would not do: React's delegated `compositionend` listener returns
  before the keydown is dispatched, and the microtask checkpoint runs between the two. Under a
  Chinese IME the bare `isComposing` check sends on the confirming Enter, and a send can still slip
  through with the claim in place, so the order is not the same everywhere — and the same-task
  claim cannot see a keydown that lands *before* its compositionend. A plain Enter therefore does
  not send at once: `sendUnlessComposing` runs the send `SEND_DELAY_MS` (60 ms)
  later, and a `compositionstart` or `compositionend` arriving in that moment cancels it. The
  command menu's Enter takes the same path, so a command runs a moment after the keystroke; the
  tests wait for it. `keyCode === 229` joins the checks for good measure.
- **Settings are the account's (A41).** `src/stores/settings.ts` keeps its shape, because the
  rest of the app reads it, but for five of the six preferences of A41 — interface language,
  polish on/off, model and strength, timeline detail — it is the cache of the account's value, not
  the source; the sixth, `stt_language`, is the iPhone's since A44 and the web neither holds nor
  writes it. `src/stores/preferences.ts` hands `hello.preferences` and every
  `preferences.updated` to the settings store (`fromHello`, `fromAccount`), which applies a field
  only when it differs, so a frame that echoes the store's own write changes nothing and writes
  nothing back; the five setters write locally and then `PATCH` that one field through
  `src/stores/preferenceWrite.ts`, the one place that talks to `/api/preferences`; right after
  `hello`, a field the account has not set is written up once from the local value, so an account
  upgraded to A41 keeps what its first tab had. `src/stores/preferenceFields.ts` maps the wire
  names to the store's and validates against the schema's word lists, dropping anything unknown.
  `collapsedDevices` and `archiveExpanded` stay local. A gateway without `preferences`, or without
  the five fields, leaves the store as it was. `InterfaceLanguage` and `TimelineDetail` live in
  `src/protocol/types.ts` now, being wire values. The mock stores preferences per account and
  broadcasts to every socket of it (`mock/preferences.ts`), so two tabs of one account stay in
  step.
- **The transcript is bounded.** `MAX_TIMELINE_ITEMS` in `src/stores/timeline.ts` caps a live
  timeline at 3 000 rows: past it the oldest go, `oldestSeq` moves forward with them and `dropped`
  counts them, which is how the chat store knows to set `historyHasMore` again — scrolling back
  pages them in as ordinary history. A page the reader asked for by scrolling is never capped, or
  paging back would fetch the same events for ever. `useChat.close` stamps a conversation with the
  order it was left in and keeps the three most recently closed; the rest are dropped with their
  slash-command lists, and a conversation that is open — `close` runs on every reconnect too — is
  never among them.
- **Every sentence a person reads comes from the string tables.** `lib/ws.ts` and `lib/gateway.ts`
  mint their failures with a code and an empty message, and `errorText` in `src/lib/errors.ts`
  turns the code into a sentence, falling through to the caller's own fallback when it knows none.
  The composer's two catch blocks go through it like every other surface; they used to render
  `err.message`, which for the app's own failures was English nobody could translate ("not
  connected", "no reply from the gateway", "connection lost").
- **A dictation ends when the composer stops accepting one.** `useVoice` watches `enabled` and
  cancels a run already in flight, so a device going offline or a terminal taking the session back
  closes the microphone instead of streaming audio to the gateway for the life of the tab. The
  words already recognised stay in the field. `SttSocket.start()` has a five-second connect
  timeout, and `cancel()` rejects a connect still waiting, so a gateway that accepts the TCP
  connection and then says nothing cannot leave the composer in `starting` with Done disabled.
- **The attachment cap is enforced where the list is written.** Two attach operations can be in
  flight at once — a pasted batch, then the file dialog before the paste has finished encoding —
  and each computes its budget from the count it started with, so the store's `addAttachments`
  applies `MAX_ATTACHMENTS` itself and reports what it had to drop. What the second batch has to
  say is added to what the first said rather than replacing it.

- **Where an open lands** is `src/layout/Landing.tsx`, the element behind `/` and behind the `*`
  fallback. It waits for the devices store's `loaded` — the socket's `hello` snapshot calls
  `replaceAll`, which sets it — drawing the same `boot` placeholder `App` draws while the session
  probe is out, and then navigates with `replace` to `/sessions` when the account has at least one
  device and to `/devices` when it has none. Deciding before the snapshot has synced would send
  every account to Devices for the length of a round trip. The rule runs once per open because the
  navigation unmounts it: somebody who then opens Devices on an empty account stays there, and a
  device arriving later moves nobody. `LoginPage` returns to `/` when there is nothing to go back
  to, so signing in lands by the same rule; a remembered `from` still wins, fragment and all (A23).
- **The session list** is one selector, `selectSessionLayout` in `src/stores/sessions.ts`. It
  filters on the device, the agent and the search text, then returns one `DeviceGroup` per device
  that still has something to show: the device, whether the user folded it shut, its active rows,
  its own Archive and whether that Archive is open. Both the Sessions page and the chat sidebar
  render its result, so the two lists cannot drift; `tests/sessionLayout.test.ts` owns the rule.
  `docs/DESIGN.md` states it in full.
- **A session with no title still has a name.** `sessionTitle` in `src/strings.ts` turns an empty
  or blank `title` into "Untitled session", and the session row, the chat header, the chat sidebar
  and the search text `selectSessionLayout` matches on all read it, so an unnamed thread never
  draws a blank line above its meta and is still found by those words.
- **Collapse state** is two arrays of device ids in the settings store, `collapsedDevices` and
  `archiveExpanded`, persisted with the rest of the preferences under the signed-in account's key. Device groups
  are open by default, Archives shut. A non-empty search overrides both — it opens every device
  group and every Archive holding a match, without writing either array, and clearing the query
  hands the list back to what was stored. There is no global "show archived" toggle: it only ever
  toggled the hand-archived sessions, which the collapsed Archive already hid, so pressing it
  changed nothing on screen. The row's one action is **Close** (A39, `docs/DESIGN.md` § "Close,
  then the Archive"): on a `remote` row that is not archived, a circled cross that sends
  `session.archive {archived: true}` — after a `ConfirmDialog` when the row's dot tone is
  `working`, at once otherwise — and the device answers with the session closed, stopped and
  unowned, which is what puts the row in the Archive marked "Archived". The store's `close` is the
  only caller of the frame; nothing else archives or unarchives from the browser.
- **The agent filter** is `agentFilter` in the sessions store rather than in a page, so the Sessions
  page and the chat sidebar always show the same slice. It is not persisted, and its options come
  from `selectAgents`, the agents the loaded sessions actually run. A25 turned it from a segmented
  control into a menu beside the device one: an "All" segment plus one per agent was 27 px wider
  than a 400 px viewport, and a menu holds any number of agents at any width. With a filter on, the
  button shows that agent's logo and name.
- **Four agents, each drawn as its own logo** (A25, A26). `agentLabels` in `src/strings.ts` names
  Claude Code, Codex, Grok Build and pi, and an id it does not know is drawn as itself. The mark
  beside or instead of a name is `src/components/AgentLogo.tsx`: one inline `<svg>` per agent with
  the vendor's published vector (Simple Icons for Claude and OpenAI, pi.dev for pi, the Grok mark),
  `fill="currentColor"` and no colour of its own, in a box one em square sitting on the cap height
  of its line, so a logo is the size of the text it stands beside and inherits its ink. An agent
  with no vector falls back to the first letter of its id in the same box, which is what Cursor now
  gets (A26 withdrew it). The component is used in the new-session form's agent control, where the
  logo stands alone because the names do not fit side by side and the line under the row names the
  one that is chosen, in the agent filter's rows and its button, on the session-row chip and in a
  device row's agent list; every segment still carries its agent's name as its accessible name and
  its tooltip. Everything else reads the agent's own lists and draws nothing for an empty one: no
  permission picker in the composer and no permission row in the form for an agent whose
  `permission_modes` is empty, no effort slider and no effort word beside the model for one whose
  `efforts` is empty, so its model card reads the model alone. No shipped agent is empty since A26
  gave pi the three modes its extension enforces, so `tests/agents.test.tsx` holds those branches
  with an agent of its own that lists neither. Agent chips share one quiet tint — the logo and the
  name tell them apart, never a colour per vendor.
- **Popovers and menus** render in a portal on `document.body` and are placed against the viewport,
  flipping to the other side when the one asked for cannot hold the panel and clamping to the
  window's edges. Anchoring them to the trigger instead let a rounded list surface or a scrolling
  pane clip them. The geometry is pure and tested in `src/components/popoverPlacement.ts`; the
  layout effect that applies it, follows an ancestor's scroll and closes on an outside click lives
  in `src/components/Popover.tsx`. Being a portal also makes the panel a sibling of any open modal
  or drawer overlay rather than a descendant, so it takes the top of the layering scale in
  `tokens.css` — `--z-sticky` 20, `--z-overlay` 60, `--z-popover` 100. With the panel below the
  overlay the device picker inside the New session drawer opened behind the drawer and looked dead.
  The drawer's own outside-click check compares the event target with the overlay element, so a
  click inside the portalled panel never closes it.
- **The model card** (A21) is one chip in the composer row reading "<model> <effort>", with a small
  lightning before it while `session.speed` is set. It opens a popover above the composer holding
  two rows. The first has the speed toggle at its leading edge — drawn only when `agent.speeds` is
  non-empty, lit while a tier is on, and cycling standard → each tier → standard through
  `session.set {speed}` where `null` is the standard speed — then the model name, the effort word
  and a chevron; tapping the name replaces the card's contents with the model list rather than
  stacking a second popover on it. The second row is the effort slider, one stop per `agent.efforts`
  entry. Three rules hold the card together:
  - **The word follows the thumb.** The live stop is the card's own state, so the word beside the
    model name changes as the thumb moves rather than when the device echoes the change. The value
    is only sent when the thumb is released: React's `onChange` fires on every step, so the commit
    listens for the DOM's own `change` on the element instead. A change from anywhere else — a
    `/model` in the terminal, another tab — wins over the position the thumb was left in.
  - **Dots and a thick track.** The pill is `.effort-track`, 28 px tall, filled to the thumb in the
    accent colour with one 6 px dot at every stop, and the thumb is a 24 px white disc. The native
    `<input type="range">` is kept for the interaction and the keyboard and drawn transparent on top
    of it, which every engine needs told separately (`::-webkit-slider-runnable-track`,
    `::-moz-range-track` and `::-moz-range-progress`). The fill and the dots live in a layer inset
    by 12 px, half a thumb, because that is the span the thumb's centre travels; `--fill` is the
    percentage across it. The lowest stop draws no fill at all — its rounded cap would be the only
    thing visible, peeking out around the white thumb.
  - **A width that never changes.** `SizedBox` in `src/features/chat/SizedBox.tsx` puts the visible
    label and every `models × efforts` pair (`labelPairs` in `modelLabels.ts`) in one grid cell, the
    alternatives `visibility: hidden` and `aria-hidden`, so the box takes the widest of them. The
    chip and the card's name row both use it, with the ghosts drawn exactly as the visible label is
    — including the lightning whenever the agent lists tiers — so nothing beside them moves while a
    level or a model is chosen. Measured, never guessed: a number would go stale the moment an agent
    renamed a model.
  The slider's accessible name is "Effort" and its `aria-valuetext` is the word, which a screen
  reader announces as "Effort, Extra high"; the toggle names itself "Speed, Fast" or "Speed,
  Standard". An agent with no efforts draws no slider, one with no tiers draws no toggle, and the
  permission-mode picker follows the card in the row. On a session a terminal holds (A17) the same
  chip is drawn as a static value with the tier appended to its label, and it opens nothing.
- **Every change made from the card is drawn at once.** `ChatPage`'s `onSetOption` applies the patch
  to the stored session before `session.set` leaves (`applyOptions` in
  `src/features/chat/sessionOptions.ts`), so the lightning fills, the word changes and the model
  name switches on the click. The reply replaces it; a refusal puts the previous session back and
  shows the error — unless something newer has already replaced what was written, which is a
  `session.updated` that arrived in between or the reply itself. The check compares the store's
  current object identity against the one that was written, not its fields, because the patch and
  the update can carry the same value.
- **Devices update themselves; a person only retries** (A22, A36). Every row carries Rename and
  Revoke, and **Retry update** only while `update_state` is `failed`. `updateNotice` in
  `src/stores/devices.ts` is the one rule for what the row says about the client: "Updating…" while
  `update_state` is `updating`, "Update failed · <message>" for `failed`, nothing otherwise — the
  gateway asks devices to update on its own, so the app never says "Update available" and never
  shows a version. The confirmation still names what Retry would install ("Update <name> to
  <version>? Its service restarts; sessions it drives are stopped."), from `config.client.version`
  read once on boot with the rest of `/api/config`; a gateway too old to name the version of its
  wheel leaves `client.version` out and the confirmation falls back to "…to the gateway's client?".
  Retry confirms first,
  then sends `device.update {device_id, build}` with the gateway's build, never the row's. It is
  disabled with a title saying why while the device is offline, while an update is in flight, when
  the builds already match and when the gateway serves no wheel at all. A refusal the device sends
  back — a running session, a client installed from source — is not an `update_state`, so it is kept
  per device in the store's `updateErrors` and drawn in the same place until the gateway sends that
  device again.
- **Add device asks nothing it does not need.** The modal has no macOS / Linux picker: the installer
  tells the two apart itself with `uname`, so the command is the same on both. The gateway still
  hands out `install.macos` and `install.linux` — the same string today — so a platform whose command
  really differs can be added without a wire change, and `installCommand` in
  `src/features/devices/AddDeviceModal.tsx` is the one place that picks a key.
- **Pairing has two ways in** (A23). The Add device modal mints a code as before and now carries a
  second block, "From your phone", with the one-liner the scan flow uses: the host asks the gateway
  for a claim token and prints it as a QR code encoding `<origin>/pair#<token>`. `/pair` claims that
  token with `POST /api/pairing/requests/{token}/claim` and then shows the handshake the modal
  shows, because the gateway mints the host an ordinary pairing code and `pairing.progress` follows
  it. Claiming spends the token, so `PairPage` claims once per token rather than once per mount — an
  effect that ran twice would answer its own first request with "This code was already used." Signed
  out, the login screen keeps the whole path including the fragment, since the token lives there;
  `rememberedDestination` accepts only a path inside the app.
- **Reconnect** backs off exponentially to 5 s, replies to `ping`, and treats 60 s of silence as a
  half-open socket. Subscriptions are re-issued with the latest `since_seq`.
- **Close codes** 4401 and 4403 end the session and return to login; every other code reconnects.
- **Sending** is always `mode: "auto"`; the device decides between send, steer and queue, and the
  button label follows that decision. "Interrupt & send" is a separate, explicit action. The one
  exception is an edited queued message, which goes back with `mode: "queue"` (A43, below).
- **A send shows up immediately** (amendment A12). The app mints the `session.send` request id, and
  the device echoes it as the `user_message` block id, so the composer clears and the bubble is in
  the timeline in the same tick as the click — no waiting for the round trip. It renders dimmed with
  a quiet "Sending…" chip until the device's event replaces it under the ordinary replacement rule,
  so nothing moves and nothing is duplicated. The pending rows live in `timeline.optimistic`, apart
  from the device's blocks: they carry no `seq`, never move the replay cursor, always sort last, and
  survive a resync. `accepted: "queued"` takes the row away again, because the Up next list stands
  for the message until the device dequeues it under that id and it lands as an ordinary block; a
  refusal the gateway is certain about also takes the row away, and hands the
  draft back if nothing was typed since; a minute with no device event turns the chip into
  "Delivery unconfirmed". A device that still mints its own block id is reconciled by `text` and
  `source: "remote"` instead, one row per event.
- **While a question is pending the button reads Answer** (A20). The composer finds the pending
  `question` block with `selectPendingQuestion`, the status line stays "Waiting for your answer",
  and submitting sends `session.answer` rather than `session.send`: the draft becomes the free-text
  answer of the first question with no option selected, beside whatever the card holds for the
  others. Nothing is queued behind a question and no optimistic row is drawn, because an answer is
  not a message. The card's own working state lives in `src/stores/answers.ts`, keyed by
  `request_id`, so the card and the composer submit the same thing; the rules are pure in
  `src/features/chat/answering.ts`. Answer is disabled when the draft has nowhere to go — the first
  unanswered question refuses free text, or every question is already answered on the card, which is
  submitted from the card.
- **Uncertain delivery** is never resent automatically. The composer offers a Retry that reuses the
  original request id.
- **The composer is gated on `control`**, never on `state`.
- **A session's status dot** is toned by `dotTone(state, control, online)` in
  `src/components/dotTone.ts`, never by the state alone: a finished turn on a live session and a
  session whose CLI exited both report `idle`. The five tones and when each applies are the table in
  `docs/DESIGN.md`; `tests/dotTone.test.ts` walks all of it. The looks are five rules in
  `src/components/ui.css`: `working` is a still green, `waiting` an amber that pulses, `live` a
  still amber, `off` grey and `failed` red — green says the agent is working and needs nobody, amber
  says there is something for you, and only `waiting` animates, which the reduced-motion block
  stops. `tests/StatusDot.test.tsx` reads those rules and the class each tone renders. The tooltip
  names the tone, so `live` reads "Done". `OnlineDot`, the device's own dot, is not part of it: it
  is green when the device is online, a grey ring when it is not, and pulses only while the device
  updates itself.
- **`control: "shared"`** (amendments A10 and A11) is a live terminal session the device is
  attached to. It behaves like `remote`: the composer, the queue and approvals all work. What the
  attachment cannot carry is hidden rather than disabled, and "Take over" never appears, because
  there is nothing to take over. Three optional agent booleans say what it carries —
  `shared_interrupt`, `shared_settings` and `shared_attachments` — each defaulting to false.
- **Long output folds** beyond 20 lines, and `output_truncated` adds "Open full output", which
  fetches the untruncated block.
- **Reading position** holds: the timeline auto-follows until you scroll away, then counts new
  blocks behind a "Back to latest" button, centred at the foot of the transcript above the composer
  and widening into a capsule around that centre when it carries a count. One click lands at the
  very end of the transcript and the button leaves.
- **Failed actions** surface in a dismissible banner above the composer rather than failing silently.

## Shared terminal sessions

A session whose `control` is `shared` is driven by a live CLI the device is attached to, so the app
can inject prompts and answer permission prompts without killing the process. A Claude terminal is
attached through the channel shim; a bare `codex` TUI is attached through the shared app-server
daemon, which carries far more.

A control the attachment cannot drive is **hidden, not disabled with a reason**. Nothing above the
composer repeats what the header already says, so an attached session carries no bar of its own: the
status line is left to a running turn's steer or queue notice, "Controlled by the terminal · take
over to send" and "Device offline".

| Surface | Behaviour |
| --- | --- |
| Composer | Enabled, exactly as for `remote`. Send label and queue are unchanged |
| Take over | Never shown. The device is already attached |
| Stop | Shown only when the agent lists the `interrupt` capability **and** the device reports `shared_interrupt`. A Claude channel reports it since A42, because the device types Escape into the terminal; where it is false, Stop and "Interrupt & send" both disappear |
| Model / permission mode / effort | Shown when the agent reports `shared_settings`; otherwise not rendered at all, and nothing explains their absence. The title is always editable |
| Attachments | Shown when the agent reports `shared_attachments`; otherwise the button and its file input are not rendered, and pasted files are ignored quietly |
| Attached bar | None. The header's "terminal · attached" is the only place the attachment is named |
| Status label | "terminal · attached" in the sidebar and the Sessions list; the dot tones `shared` exactly like `remote` |
| Approvals | Whatever `options` the block carries. A Claude relay sends Allow and Deny; the Codex daemon sends up to four decisions |
| Questions | Answerable, exactly as for `remote` (A20). The composer's button reads Answer while one is pending, and a question the terminal answered first reads "Answered in the terminal" |

The three booleans are independent and read straight off `AgentInfo`, so no surface needs
agent-specific logic:

| Agent | `attach` | `shared_interrupt` | `shared_settings` | `shared_attachments` |
| --- | --- | --- | --- | --- |
| Claude, through the channel shim | `channel` | true (A42) | true, `model` and `effort` only (A40) | true (A49) |
| Codex, through the app-server daemon | `daemon` | true | true | true |

Sending uses the protocol's own modes. On a running shared session `auto` steers when the agent
lists `steer` or reports `shared_steer` (A51: the device hands the message to a running Claude
turn at its next batch of tool calls) and is held otherwise, `queue` is always held, and
`interrupt` ends the terminal turn and starts a new one, so "Interrupt & send" needs the same
`shared_interrupt` as Stop. The composer then says "Message will steer the turn…" with a "Send"
button when the session can steer, and "Message will be queued…" with a "Queue" button when it
cannot, matching the status line either way. `canSteer` in `attach.ts` is the one rule both read;
`shared_steer` counts only on a `shared` session, so a Claude session the device started still
queues.

Control changes arrive as a `meta` event carrying `control`, with a `status` event only when the
session state moves as well, so the app never infers the owner from a status change. The transitions
are `terminal → shared` when the attachment registers, `shared → terminal` when it drops, and
`shared → none` when the CLI exits.

### Approvals answered elsewhere

Both sides of a shared session see the same permission request, and whoever answers first wins. When
the terminal answered, the block resolves with the reserved decision `{option_id: "elsewhere", by:
"terminal"}`, which matches none of the offered options on purpose. The card then reads "Answered in
the terminal" rather than falling back to the raw option id. Every other resolved card still names
the option and who decided it.

### Questions answered elsewhere

A question an attached Claude Code session asks exists twice at the same moment: as the CLI's own
dialog in the terminal, and as a card here (A20). Either can answer it and the first one wins. The
card is answerable on a `shared` session exactly as on a driven one, and a question the terminal
answered first resolves with `by: "terminal"`, which the card reads as "Answered in the terminal" —
the same wording an approval answered there uses. A resolved card shows what was answered: the
options that were picked, and the free text that was typed, unless the question was `secret`.

### A held message is a queue entry

A message sent into a shared session while its terminal turn is running is not a block at all
(A19). The device holds it, answers `accepted: "queued"`, and publishes a `queue` event naming it,
so the row from sending moves into "Up next" the way any queued message does. The `user_message`
appears only when the CLI takes it, with a `first_seq` after the output of the turn it waited for —
where the terminal draws it too. One `delivery` state is left on a bubble: "will be re-sent", for a
message the CLI read as mid-turn data. The device replaces the block under its original `block_id`
once the message lands, and the chip disappears.

A `terminal` session whose agent reports `attach` adds one secondary line under "Controlled by the
terminal", saying why this session cannot be driven from here:

| `attach` | `attach_ready` | Hint |
| --- | --- | --- |
| `channel` | `false` | "Start claude through the SideCode shim to control it from here" |
| `daemon` | `false` | "Run rc-client codex setup on the device to attach its Codex sessions" |
| either | `true` | "This terminal session was started without the attachment; restart it to control it from here" |

The "Take over" button beside the hint follows the agent's `takeover` capability, in the composer
bar and in the status line alike, and so do the words. The disabled field says only "Controlled by
the terminal", whatever the agent is; the status line adds " · take over to send" where the button
is, and stops at "Controlled by the terminal" where there is nothing to press. Grok Build and Codex
advertise no `takeover`.

The one line under the hint is worded per `attach` (`src/features/chat/attach.ts`): the shim for
`channel`, `rc-client codex setup` for `daemon`, `rc-client pi setup` for `extension`, and for
`leader` (A28) "Run rc-client grok setup on the device, then restart Grok to attach its sessions";
with `attach_ready` true every kind says the CLI was started without the attachment and must be
restarted. A shared Grok session needs nothing of its own: `shared_interrupt` shows Stop,
`shared_settings` the pickers, and `shared_attachments` the attachment button (A49).

### Settings the device can type (A40)

`shared_settings` used to be all or nothing, and for Claude it was nothing: the model card and the
permission chip on a shared Claude session were A17 values. Since A40 the device types `/model`,
`/effort` and `/compact` into the pseudo-terminal its shim owns, and `AgentInfo.shared_settings_keys`
names what it can type — `["model", "effort"]` for Claude. `canSetShared(agent, key)` in
`src/features/chat/attach.ts` is therefore per key: absent keys mean all four, `shared_settings`
false means none. The composer draws each setting one way or the other — the model card is live
when any of model, effort or speed is settable and decides row by row inside; the permission mode
stays the read-only chip — so nothing on the row fails when tapped. A change on a shared session is
drawn only once the device replies: the terminal has to confirm it, and the reply's `Session` is
what it now runs; `remote` sessions keep the optimistic drawing (A21). A `conflict` from
`session.set` or `session.command` is shown in the device's own words (`refusalText` in
`src/lib/errors.ts`), because only the device knows whether a turn was running or someone was
typing. The mock types too (`mock/typing.ts`): a change lands 1.5 s later, is refused with "the
terminal is busy; try again in a moment" while the session's scripted turn runs, and `/compact` is
listed, echoed and followed by a compaction notice.

### Stop on a shared Claude session (A42)

`canInterruptShared` needed no change: Claude's agent object now reports `shared_interrupt` (the
device types Escape into the pseudo-terminal, A42), so Stop appears in the header of a shared
Claude session exactly as on a shared Codex one. What changed is the refusal: a `conflict` from
Stop — "answer the prompt first" while an approval or a question is on screen — is shown in the
device's words through `refusalText`, because the canned sentence about taking the session over
names an action a shared session does not offer. The mock ends the scripted turn as `interrupted`,
refuses while its prompt is up and returns `{}` when idle (`mock/typing.ts` `promptOnScreen`).

## Up next and editing a queued message (A43)

The ruling is `docs/DESIGN.md` § "Up next"; this is where it lives. The composer draws no queue
rows. `src/features/chat/UpNext.tsx` puts one chip, "Up next · N", first in the control row (A44)
while the device's `queue` snapshot holds anything, and its popover lists the entries in snapshot
order: one line each with the full text in the tooltip, a paperclip and the count for an entry with
`attachments`, and an × that sends `session.queue_remove`. A `not_found` there puts "That message
has already been sent." in the banner above the composer — the sentence an edit shows under the
field, both from `queueRemoveText` in `src/lib/errors.ts` — never "Not found.". Tapping a row edits
it; an entry with files, a composer that cannot send (terminal-controlled, device offline) and a
field that is already editing one — one edit at a time — draw the row as plain text, so only the ×
acts.

The edit is part of the session's draft (`src/stores/drafts.ts` `editing`: the entry's `ts`, its
original words, the draft set aside, and whether the words are on their way back), so it survives a
switch to another conversation like the words do. `src/features/chat/useQueuedEdit.ts` runs the two
round trips. It sends `session.queue_remove` first and begins the edit only when that succeeds: the
field's words and files go aside, the entry's words come in with the caret after them, and a
`not_found` answer instead puts "That message has already been sent." on the composer's error line.
While editing, a strip over the field reads "Editing a queued message" with Cancel; the `/` panel
does not open and a pending question waits rather than taking the field. Send — Queue while a turn
runs, even for a steering agent, and the status line says queued to match — and Cancel, with the
original words, are `session.send {mode: "queue", queue_ts}` through `useChat.send`; the ⋯ menu's
Interrupt & send goes without `queue_ts`. The field keeps the words until the gateway answers, with
the slot's spinner and a read-only field meanwhile, so nothing goes twice: accepted or uncertain, the
edit ends and the aside comes back; refused, the words stay, the edit goes on, and the error shows
as for any send. `queue_ts` is kept on the outbox entry, so a Retry of an uncertain put-back sends it
again.

## Slash commands (A27)

Typing `/` into the composer of a session whose agent carries capability `commands` opens the same
list a terminal opens, above the field. Codex, Grok Build and pi carry it; Claude never does, and on
a Claude session `/` is an ordinary character with nothing to explain it.

**The list.** `src/stores/commands.ts` keeps one entry per `session_id`: the commands, when they
arrived, and whether a request is out. `ChatPage` calls `open()` when the conversation opens and
`refresh()` on the keystroke that opens the panel; `refresh` asks the device again only when the
last answer is older than `COMMANDS_TTL_MS` (60 s), was empty, or never came — the rule
`PROTOCOL.md` §6.3 states. A refusal is stored as an empty list rather than raised: the panel simply
does not appear, the way it does not for an agent without the capability, and the stamp keeps the
next keystroke from asking again at once. Nothing is ever asked for an agent without the capability,
so the gateway never answers `unsupported` on our account.

**Reading the draft** is `src/features/chat/commands.ts`, four pure functions the panel, the send
routing and the tests all share. `commandQuery` is the partial name while the draft is a slash and
nothing else — `/`, `/com`, `/skill:pdf` — and null the moment a space follows, which is what closes
the panel. `matchCommand` reads a complete first word and its argument, and returns null for a name
the session does not offer, so `/nonesuch do the thing` is sent as text exactly as a terminal treats
an unknown slash. `filterCommands` is a prefix of the name in the device's own order, and
`commandSections` groups the rows only when the agent distinguishes more than one group: Codex has
one source and draws no headers, pi draws Built-in, Prompts, Skills and Extensions.

**The panel** is `src/features/chat/CommandMenu.tsx` with its own stylesheet. It is not a popover —
nothing opened it, a keystroke did — so it is drawn in the composer's own flow, anchored to the
field's wrapper and never moving the field down. One row per command: `/name` in the monospace face,
the description after it, the argument placeholder at the trailing edge in the tertiary colour.
Eight rows, then it scrolls. The field keeps focus throughout: the list is a `listbox` the field
points at with `aria-controls` and `aria-activedescendant`, and a mouse press on a row is
`preventDefault`ed so the caret never leaves. ↑/↓ move the highlight and wrap, Tab and Enter take
the highlighted row, Esc puts the panel away until the next keystroke, and a click takes a row.
Taking a row writes `/name ` when the command takes an argument and `/name` when it does not, so the
second Enter runs it — the terminal's own second Enter. Once the first word is complete the panel
gives way to a one-line hint in the same place, naming the command and where its argument goes.

**Sending.** The composer's `submit` asks `matchCommand` first: a listed command becomes
`session.command {name, argument?}` through `useChat.runCommand`, and everything else stays
`session.send`. `runCommand` mints the request id, puts the row in the timeline under it before the
request leaves (A12, exactly as a message does), and marks it accepted when the gateway answers; a
refusal takes the row away again and the message reaches the composer's own error line under the
field, in the device's words. A command is never sent while a turn is running: the rows dim, the
panel grows the footer "Available when the turn finishes", and Send answers with that same sentence
inline rather than spending a round trip on a `conflict` it can predict.

**The output.** What a terminal would have printed comes back as a `tool_call` block whose `tool`
and `title` are the command itself. Two small rules follow: `selectView` in `src/stores/timeline.ts`
draws such a block at Simple as well as Detailed, because it answers what the person asked for
rather than being one of the agent's workings — otherwise `/usage` would run and show nothing — and
`ToolRow` prints the title only when it differs from the tool name, so the row reads `/usage` once.
A `compact` reports as a `notice`; a Grok command may answer nothing at all, and the composer never
waits for output.

**The mock** answers `session.commands` for its Codex, Grok Build and pi sessions from
`commandsFor()` in `mock/fixtures.ts` and nothing for Claude, and `commandScript()` in
`mock/script.ts` plays the echo and a plausible outcome: a `notice` for `/compact`, a `tool_call`
block for Codex's read-only commands, a short turn for the rest, and nothing but the echo for
Grok's `/context`, which renders in its own pager.

## A device row opens a terminal (A38)

Clicking a device row opens `/devices/:deviceId/terminal` when the device is online and
`Device.terminal` is true; otherwise the row stays put and says why in place ("This device is
offline." / "This device does not offer a terminal."). The row's menu reads Rename · Retry update
(only while failed) · Show quota · Revoke, and Show quota is the link to the device page
(`docs/DESIGN.md` § "A device has a page, and a device row opens a terminal").

`features/devices/TerminalPage.tsx` is the screen — a header with a back arrow, the device's name,
the status line (Connecting / Connected / Disconnected with Reconnect / Shell exited with New shell)
and **Close**, and xterm.js filling the rest — loaded lazily from `App.tsx` so the 336 KB emulator
stays out of the main bundle. `useTerminal.ts` joins the emulator to the session: on mount it fits
the terminal to its container and sends `terminal.open {cols, rows}`; `terminal.output` frames for
this terminal are base64-decoded (`lib/base64.ts`) and written, with `seq` watched for a gap;
keystrokes go out as `terminal.input`; a container resize is debounced into `terminal.resize`; a
lost socket shows Disconnected and, on reconnect, `terminal.attach` with the `terminal_id`
remembered per device in `sessionStorage` (`stores/terminal.ts`, cleared on sign-out), the
`scrollback` written before output resumes; `terminal.exited` shows the code and offers New shell;
Close, back or any route change sends `terminal.close`. `terminalTheme.ts` is the light ANSI
palette in the app's ink. `stores/connection.ts` dispatches the two push frames to the store.
Packages: `@xterm/xterm` 6.0.0 and `@xterm/addon-fit` 0.11.0, pinned. The mock's `mock/shell.ts` is
a fake shell — prompt, echo, `exit`, a 64 KiB ring, 16 ms coalescing, rising `seq` — and
`mock/server.ts` answers the five requests and detaches after a lost socket, so the page can be
driven; `dev-mac` offers a terminal in the fixtures and `dev-ci` does not. Tests cover the row's tap
rule and menu order, open with the fitted size, output written, input and resize sent, attach after
a reconnect with the scrollback first, close on unmount, and exited with New shell.

## The directory picker makes a folder (A37)

`features/sessions/DirectoryPicker.tsx` is the modal the New session drawer's Browse opens: the
path line, Up one level, the sub-directories with a branch mark on repositories, Cancel and Use
this directory. Beside the path line sits **New folder** (`docs/DESIGN.md` § "The three screens",
A37). Pressing it reveals `NewFolderRow.tsx`: one mono field (placeholder Folder name, autofocus,
Enter creates, Escape cancels the row and not the modal), Create and Cancel. Create sends
`device.mkdir {device_id, path: <the listing on screen>, name}`; the reply is a `DirsResult` of the
new, empty directory and becomes the listing on screen, so Use this directory picks it and the
drawer's working-directory field fills with the new path. A `conflict` shows "A folder with that
name already exists." under the field and keeps the name for editing; every other refusal —
`bad_request` for a slash or a leading dot, `not_found`, `forbidden` — shows the device's own
sentence the same way. The row owns no request and no state beyond its text, so a clash never
loses what was typed. The mock's directory tree moved to `mock/dirs.ts` so its `device.mkdir` can
make, clash and refuse like the device; `tests/DirectoryPicker.test.tsx` drives the row and
`tests/mock-dirs.test.ts` the tree.

## The session row

`features/sessions/SessionRow.tsx` draws what `docs/DESIGN.md` § "The session row" rules: three
lines, each a `.session-line` that puts one thing at the leading edge and one at the trailing edge.
The first is the title and the relative time. The second is the agent chip and, at the trailing
edge, the `StatusDot` with the session's **origin** — `sessionOriginLabel` in `src/strings.ts`:
"Terminal" for `origin: terminal`, "Remote launch" for `origin: remote`, `strings.labels.origin` —
in the secondary ink whatever the state (`docs/DESIGN.md` § "The session row says where it came
from"). The state is the dot's colour alone; "running", "idle", "terminal · attached" and "device
offline" are gone from the row, and a hand-archived row reads "Archived · <origin>". The chat
sidebar's rows follow the same rule: `SidebarItem.tsx` prints `basename · time` under every title
and leaves the state to the dot. On both lists a session the gateway marked carries the red dot of
A47 in the leading padding of its first line (§ "A red dot and the app icon badge (A47)"). Above the
list, `SessionLegend.tsx` draws the legend once — under the
toolbar, `.session-legend`, `role="list"`, the four dots at the rows' size with `legendEntries()`
from `legend.ts`: Working (green), For you (still amber; the pulsing amber is the same colour),
Not running (grey), Error (red) — caption type, secondary ink, no box, no border, and not when the
list is empty. The dot's tooltip (`labels.dotTone`) uses the same words for grey and red and says
"Waiting for you" / "Done" for the two ambers. `tests/SessionsPage.test.tsx` holds the row's word,
the archived prefix, the offline row and the legend; `tests/Sidebar.test.tsx` the sidebar's
sub-line. The third, `.session-cwd`, is the working
directory alone: a lucide `Folder` drawn to the same rule as the device row's laptop (ink, no fill,
stroke 1.5, round caps and joins, 14 px), then the path in mono from `tildePath`. The path truncates
from the head — `direction: rtl` on the span and a `<bdi>` around the text keep the characters in
order while the ellipsis lands at the start — so the folder the path ends in is what survives a
narrow row. Rows are `--row-h-three` tall at every width; the phone-width rules that used to wrap
the status onto its own line are gone with the column they served. `tests/SessionsPage.test.tsx`
holds the row to the three lines, the order on the second and the folder's outline.

## The device row

`features/devices/DeviceRow.tsx` draws what `docs/DESIGN.md` § "The device row" rules. A lucide
`LaptopMinimal` — a rounded screen over one base line, no fill, round caps and joins, stroke 1.5 —
sits at the leading edge in the ink, the same glyph on every device whatever its platform, because
the app cannot tell a laptop from a desktop and one honest glyph beats a wrong guess; it is what
keeps two devices apart now that no rule is drawn between them, and `--device-glyph` on
`.device-row` is what the lines under the name and the agents on a narrow screen indent past. The
iOS app draws the same path itself, so the two rows carry one mark. The name is the title and nothing repeats it: the hostname and the architecture left
the row for the device's page. The status line carries the online dot, which moved off the name to
sit with the word it belongs to, then "online" or "offline", then the platform as a word from
`platformLabels` in `src/strings.ts` — `macos` → macOS, `linux` → Linux, and a platform that table
does not know printed as the device sent it — and then the session count and the latency or last
seen. There is no client line (A36, `docs/DESIGN.md` § "A device keeps itself current"): the
gateway keeps every device on the wheel it serves, so the row shows no version and no "Update
available", ever; a third line, `.device-client`, appears only while `update_state` is `updating`
("Updating…", the dot pulsing) or `failed` ("Update failed · <reason>"), from `updateNotice` in
`src/stores/devices.ts`. The menu offers Rename and Revoke, and **Retry update** only while the
device is `failed` — disabled with a reason while the device is offline or the gateway serves no
wheel — which confirms with the version it would install and sends `device.update` exactly as A22's
Update did. The device page states no client version or build either: its facts are the hostname,
the platform and the architecture, and it shows the same notice and Retry. `tests/DeviceUpdate.test.tsx`
holds the row and its menu to that, `tests/DevicePage.test.tsx` the page; the mock stages one device
whose automatic update failed so the Retry path is on screen in development.

The agents are their logos alone, evenly spaced and with no name and no version beside them; each
logo is wrapped in a `role="img"` span whose `aria-label` and `title` are the agent's name, so the
row still reads aloud and a hover still names the mark. Versions live on the device page's agent
cards. `tests/DevicesPage.test.tsx` holds the row to all of it — one glyph per row, drawn as an
unfilled `rect` with rounded corners over a horizontal `line` with round caps and joins, the name
once, no hostname and no architecture, the dot inside `.device-status`, and an agent strip whose
text is empty.

## A device's page (A33)

`/devices/:deviceId` is `features/devices/DevicePage.tsx` with its own `device-page.css`. The row
opens it: `DeviceRow` wraps the device's name in a `Link` whose `::after` is stretched over the
whole row, and the row's menu is lifted above that box, so Rename, Update and Revoke keep working
and none of them navigates. The page repeats none of those three actions.

The header is the online dot, the name, `hostname · platform · arch`, and the client version with
the first eight characters of its build. The hostname and the architecture are here alone — the row
dropped both, and this is where someone goes to check them. Then one card per agent with
`available` true, in the device's own order: the agent's logo, its name and version, and under it
one sign-in line per account. A device with no agents says so in one line.

**The words on an account** are `features/devices/accounts.ts`, pure functions the tests drive
directly. The vendor comes from `vendorLabels` in `src/strings.ts` — `anthropic` → Anthropic,
`openai` → OpenAI, `xai` → xAI — and a provider that table does not know is printed as the device
reported it. Both methods lead with that name: an account reads *Anthropic account · Max · Max 5x ·
me@example.com*, with every part after the first drawn only when the device reported it, and a key
reads *Anthropic API key*, or *OpenAI API key · api.relay.example* when it goes to a third-party
host. pi signs in per provider, so it is the one agent with two of these lines. The plan word is the
vendor's own with its first letter raised, and everything else the device sent — the tier, the
email, the host, the window's scope — is printed exactly as it arrived, in both interface languages,
because it is data and not the app's own words; the device puts its tier into words itself, so
nothing here reformats one. An agent whose
`accounts` is absent came from a device too old to look, and the card says nothing at all; an empty
list is an agent signed in nowhere, and reads *Not signed in*.

**The meters** are drawn for accounts only, because a key has no plan window to measure. A window is
named from `window_minutes` — 300 is *5-hour*, 1440 *24-hour*, 10080 *7-day*, with its scope after
it as *7-day · Fable* — and carries the used share as a fill, the percentage, and *resets 15:40*
today or *resets Tue 22:00* on another day. The fill is the ink colour, the attention colour past
80 % and the danger colour at 100 %; no other colour appears on the page.

**Where the fresh figures live.** `hello` and `agents.updated` carry an account without its windows,
so the devices store never holds one. The page asks `device.agents` when it opens, and
`useDeviceQuota` keeps that reply in the page's own state, under the key of the request that fetched
it — device, reachability, attempt — so a Refresh or another device reads as *Checking…* without the
effect writing that state itself. The stored device is never overwritten, which is what keeps the
list and the new-session drawer from diffing against limits every quarter hour. An offline device is
not asked at all and reads *Offline · quota unavailable* where its meters would be; a
`device_offline` reply says the same; any other refusal, and a per-account `limits_error`, is
repeated in the device's own words with no meter under it.

## Layout and styling

`src/styles/tokens.css` holds every colour, size, radius and shadow, and `src/components/ui.css`
the primitives built on them — `.surface` for a grouped list, `.group-title` for a plain caption
above one, `.group-head` for a caption that doubles as its disclosure control, `.label` for a form
field's label, `.pill`, `.badge`, `.agent-chip`, `.btn` and the status dots. A visual change belongs
in those two files before it belongs in a component. The rules they encode are in `docs/DESIGN.md`:
one canvas, soft surfaces instead of bordered boxes, list rows instead of tables, one hairline
between rows, one filled primary button per surface, tinted rather than outlined chips, and sentence
case everywhere — no `text-transform` in any stylesheet.

**A class name belongs to one feature.** Nothing here is scoped or hashed, so two features that
pick the same name style each other's markup: the accounts screen called its list rows `.user-row`,
which is the chat's own message row, and its hover tint was what appeared under the pointer on a
message bubble. The accounts screen's rows are `.account-row`, `.account-name` and the rest now, and
`tests/css-ownership.test.ts` fails the build when two feature stylesheets take the same root class
again — the first class of a selector, which is what a rule applies to when nothing scopes it. The
message bubble is inert, as `docs/DESIGN.md` rules: neither `.user-row` nor `.user-bubble` has a
hover rule of any kind, because a message is a record and not a control.

At 1024 px and above the chat is two panes with the session sidebar. Below that the sidebar
collapses into the Sessions page, the chat runs full width with a back button, and the composer
sticks above the keyboard.

## Known gaps

- Real Web Push delivery, speech to text and attachments have not been exercised in a browser
  against a real gateway.
- The A47 badge on an installed web app's icon, from the page or from the service worker, and
  `session.seen` against a real gateway have not been exercised.
- Two corners of the approval card are untried: the session-scoped middle option, and an approval
  for a command rather than an edit.
- `session.delete` and renaming a session exist in the protocol and the device implements both, but
  the web UI has no entry point for either.
