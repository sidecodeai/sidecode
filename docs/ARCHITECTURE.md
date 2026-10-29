# Architecture

How the components fit together, what each one owns, and why the boundaries fall where they
do. `protocol/PROTOCOL.md` is the normative contract; this document explains the shape behind it.

## Components

| Component | Runs on | Owns |
| --- | --- | --- |
| `rc_gateway` | Your VPS, in Docker | Authentication, device enrollment, routing between apps and devices, the session index, a bounded replay buffer, the speech-to-text proxy, dictation polish, push, and serving the web app and the installer |
| `rc-client` | Every developer machine | Agent discovery, session lifecycle, the Claude, Codex, Grok Build and pi adapters, terminal-session mirroring and attaching, the full event history, and the local `seq` counter |
| `web` | A browser | The four screens, live rendering of the block timeline, voice capture, Web Push |
| SideCode apps | iPhone, Android, Mac, Windows | Distributed separately, not in this repository. The same screens natively, speaking the same `WS /ws/app` and `/api` surface as the web app to any gateway, the official one or yours; on-device or gateway dictation; their own notifications (§ "Push") |

Two WebSocket endpoints carry everything: `WS /ws/device` for daemons and `WS /ws/app` for apps.
Everything else is a small HTTP surface under `/api`, plus `GET /install.sh` and `GET /dist/*.whl`.

## Data flow

**Device to app.** The daemon assigns a `seq` to every session event, writes it to its own SQLite
history, and pushes it to the gateway as `session.event`. The gateway appends it to that session's
replay buffer, stamps `device_id` (amendment A5), and fans it out to the app connections subscribed
to that session. Nothing is stored beyond the buffer and the latest session summary.

**App to device.** An app sends a request such as `session.send` on `/ws/app`. The gateway resolves
the owning device, adds `device_id` when the request is addressed by `session_id`, stamps an opaque
`from` connection id, and forwards the frame unchanged. The device replies with the same `id` and
`from`, and the gateway routes the reply to that one connection. If the device is offline the
gateway answers `device_offline` itself; if no reply arrives within 60 s it answers `timeout`, and
a request written to a device link that a newer connection has replaced is answered `timeout` 10 s
after that connection's `hello_ack` if the device has not answered it there by then, because a
device that kept running answers over whatever link is current, and a connection that is replaced in
its turn hands the request to the next, which has its own 10 s (A56). Nothing is written to a device
before its `hello_ack`: the connection holds whatever is sent to it during the handshake and queues
it behind the ack and the preferences, so a request that was parked for a returning device cannot
cost it the link.

The gateway never inspects message text, tool input or tool output. It parses the envelope
(`type`, `id`, `device_id`, `session_id`, `seq`) and the `Session` summaries it indexes, and treats
the rest as opaque bytes bounded by size limits.

### What one process is allowed to hold

The container has 512 MiB (`mem_limit` in `docker-compose.yml`), and §5 lets one `session.send`
carry eight attachments of 6 MiB, which base64 inflates to a 64 MiB frame. Forwarding one was
measured at 128 MiB of live allocation — the parsed payload and the string queued for the device —
with a 142 MiB peak, so the bounds are chosen as a budget rather than one at a time:

| Bound | Figure | Why |
| --- | --- | --- |
| Large frames in flight through the forward path | one maximal frame, 72 MiB of payload (`rc_gateway/budget.py`) | Two would reach 592 MiB against a 512 MiB limit. A sender beyond it gets a `too_large` reply, not an OOM kill that takes every link with it |
| Replay buffers kept at once | 16, so 64 MiB at the §6 per-session maximum | The map is bounded because each buffer is; evicting the coldest costs the next subscriber a `resync: true` |
| `queue` snapshots and session owners kept at once | 512 and 8192 | Both are keyed by a session id a device chose, and both fall back cheaply: a snapshot is simply absent, an owner is re-read from the index |
| `/ws/app` sockets per account | 8, oldest closed with 4009 | Every broadcast is linear in them, on the shared event loop, so one account's sockets are everybody's fan-out cost |
| Agents a device may announce | 16 entries, each at most 64 KiB | `POST /api/devices/enroll` has always truncated to 16; `hello` and `agents.updated` now agree, because the blob is stored, returned by `GET /api/devices` and re-broadcast to every app socket. A gateway limit, not a wire rule: the protocol says nothing about how many agents a machine may have |

## Liveness: one clock, and what "offline" means

The gateway pings both socket types every 25 s and closes a connection silent for 90 s. That is the
only keepalive on the link: uvicorn's own protocol-level ping, which defaults to a 20 s interval
with a 20 s answer deadline, is switched off (amendment A13). Two clocks meant the shorter one won,
so a daemon whose event loop stalled — a large `git status`, a laptop resuming — was closed with
`1011` while still comfortably inside the 90 s the contract gives it.

A device link that drops is not an offline device. Mobile NAT, a suspended laptop and a daemon
restart are indistinguishable for the first few seconds, so the gateway keeps reporting
`online: true` for a **20 s grace period** and only reports `online: false` if no replacement
connection arrived. A reconnect inside the window produces no offline frame at all, and a request
addressed to the device in the meantime waits for the replacement rather than being refused;
it is answered `device_offline` only when the period ends without one. A close carrying `4401` or
`4403`, and an explicitly removed device, skip the grace and flip `online` at once: those say the
device is not coming back, not that the link went quiet. A second connection for the same device
still replaces the first with `4001`, unchanged.

Two situations earn a longer grace (A56). A link that drops while an update the gateway forwarded
is running is waited for **60 s**, because the device's own service restart comes on top of the
link dropping. And a gateway that stops cleanly records, in `devices.sqlite3`, the devices the apps
were told are online; a process that starts within 120 s of it begins a **30 s** grace for each
(60 s for one the store says is updating), so a restart does not report every device offline for the
4 to 19 s the devices take to reconnect, and the requests the apps make in the meantime wait for
their device and follow its `hello_ack`. The record is spent by the start that reads it and a crash
makes none, so a cold start is as it always was.

Refused upgrades are logged, at one line per source address per minute carrying how many attempts
it made in between, and an address far past any sane retry backoff is refused before the handshake
rather than accepted only to be closed. A daemon left behind by a wiped machine retries forever —
one gateway saw 685 refusals in three hours from a single address, all silent — and logging every
one would let any client fill the disk. All three upgrades count separately: `/ws/device`,
`/ws/app` and `/ws/stt` each keep their own tally, so a flood on one never refuses another.

All three are text protocols, and a binary frame on any of them is ignored rather than fatal. It
used to raise inside the handler, which cost the peer its link with no close code and, on a device,
the 20 s grace period too; a peer that sends three in a row is now closed with `1008` and told why.

## The block timeline

Every adapter normalises its agent's output into one model, so the apps contain no agent-specific
rendering. A session is a list of **blocks**, each identified by a `block_id`:

| Kind | What it renders as |
| --- | --- |
| `user_message` | A message you or the terminal sent, with attachment names |
| `assistant_text` | Streamed Markdown, `delta` events appending until one arrives with `done` |
| `thinking` | The same streaming rule, collapsed behind "Thought for 12s" |
| `tool_call` | A one-line row that expands to input, output and diffs |
| `approval` | A card with the exact options the agent offered |
| `question` | A card with one or more questions, options, free text or secret input |
| `todos` | A snapshot that replaces the previous list |

Around them sit non-block events: `turn_started`, `turn_completed`, `status`, `meta`, `queue`,
`notice` and `error`. Three rules make this safe to render incrementally:

1. **A later event with the same `block_id` replaces the earlier one**, except streaming deltas,
   which append.
2. **Blocks sort by `first_seq ?? seq`** (amendment A8), so a tool call that finishes long after it
   started keeps the position where it appeared, live and after a reload.
3. **Only `status` and `turn_*` change a session's state.** No UI infers completion from silence.

Tool payloads are bounded inside events — input 8 KiB, output 16 KiB, patch 32 KiB, 64 KiB per
frame — with a `*_truncated` flag. The untruncated block is fetched on demand with `session.block`,
up to 1 MiB.

## Session ownership, replay and backfill

The **device is the source of truth**. It holds the complete history and the `seq` counter, and the
counter survives daemon restarts. The gateway holds two things: the latest `Session` summary per
session in SQLite, so lists render while a device is offline, and a replay buffer of the last 2000
events or 4 MiB per session, whichever is smaller, for the 16 most recently used sessions.

An app reconnecting renders its cached list, receives `hello`, then subscribes with the last `seq`
it applied. If the buffer still covers that cursor it gets the missing events in the reply. If it
does not and the device is connected, the gateway asks the device for them (below, A56). If that
does not work either, the reply carries `resync: true` and the app pages history from the device
with `session.history`. The subscribe reply also carries the latest `queue` snapshot (amendment A6),
because a queue is current state rather than a timeline row. The device keeps every queue in `ts`
order, and an app edits an entry by taking it out with `session.queue_remove` and sending the new
words back with that `ts` as `queue_ts`, so the edit waits where the original did and cannot be
delivered while it is still being changed (A43).

When a **device** reconnects after an outage, the gateway compares each session's `last_seq` from
the device `hello` with the tail of its own buffer and with the cursor of every app subscribed to
the session. Where the device is ahead of either, it asks the device for
`session.history {after_seq: <the lowest of them>}`, appends what comes back to the buffer, and
fans it out to subscribers as ordinary events, each subscriber being given the events after its own
cursor (amendment A9). Apps need no special handling: they apply events by `block_id` as always.
The buffer being empty is not a reason to skip a session: after a gateway restart every buffer is
empty, and an app that subscribed before its device's `hello` was told nothing was missing and then
kept a hole where the device's outage was, because nothing asked for those events. Now something
does (A56). The requests run as background tasks so a slow device cannot hold up the reconnect.

**The device, not the gateway, answers what the gateway cannot (A56).** The same request serves a
`session.subscribe` the buffer cannot cover, when the device is connected: the reply carries the
events the device returned followed by what arrived live while it was being fetched, and
`resync: false`. Both paths are bounded — a request is 500 events and 1 MiB and waits 3 s, one
request is in flight per session and two per device, so an account whose apps all reconnect at once
cannot flood the one machine that holds its history; a subscribe takes one page, and a device's
`hello` pages through a longer gap, five pages and 10 s at most, the wait for a turn counted, and
gives no socket more than 1,500 frames at once, counting what its send queue holds, which an app's
queue can take — and a reply that is an error, exceeds a bound or never comes, or a gap that
outlasts the pages or the time, is applied in no part, because half a gap is how a hole gets into a
transcript. What follows from that failure is the old answer, `resync`: a subscribe is answered
`resync: true`; every socket that is subscribed and behind the device's count when a `hello`'s fetch
fails, the ones that were waiting and any that subscribed meanwhile, is marked and answered
`resync: true` the next time it subscribes, which is what an app's own gap repair does, so the apps
that cannot repair a gap themselves still end with a transcript that is whole; a later fetch that
succeeds clears the mark only when it covered the hole the mark stands for; and a replay buffer that
would be left with a hole is dropped, so that the next subscribe asks the device instead of trusting
it. A socket that subscribes with no cursor pages history itself, and counts as holding the
session's `last_seq`, so the events a link outage cost it are fetched for it too. A subscribe that
has to wait for the device runs apart from its socket's read loop, so a Stop, a send or a pong on
the same socket is not read after the fetch; subscribes to one session are answered in order and an
unsubscribe ends one that is still waiting.

Order matters, because an app drops an event at or below its cursor as late. While a session's fetch
is in flight its live events are held and published with the fetched ones in one pass in `seq`
order, a held event that history does not carry (a `queue`, a delta, a `status`) keeping its place
between them, and the buffer turns away any the fetch already brought; a socket is only sent fetched
events past its own cursor, so two apps at different cursors are each given exactly what they lack,
and one that paged history itself is owed what follows the `last_seq` it counted as holding. If the
device's connection is replaced meanwhile, its fetches end and what they held is dropped, neither
published nor buffered, so that the replay buffer is never given events that skip what nobody
fetched: the sockets that were behind are marked, a session event the replaced connection still
delivers is dropped, and the next connection's `hello` fetches again from each socket's cursor,
because the device keeps every event history carries, and a current client sends the Up next queue
again after its handshake and its summaries in the `hello`. The hold is bounded and, if it fills or
the fetch ends in an error, the sockets that are owed events are marked, as they are when a fetch is
cut short while it publishes: the marks go on before the first event and off after the last, and a
pass that ends before its last event marks every subscriber of the session, those level with the
hello too, and drops the replay buffer, because an event it held may have reached only some of them.
When the link dropped, what was held and is not out follows as live events; after an overflow, when
newer events are out already, and when the connection was replaced, nothing is published again, and
the marks cover the loss. A socket that would be given more than the 1,500 frames is sent none of
the fetch and stays marked, which still reaches the buffer and the sockets that fit. Only the device
that owns a session is ever fetched from for it, so a `hello` that names another device's session
fetches nothing and publishes nothing into it. Fetched events pass through memory like every other
event: nothing of a conversation is written to the gateway's disk, the replay buffer is not
persisted, and the log lines say ids, `seq` numbers, counts, milliseconds and the outcome and never
a title or any text. The queue snapshot is not recovered by a fetch, because `session.history`
carries no `queue` event; it comes back with the device's next one. A summary, from a
`session.updated` or a `hello`, that reports `queued` as 0 drops the snapshot, so a queue emptied by
a frame lost on a half-open link does not outlive it.

Nothing on the way from a device frame to the apps touches the disk. A device socket reads and
dispatches one frame at a time, so anything awaited while handling a frame is latency every later
frame from that device inherits. The `last_seq` cursor an event advances is therefore recorded in
memory, before the fan-out, and written to SQLite by a background task that batches whatever has
accumulated; every read of the index overlays the values still unwritten, so the recorded cursor is
visible to `session.subscribe` and to `hello` the instant it is recorded and an app can never be
told it is up to date about an event it has not received. Losing the unwritten tail to a crash only
makes a reconnecting app resynchronise, which the protocol already handles. Routing a request from
an app to a device is answered from the in-memory owner map for the same reason: only the owning
device id is needed to forward, and reading the whole summary back from SQLite would put a disk
read in front of every message anyone sends.

## Terminal sessions: mirroring, attaching, takeover

A session you start yourself in a terminal is not invisible. Every ten seconds the daemon scans for
Claude transcripts under `~/.claude/projects` and Codex rollouts under the Codex home, tails the
ones that have grown, and translates their rows into the same block timeline. Growth is detected by
file size rather than modification time, because `claude --resume` touches mtime without appending.
Codex mirroring is the fallback rather than the rule: a thread the shared app-server daemon knows
about is read from the daemon instead, as the Codex subsection below describes.

Mirroring answers "what is happening". A parallel process scan answers "who may type", and that
answer is the session's `control` value. It is the only thing the apps consult to decide whether the
composer is live; `state` alone never decides it (amendment A7).

| `control` | Who owns the input | What an app can do |
| --- | --- | --- |
| `remote` | The daemon runs the agent for this session | Everything |
| `terminal` | A live CLI owns it and the device has no way in | Read only, plus "Take over" when the agent advertises `takeover` |
| `shared` | A live CLI owns it **and the device is attached to it** | Compose, approve and queue exactly as for `remote` |
| `none` | No process owns it | The next message resumes it and control becomes `remote` |

`terminal` and `shared` are the same CLI process seen with and without an attachment. `shared` is
what makes leaving the desk cheap: the terminal keeps running, and the phone joins it.

State follows from that. A `terminal` session reports `running` while its terminal turn is in flight
and `readonly` only when that turn is idle, and `readonly` means exactly this one situation. An
attached session never reports it: `shared` sits at `idle` between turns, like any session the
device runs itself.

### The channel bridge

Claude Code 2.1.267 and later can load a **channel**, an MCP server allowed to inject user messages
into the live interactive session and to relay its permission prompts. `rc-client channel` is that
server. The CLI spawns it over stdio; it reads `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`
from its own environment, dials the daemon's Unix socket at `RC_CLIENT_HOME/state/channel.sock`, and
registers the session it belongs to.

```
claude (your terminal)
  └── rc-client channel          stdio MCP server, spawned by the CLI
        └── state/channel.sock    newline-delimited JSON
              └── rc-client run    the daemon, which owns the gateway link
```

A Unix socket rather than a port, because a fixed port collides the first time two things want it.
A socket path is capped at 104 bytes, so a deep `RC_CLIENT_HOME` moves the socket into a short
per-user directory under `TMPDIR` instead. The bridge buffers what it cannot send and replays its registration frame on every reconnect, so a
daemon restart does not cost the attachment. It exits the moment stdin closes, because Claude Code
does not reap it and a survivor would hold the socket.

### Inject only when the session is idle

This is a hard rule, not a tuning choice. A message injected while a turn is running does not become
a prompt: the CLI wraps it as an attachment and tells the model, in as many words, that it is
untrusted external data that must not be obeyed. The same text injected while idle starts a turn in
about twenty milliseconds.

So the daemon holds anything it cannot inject immediately, and the queue says so. A message sent
into a running shared session is a `queue` entry and nothing else until the transcript shows the
turn has ended; then the daemon injects it and emits the `user_message` under the request's id,
so `first_seq` puts it after the turn it waited for, where the terminal draws it (amendment A19).
If the CLI absorbs an injection anyway the bubble becomes `delivery: "absorbed"` and the device
re-sends it once at the next idle point. The block is replaced, never duplicated, and `first_seq`
keeps it in the position where it first appeared (amendment A8).

Turn state comes from the transcript the daemon already tails, so it lags reality by up to a couple
of seconds. Every injected message carries the device's own id in the channel `meta`, which survives
verbatim into the transcript row, and that is how the daemon recognises its own echo rather than
mirroring it twice.

### The permission relay

Permission prompts raised inside the attached CLI arrive on the channel as `{request_id, tool_name,
description, input_preview}` and become ordinary `approval` blocks. Two things differ from an
approval the device raised itself. The options are exactly Allow and Deny, because the relay offers
no session-scoped grant. And the terminal dialog stays open the whole time, so the same request has
two answerers.

Whoever answers first wins, and a late answer from the other side is discarded silently. When the
terminal answers first the daemon learns it from the transcript — the tool ran, or it was refused —
and resolves the card with `decision.by: "terminal"`, which is how a decision made at the desk shows
up on the phone. If the CLI exits or the attachment drops with a request still open, the card
expires.

### The shim, and why the dangerous flag is unavoidable

Claude Code refuses to load a self-hosted channel without
`--dangerously-load-development-channels`. Every alternative was tried and refused: `--channels
server:rc` is rejected as not on the approved allowlist, the same for a locally packaged plugin
channel, and the project setting `channelsEnabled` is ignored outright. The one setting that could
allowlist a channel plugin, `allowedChannelPlugins`, is managed and exists only for Team and
Enterprise accounts, so a personal plan has no path to it.

That leaves wrapping the CLI. `rc-client shim install` writes a small `claude` wrapper at
`RC_CLIENT_HOME/bin/claude` and puts that directory first on your `PATH`. The wrapper appends the
channel flags **only** when stdin and stdout are both terminals and the command line carries none of
`-p`, `--print`, `--input-format`, `--output-format`, `--sdk-url`, `--mcp-config` or
`--dangerously-load-development-channels`; anything else execs the real binary untouched. That test
is what keeps the device's own remote sessions, which are driven over pipes, out of the attachment
path entirely, and it is why a script that pipes into `claude` behaves exactly as before.

The cost is one dialog. Claude Code shows a development-channels warning once per interactive
session, and it has to be answered before the session starts. There is no way to suppress it on a
personal account, and pretending otherwise in the installer would only make the first run confusing.

### The pseudo-terminal the shim owns (A40)

The channel carries messages and permission verdicts and nothing else: three methods, and text
injected through it arrives wrapped in a `<channel>` tag that the CLI never reads as a slash
command. So a `shared` Claude session could show its model and effort (A17, read from the
transcript) but not change them, and could not compact. What can do all of that is the keyboard,
and the shim is already the process that starts the CLI. It starts the CLI inside a
pseudo-terminal a small proxy owns (`rc_client/channel/pty.py`, stdlib only, so the terminal does
not lag at launch): the proxy relays bytes and window sizes both ways, registers with the daemon
under the CLI's pid — the same pid the channel bridge and the SessionStart hook report, which is
how the daemon pairs the two — and types what the daemon asks it to. The daemon types exactly what
the person would: `/model` and `/effort` open their own pickers and are confirmed with `s`, "this
session only", so no settings file of the person's is written; `/compact` is typed as is. It types
only into an idle terminal — no turn, no dialog, nobody typing (the proxy counts the person's
keystrokes since their last Enter) — and answers `conflict` otherwise, queueing nothing; it reports a
settings change only once the transcript's `<command-name>` row confirms it. `AgentInfo` says which
settings this covers through `shared_settings_keys`: for Claude `model` and `effort`; the permission
mode has no command to type and stays the terminal's.

### Transitions, and what takeover is still for

Control moves `terminal → shared` when a bridge registers, `shared → terminal` when the bridge drops
while the CLI is still alive, and `shared → none` when the CLI exits. Each move travels as a `meta`
event carrying `control`, a `status` event when the state changed with it, and a republished session
summary — the same mechanism every other control change uses.

Takeover has not gone away; it has become the fallback. `session.takeover` is still Claude-only and
still works only while the terminal owns an *idle* session: the daemon sends SIGTERM to the exact
process it identified, confirms the release, and resumes the session itself. That kills whatever the
CLI was running, which is the whole reason attaching exists, so it is now for one case — a terminal
that was started without the shim and cannot be attached. On a `shared` session `session.takeover`
answers `conflict`. Codex advertises no `takeover` capability and needs none: a bare `codex` is
attached rather than taken over, and a Codex TUI that cannot be attached is released by quitting it.

### Codex on the shared app-server daemon

Claude needed a shim because the CLI has to be told to load our channel. Codex needs no shim at all.
Since 0.153 every **bare** `codex` TUI runs its conversation inside one local app-server daemon, and
that daemon accepts further clients. The device is simply one more of them.

The control socket is `$CODEX_HOME/app-server-control/app-server-control.sock`, and it speaks
JSON-RPC 2.0 over a WebSocket rather than newline-delimited JSON: a client opens an `AF_UNIX` stream,
writes an RFC 6455 upgrade request, expects `101 Switching Protocols`, and exchanges frames from
there. Compression has to be off: the daemon closes the connection outright when a client offers
`permessage-deflate`. Authentication is the socket's own permission bits. It is owner-only, so the
device has to run as the same user as the TUI, and nothing here is reachable from the network. The
device opens one connection per machine at startup and names itself `remote-control`. On Codex
0.154.0 the first client to connect was observed to stamp its name on every daemon thread,
including a terminal user's; this is evidence for that version. A terminal thread inspected on
Codex 0.160.0 reports `originator: "codex-tui"`, `source: "vscode"`. Every server request is
dispatched to its own task, so an approval nobody answers cannot block the read loop.

A Codex thread is therefore joined rather than mirrored. `thread/loaded/list` is the set of live
threads and `thread/list` the history; `thread/resume {excludeTurns: true}` subscribes to one, and
`thread/turns/list` with `thread/items/list` backfills it. The daemon's `item/*` and `turn/*`
notifications map onto the block timeline one for one, so a shared Codex session has no rollout
tailing behind it. Threads the daemon marks `ephemeral` never become sessions: Codex spawns one per
turn purely to generate a title.

The device filters provenance before either daemon adoption or rollout mirroring (A18, A48).
`source` must be a string, and the thread must carry the device's own client name, a `cli` or
`exec` source, or the exact pair `originator: "codex-tui"`, `source: "vscode"` observed for a
terminal TUI on Codex 0.160.0. Other applications' `vscode` threads stay foreign, and a subagent's
object `source` never becomes a session of its own. Apps read only the published session summary;
they need no Codex-specific discovery rule.

`origin` and `control` come from one table in `protocol/PROTOCOL.md` (4.4). A thread the device
created and no terminal has typed into is `remote` and `remote`. A thread that was already loaded
when the device found it, or that carries a message typed at a TUI, is `shared`, with origin
`terminal` when the device did not create it. A thread known only from the daemon's history is
`none`, and the next message resumes it. A rollout held by a Codex process that is *not* the daemon
is `terminal` and cannot be attached at all. `shared` is sticky, because the daemon emits nothing
when a TUI exits and the thread simply stays loaded.

A thread that disappears from `thread/list` altogether is a different matter: the device deletes the
session and emits `session.removed`, so a thread deleted with `codex delete` does not linger in the
apps. That check is guarded against a thread merely falling off the end of a page (an answer with
no `nextCursor` is the whole history and has no end to fall off) and against a session a runner is
still driving, because both would otherwise look like a deletion.

Above the daemon client, a shared Codex session is an ordinary session. It is a hub runner like any
other, so `session.send`, steering, the queue, `session.stop`, `session.set` and `session.answer` all
travel the same code a remote session uses; only the runner underneath differs.

### What each attachment carries

A shared Codex session can do everything a remote one can, which is much more than a Claude channel
can. Apps stay agent-agnostic by reading five optional fields on the agent object rather than
branching on the agent id:

| Agent | `attach` | `shared_interrupt` | `shared_settings` | `shared_attachments` |
| --- | --- | --- | --- | --- |
| Claude, through the channel shim and its pseudo-terminal | `channel` | true — Escape typed into the terminal (A42) | true — `model` and `effort` only (`shared_settings_keys`, A40) | true — files the injected words name (A49) |
| Codex, through the app-server daemon | `daemon` | true | true | true |
| pi, through the device's extension | `extension` | true | true | true |
| Grok Build, through its leader process | `leader` | true | true | true — files the prompt names (A49) |

`shared_interrupt` maps to `turn/interrupt`, which works whoever started the turn. `shared_settings`
maps to `thread/settings/update`, so changing the model, the permission mode or the reasoning effort
from a phone changes the thread for everyone attached to it. `shared_attachments` is true because the
daemon accepts image inputs; a Claude channel and Grok's leader take no image, so the device writes
each file to disk and the words the agent receives name it, and the agent opens it itself (A49).
And because Codex advertises `steer`, a message sent into a running shared turn joins that turn
through `turn/steer` instead of waiting for it — the one thing an attached Claude session cannot
do. A deliberately queued message on a Codex session waits in the
queue as it does anywhere else (A19). `attach_ready` is set from a real handshake on the socket, not from the
socket file existing, because a stale socket is exactly the case the hint text exists for.

**pi attaches through an extension of the device's own (A26).** pi has no daemon and no socket, but
it loads TypeScript extensions from `~/.pi/agent/extensions/` into every process, and an extension
sees every agent event, can block a tool call, inject a user message, abort the turn and change
the model and thinking level. `rc-client pi setup` installs the device's extension there; the
device also loads it into the RPC sessions it starts when the installed copy is missing or stale.
The extension dials a Unix socket in the device home, registers the pi session with the branch it
already holds, forwards pi's events verbatim so one translator serves terminal and remote sessions
alike, and takes the device's commands back. It is also what gives pi permission modes: pi itself
asks nothing, so the extension holds a tool call until the app or the terminal answers — the
Codex vocabulary, `untrusted` / `on-request` / `never`, with the device deciding which tools each
mode asks about. A pi session started in a terminal is therefore `shared` for as long as the
process lives, and without the extension it does not exist for the apps at all.

**Grok Build attaches through its own leader (A28).** Grok has what Codex has, under another name:
a *leader* is one backend process per machine, and every `grok` on a machine whose
`~/.grok/config.toml` says `[cli] use_leader = true` runs its session inside it instead of in its
own process. Whichever client comes first starts the leader — the TUI, or the device's own
`agent agent --leader stdio` client — and it stays up when they leave. A `session/load` from a
second client of the leader does not open a second copy: it joins the session the TUI is in, and
from then on every update reaches every client, a prompt from a phone runs in the one conversation
and is drawn by the TUI, `session/cancel` stops the TUI's turn, `session/set_config_option` changes
the model or the effort for everyone, and a permission prompt is a request sent to every client,
answered by whichever comes first; on grok 1.0.30 that holds in both directions, with two device
clients joined at once. The flag is off by default and lives in the person's own configuration, so
`rc-client grok setup` turns it on in place — the dotfile is often a symlink into a synced folder,
so the file is edited where it is, never replaced —
and a `grok` already running when that happens keeps its own process until it is restarted, which
is what the apps' hint says. Who is in a session is read from Grok's own registry,
`~/.grok/active_sessions.json`, because the leader announces nothing when a TUI exits; whether a
registered TUI is inside the leader is one `_x.ai/session/info` away, which answers `{}` for a
session the leader does not hold. Two things the device never does: `session/close`, which unloads
a session for every client including the terminal that is in it, and offering the leader's "don't
ask again for anything" approval option, which is a permission policy and so a `session.set` matter.

### Approvals are shared state, not a private modal

A permission request inside the daemon is a JSON-RPC *request*, and it fans out to every subscriber:
the TUI raises its dialog and the device receives the same request with the same item id. Either side
may answer, whoever answers first wins, and the loser's answer is discarded silently with no error.
The device turns the daemon's own `availableDecisions` into the block's options — `allow`,
`allow_session`, `allow_always`, `deny` — so a card offers exactly what that prompt allows rather
than a fixed pair. A10's "exactly Allow and Deny" was always a property of the Claude relay, which
has no session-scoped grant to offer.

When the answer came from somewhere else, the daemon broadcasts `serverRequest/resolved` carrying
only a request id: not who answered, and not what they chose. The device closes the block with the
reserved decision `{option_id: "elsewhere", by: "terminal"}`, which matches none of the options on
purpose, and both apps render it as "answered in the terminal". `session.approve` still accepts only
an option the block offered, `elsewhere` included in the refusal, so an app can never send it back.

A request the device does not present gets no reply at all: the first reply of any kind, an error
included, settles a request for every subscriber, so answering what is not the device's to answer
would answer it for the terminal too. The device answers only when nobody else could, in a turn it
started on a thread no terminal is in; CLIENT.md, "Codex on the shared daemon", has the rule.

### Without a daemon, and the one rule for users

If the socket is missing or does not answer, the device falls back to what it did before: one
`codex app-server` process per session, spawned by the device, with terminal Codex sessions mirrored
from their rollout files and left at `control: "terminal"`. The agent keeps reporting
`attach: "daemon"` with `attach_ready: false`, which is what the apps' hint is for. But the device
does not wait for somebody else to bring the daemon up. Since openai/codex rust-v0.157.0
`daemon_auto_start` is Stable and on by default: a bare TUI starts the daemon itself
(`tui/src/startup_orchestration.rs`) and connects to the same socket, falling back to an embedded
app-server for `--no-daemon`, `--oss`, `--profile`, `-c`, `--enable`, `--disable`, `--search`,
`--strict-config`, WSL DrvFS and a Windows administrator. Codex 0.153 to 0.156 never starts it (a
bare TUI runs embedded and joins only a daemon that already exists), and the excluded launches never
do on any version, so on the same ten-second scan the device runs `codex app-server daemon start`
itself whenever the standalone build is installed and the socket is not answering — idempotent, a third of
a second, rate-limited, never downloading — installs its supervision once if the machine has none,
and connects the moment the socket answers, publishing `agents.updated` so the apps drop the hint.
The one thing it never does from the background is install Codex: that stays in
`rc-client codex setup`, where a person asked for it. A device whose Codex came from npm, one that
has upgraded Codex and one enrolled under npm and later switched to the curl build are all this same
case: a missing daemon.

An upgrade has a second effect. `codex update` replaces the build on disk and leaves the running
app-server alone, so the daemon serves an older version than the CLI until something restarts it;
TUIs join it anyway, and only `codex app-server daemon version` shows the drift. Every fifteen
minutes the device asks, and when the versions differ it runs `daemon restart` — only when no turn it
drives is running and no terminal is attached to a daemon thread, because a restart drops every
subscriber — then reconnects and backfills as it does after any drop.

There is no rule about how to start Codex. Some flags make the CLI spawn its own embedded
app-server, invisible to the shared daemon and unattachable — `-c` does on 0.154 — and some do
not: `--dangerously-bypass-approvals-and-sandbox` runs on the shared daemon like a bare `codex`. The
device does not keep a list of which is which. It
looks at what the process holds: a TUI writing its own rollout is the one holding a file under
`$CODEX_HOME/sessions` open, and only that kind is left out of the count of terminals attributed to
daemon threads. Such a session still appears, mirrored from its rollout with `control: "terminal"`.
Everything one would have set with `-c` can be set from the apps instead, through the pickers
`shared_settings` unlocks.

Codex's own `daemon bootstrap` reports `backend: "pid"`: it starts the app-server and a detached
updater loop, and installs no launchd job and no unit, so nothing brings it back after a reboot. The
device therefore supplies its own supervision — a launchd agent `dev.remote-control.codex-daemon` on
macOS, an `rc-codex-daemon.service` user unit on Linux — each running the idempotent
`codex app-server daemon start`. `rc-client codex setup` installs the standalone build when it is
missing, does the bootstrap and the supervision; `rc-client codex status` verifies with a handshake
rather than a file test and reports the two versions. Details are in
[`docs/CLIENT.md`](CLIENT.md#codex-on-the-shared-daemon).

Four things about the daemon are known to be untested, and all four are properties of Codex rather
than of this project. What happens when a TUI and the device start a turn on the same thread within
milliseconds is unknown. Nothing observed unloads a thread, so whether there is an eviction policy
matters for a daemon that lives for weeks. Whether subscriptions survive a dropped socket, and
whether anything emitted while disconnected is replayable beyond re-reading history, was not
established — the device re-resumes and backfills on every reconnect precisely because it is not.
And the updater loop replaces the app-server under connected clients on a new release; what they see
during that swap was never observed.

## Slash commands

A terminal offers its agent's commands the moment `/` is typed; the apps offer the same list on any
session whose agent reports capability `commands` (amendment A27). The mechanics differ per agent
and the protocol hides that: `session.commands` returns what the session offers now,
`session.command` runs one, the device echoes it as a `user_message` under the request's id and
reports the outcome as ordinary events.

Grok Build advertises its list over ACP when a session opens (`available_commands_update`) and
interprets a prompt that begins with a slash itself, so the device stores the list and hands the
command over as the text of a turn; a shell-side command such as `/hooks-list` runs locally at zero
tokens and answers in `assistant_text`. pi answers `get_commands` in RPC mode with its extension
commands, prompt templates and skills, and expands a `/name` prompt before the turn; a terminal pi
session gets the same through the device's extension, which asks pi to expand what it injects.
Codex's app-server interprets nothing — a `turn/start` that begins with `/status` is a model turn
about the literal text — so the device ships a fixed table and maps each entry to the method it
stands for: `thread/compact/start`, `review/start`, and read-only methods whose answers become a
`tool_call` block titled with the command. Claude's channel carries user text and nothing else, so
Claude does not list the capability and the apps draw no menu for it.

Three things are never commands: settings (`/model`, `/permissions`, `/fast`, `/thinking`) are
`session.set`; lifecycle (`/new`, `/archive`, `/delete`) has frames of its own; terminal ergonomics
(`/vim`, `/theme`, `/keymap`) change a terminal the app cannot see.

## Speech to text

Voice is a stream, not an upload. The app opens `WS /ws/stt`, captures the microphone,
downsamples to 16 kHz mono PCM16LE, and sends roughly 100–200 ms binary frames. The gateway buffers
them, wraps the audio as WAV, and asks the configured backend for a partial transcript about every
two seconds, sending back `stt.partial`. On `stt.stop` it transcribes everything and returns
`stt.final` with the detected language, then closes the socket. Limits are 120 s and 4 MiB per
utterance. Nobody chooses the language (A44): every provider is asked to detect it, a `language`
an older app sends is ignored, and `stt.languages` is always `["auto"]`. The one recogniser that
must be told a language is a phone's own, so `stt_language` is that phone's setting, Chinese
until the person picks another.

The backend is anything that implements `POST {STT_BASE_URL}/audio/transcriptions`: OpenAI itself,
or a local Whisper server on the compose network so audio never leaves the VPS; Xiaomi MiMo is
reached through a chat completion carrying the audio. All of those answer a whole utterance, so
their "partials" are the whole buffer transcribed again — two seconds behind, and costlier the
longer the dictation runs. The `realtime` provider (`rc_gateway/stt_realtime.py`) is the
one that streams: it holds a WebSocket to the vendor for the life of the utterance over the OpenAI
Realtime transcription protocol in the dialect Alibaba Model Studio documents for
`qwen3-asr-flash-realtime` (`session.update` with `input_audio_format: pcm` at 16 kHz and server
VAD; `input_audio_buffer.append` per frame; incremental
`conversation.item.input_audio_transcription.text` events with a tentative `stash`; `….completed`
per sentence; `session.finish`/`session.finished` to end), forwards each frame as it lands and
sends `stt.partial` on every vendor event. The app protocol did not change: the same `stt.partial`
and `stt.final` frames, only sooner. A phone app can skip the gateway entirely and use its platform's
on-device recogniser instead, which is a different privacy story and is labelled as such in its
settings. Either way the transcript lands in the composer as an editable draft;
sending is always a separate, explicit action.

There is also a non-streaming `POST /api/stt/transcribe` for a recorded file. Both return `503`
when `STT_PROVIDER` is `none`.

## Dictation polish

Speech is immediate, and a transcript of it is full of "um", second starts and references that made
sense with the screen in front of the speaker — "the green blinking thing" for a session's pulsing
status dot. Sent as-is it makes the agent guess. So the gateway can hold one more key of the
operator's (A29): `POLISH_BASE_URL` and `POLISH_API_KEY` name an OpenAI-compatible provider, and
with them set the gateway serves that provider's models at `GET /api/polish/models` and polishes a
dictation at `POST /api/polish`. Apart from the voice assistant below, this is the only language
model the gateway calls, it does so only when an app asks, and the request is the app's own words:
the dictated text, the model and strength the user chose in Settings, a language hint, and up to
twenty of the conversation's most recent user and assistant messages exactly as the app shows them.
The gateway never reads a device's history for this, stores nothing from the call, and hands the
answer back to the app alone — it is a draft in the composer, and sending it is still the person's
action.

Two strengths, fixed in the protocol so both apps and the prompt agree. *Moderate* removes fillers,
false starts and repetitions, corrects what the recogniser plainly misheard, punctuates, and keeps
the speaker's words and order. *Strong* also restructures for clarity and precision and resolves
vague references from the conversation — the blinking thing becomes the term the conversation used
— while adding no request the speaker did not make. Both answer in the language the text was spoken
in and return text only; the gateway wraps the dictated text so a model cannot read it as an
instruction. `hello` and `GET /api/config` carry `polish.enabled`, which is what lets an app show
the setting or disable it with a note. Without the two variables everything about dictation is as
it was.

## The voice assistant

An experiment (`docs/LIVE_ASSISTANT.md`), off unless switched on in `.env`. A client, the web
app's panel or an app's call screen, talks to the gateway on `WS /ws/live`, and the gateway alone
holds the one WebSocket to OpenAI's Live API with the operator's key, so neither the key nor the
audio path reaches a browser or an app. The model's tool calls run in the gateway as the signed-in
account: every read and write starts from that account's own devices, and a session that is not
theirs is answered as if it did not exist. The tools list the sessions and their pending
decisions, read a bounded tail of one session's replay buffer (`ReplayBuffer.tail`), and draft a
message that the hub sends as the account's own app would (`Hub.send_session_as`), but only after
the assistant has read it back and the person has said yes or pressed Send. The gateway stores
nothing of a call, neither the audio nor the transcripts nor what a tool returned, and its one log
line per call holds ids, counts and timings. A client holds its own side of a call, the socket, the
microphone and speaker and the saved conversations, which stay on its device; the gateway sees one
socket.
Who may call, and what a call costs, are a policy the service is given (`live/policy.py`): the
default is the `LIVE_ALLOWED_USERS` allowlist with a daily cap on call time, and nothing billed.

## A terminal on a device

A device row opens a shell on that machine (A38, `docs/DESIGN.md` § "The terminal"). It is not
SSH: the device dials out as it always has, nothing on the host listens and no key travels. The
device starts the person's login shell in a pseudo-terminal and streams its bytes through the
gateway to the **one app connection** that asked, and the gateway relays those bytes without
reading them — a terminal is one person's view, never broadcast to the account's other sockets the
way session events are.

The pieces: the app sends `terminal.open {cols, rows}` (forwarded by `device_id` like any other
device request), the device replies `{terminal_id}` and from then on publishes `terminal.output
{terminal_id, to, seq, data}` frames — output coalesced for ~16 ms, at most 16 KiB decoded, `seq`
rising by one per frame — where `to` is the app connection id the request carried; the gateway
delivers each to that connection alone, stripping `to` and adding `device_id`, and only when that
connection belongs to the device's account. Typed bytes travel back as `terminal.input`, the view's
size as `terminal.resize`. The gateway remembers which connection holds each terminal from the
`open` and `attach` replies; when that connection closes — a phone locked, a tab gone — it asks the
device, on its own account, to `terminal.detach`, and the device stops streaming but keeps the
shell and its 64 KiB scrollback ring for ten minutes so a `terminal.attach` from any connection of
the account can take it back, scrollback first, `seq` continuing. `terminal.close` ends the shell
(SIGHUP, then SIGKILL); a shell ending on its own is a `terminal.exited` to the holder. A device runs
at most four terminals, ends them all when its own process ends, says in `hello` whether it offers
the capability at all (`terminal`, a `[terminal] enabled` switch in its configuration), and logs
only that a terminal opened and closed — never what went through it. The apps render the bytes
with a real emulator (xterm.js on the web) and, on the phone, put the keys a
shell needs on a bar above the keyboard.

## Devices keep themselves current

The gateway serves one client wheel and knows the build every device runs from its `hello`; when
the two differ it asks the device to update itself, on its own account, and nobody presses anything
(A36). `rc_gateway/auto_update.py` is the whole policy, so the hub keeps routing frames and one
place decides who is asked and when: one `device.update {build}` per hello that is behind, sent once
the device has been quiet for 120 s (A56) — none of its sessions that is driven or attached
(`control` `remote` or `shared`) has a turn in progress or a message queued, which the gateway
follows from the device's summaries and starts the clock over whenever it changes — or, for a
device that is never quiet, once after six hours, because a client up to 1.18.1 does not guard a
session a terminal shares; a device that answers `conflict`, by its own wider rule, is asked again
when it is quiet and at most every ten minutes, for as long as it stays connected; `unsupported` (a
client installed from source) is left alone for the life of the connection; and a failure — `update.failed`
or a device that never comes back within five minutes — is remembered on the device row
(`update_failed_build`, an additive column) so the gateway never hammers a machine: it tries that
build again only when an app retries or a newer wheel arrives, and a device that reconnects still
behind is shown `failed` with its reason rather than cleared. An accepted gateway request moves the
device to `updating` on the same path an app's does, so the timer, the announcement and the failure
handling are one code path whoever asked. A gateway restart needs no state of its own: every device
re-says `hello`, and the policy runs again from there. The apps show no client version anywhere;
they draw "Updating…", "Update failed · <reason>" and a Retry, and nothing else about the client.

## Account preferences

Some choices cannot live in an app. Whether a session the vendor's usage limit stopped resumes
itself once the limit resets (A35) is acted on by a device while no app is running, and it has to
read the same in the browser and on the phone, so the gateway keeps it: one row per account in a
sixth SQLite file, `preferences.sqlite3`. A41 put the Settings screen's own choices in the same
row — `language`, `stt_language`, `polish_enabled`, `polish_model`, `polish_strength` and
`timeline_detail` beside `resume_after_limit`. An account with no row reads the defaults, so
nothing is seeded, and a deleted account's row goes with its push registrations.

Only `resume_after_limit` always has a value, off until the person turns it on. The other six live
in nullable columns, added to an existing `DATA_DIR` by the store's own migrations on the first
start of the new release, and are left out of the object until somebody sets one: absent is how an
app tells "nobody has chosen" from "chosen, and this is the value", and it is what makes an app
write its own value up once on the day of the upgrade. The gateway validates what it stores — the
word lists of `language`, `polish_strength` and `timeline_detail`, booleans for the two switches,
a non-empty `stt_language` of at most 32 characters, a `polish_model` of at most 128 where empty
means no model — and answers `bad_request` to anything else. The device client reads the switch
and ignores the rest.

`GET` and `PATCH /api/preferences` read and write the caller's own account and nothing else: no
path here names a username. A `PATCH` sets the fields it names and leaves the others alone; if
anything changed, the whole object goes out at once — `preferences.updated` to every app socket of
the account, `preferences` to every device of it — and every device is told again right after
`hello_ack`, so a daemon that was offline while a switch moved acts on the current value without
asking for it. The gateway is the single writer and the order the writes arrive in is the order of
truth, so the last write wins on every screen within the round trip. A `PATCH` that changes nothing
announces nothing. `hello` on `/ws/app` carries the object; a gateway older than A35 sends none,
which is how an app knows to show the resume switch disabled.

What a device does with a paused session is the device's own business (`docs/CLIENT.md`). The
gateway forwards `session.resume_set` and `session.resume_cancel` like any other request, relays
the `resume` events of §5.15 and the `limit` on a `turn_completed` untouched, and turns three of
those events into notifications.

## Push

The gateway watches for four things worth interrupting someone about: a session that needs approval,
one that needs an answer, a completed turn, and an error. Three more come from A35, and they are not
state changes — a session with a resume pending is idle like any other — so they are cued by the
`resume` event itself: `limit_reached` on `scheduled`, `resumed` on `fired`, `resume_dropped` on
`dropped`. A time that moved (`rescheduled`) and a resume a person ended (`cancelled`) are silent.
It sends Web Push through VAPID and APNs through an ES256 provider token, with a payload that names
the device and the reason and nothing else:

```json
{"rc": {"v": 1, "kind": "needs_approval", "device_id": "…", "session_id": "…",
        "device_name": "mac-studio-office", "title": "…", "badge": 2}}
```

A notification fires on an observed *transition* between two states, unless an app that is
actively watching the session (subscribed to it, and used within the last minute) already shows
it. The hub reports the sign-ins of those apps, the `jti` of each socket's bearer token, and the
push service decides what each one silences: any watcher silences Web Push, but the alert for the
phones is silenced only by a watcher that is not one of them, one whose sign-in has no live APNs
registration. The iPhone app registers for APNs under the same sign-in its socket carries and stays
subscribed when it goes to the background or the phone locks, so its own socket never stands in for
the alert; in the foreground it drops the banner itself. A session the gateway is seeing for the
first time has no previous state, so a device reconnecting with a hundred sessions produces no
notifications at all. The three resume kinds follow the same rule: the app that has the session
open draws the notice above the transcript, and a push on top of it would say the same thing twice.

Delivery is an outbound HTTP call to a browser vendor or to APNs, so it runs as a task rather than
inside the frame handler that noticed the transition: awaiting it there would hold every later
frame from that device behind a third party's response time. Whether an app is watching is decided
at the transition, not when the call goes out, so running late cannot change the outcome. A
shutdown gives the calls already in flight five seconds to finish before cancelling them. That wait
starts only after the server has given open connections up to five seconds to close
(`timeout_graceful_shutdown`) and stopped waiting for those still open, such as a socket whose
client has stopped reading, which would otherwise never close and keep the shutdown from getting
this far.

Every payload also carries the account's count of sessions marked `unseen` (A47) — `rc.badge`, and
`aps.badge` on APNs — so an iPhone's home-screen badge is right after any notification it shows.
The count also moves when nothing is pushed: a mark cleared by `session.seen` or by the session
running again, a marked session closed or removed, one that nothing holds any more or that
something holds again, a mark set while an app besides the phone
watched the session. For those the gateway sends the account's phones one badge-only APNs
notification (`kind: "badge"`, an empty title, no alert or sound, `apns-priority` 5) once the count
has been still for three seconds, and only when it differs from the last count it sent; waiting for
it to settle is what keeps a turn that ends on a screen other than the phone's, and is opened a
moment later, from costing a push at all. The phone's own screen gets no such saving: the gateway
cannot tell whether its app is in the foreground, so the phone is alerted either way, and a session
opened afterwards costs one badge-only push, because the alert already carried the count with its
mark. Web Push never carries one, because a browser shows every push it receives. Two consequences
are accepted: APNs keeps only the newest notification for a phone that is offline, so a badge-only
push can replace an alert not yet delivered (as a second alert would), and an alert the journal
retries carries the count it was built with until the next push corrects it.

The count is of the sessions that show the mark, and what shows is not what is kept. A session is
closed when it is archived (A39's close ends in `archived: true`, `control: "none"` and `state:
"stopped"` at once) or its state is `stopped`: it loses its mark for good and the count falls with
it, which costs the phones a badge-only push like any other change. A session that nothing holds
(`control: "none"`) is no close. A device says it of a CLI that exited, and also of a terminal its
scan has not found yet, a pi extension that has not reconnected and a scan that missed a holder,
and it comes and goes. So the mark is kept and not shown while nothing holds the session, which
every app files in its Archive, and shown again when something does; a session that is unheld and
held again within the three seconds the count takes to settle costs no push. A close takes the
device a moment: it interrupts the turn first, and the end of that turn reaches the gateway as an
`idle` summary before the archived one does. So the hub notes every `session.archive` with
`archived: true` it forwards until the device answers, reads what that session reports meanwhile as
part of the close, and cues no notification for it: the person who closed it is not told that a
turn finished, and the count never includes it. The note is the request's own entry in the table of
unanswered requests, which outlives the app socket that asked, so a phone that taps Close and
disconnects at once is not told either, and ends when the device replies, refuses, goes offline or
the request times out after 60 seconds. When it starts, the hub also takes the mark off every
closed session that still keeps one, because only a device replaces the summary it last sent and
one that is away cannot; no app was shown those marks, so the count does not move and nobody is
told. A device that restarts would hide every mark at once, since its hub restores every session
as held by nothing; so its first hello waits for the first scan of its terminals
(`docs/CLIENT.md`) and reports what that scan found, and a terminal that is still running shows
its mark all the same.

Web Push is a blocking library call, so it runs on threads of its own with an explicit ten-second
timeout. Both matter: `pywebpush` passes its `timeout` argument through even when it is `None`, so
its own default never applies and the call would wait until the operating system gave up, and the
loop's default thread pool is what every blocking database call in the gateway uses — the CPU count
plus four threads, at most 32. A vendor that black-holes connections must not be able to queue
logins, device lookups and session-index reads behind it.

Which transport reaches whom depends on who holds the credentials. Web Push works on every
gateway: the gateway generates its own VAPID key pair, and a browser subscribes to that. APNs needs
the app publisher's signing key for the iPhone app's topic (its bundle id), which only the
official gateway holds, so a self-hosted gateway cannot push to the SideCode iPhone app; with
`APNS_*` unset it reports `apns_enabled: false` and the app keeps its local alerts while it runs.
The Mac, Windows and Android apps have no push channel at all, on any gateway — an Android push
would need Firebase Cloud Messaging and a sender on the gateway — so their notifications are local:
the running app posts a system notification at the same moments with the same words, from the
frames it already receives, and nothing on the wire or in the gateway changes. Their icon badges —
the Dock's label, the taskbar's overlay, the Android notification's number — are the app's own
count, from the session list it already holds.

A registration is bound to the account that made it and never changes hands. Both tables are keyed
by the subscriber's own identifier — a push endpoint URL, an APNs device token — which is not a
secret the gateway issued, so registering one that already belongs to another account is refused
with `conflict` and deleting one only ever deletes that account's row. Without this, anyone who
learned an endpoint could move someone's notifications onto their own account and have their own
sessions delivered to that person's phone.

## Storage

| Where | What |
| --- | --- |
| Gateway `DATA_DIR` | `auth.sqlite3` (issued login sessions), `devices.sqlite3` (devices, pairing codes — both hashed — and, between a clean stop and the next start, the ids of the devices that were online and when, A56), `sessions.sqlite3` (the latest summary per session, with its `unseen` mark in a column of its own, A47), `push.sqlite3` (Web Push subscriptions, APNs tokens, a delivery journal), `users.sqlite3` (the accounts and their hashed passwords), `preferences.sqlite3` (one row of preferences per account, A35, A41), `session_secret` and `vapid_private.pem`. The databases and both secrets are created at 0600 |
| Gateway memory | Live connections, the per-session replay buffer, and a cache in front of the login-session store that also holds the event which closes a socket the moment its session is signed out |
| Device `~/.rc-client` | `config.toml` at 0600 (gateway origin, device id, device token, name), `state/rc-client.sqlite3` (sessions, events, a key-value table, and request ids for `session.send` idempotency), `state/attachments/`, `state/channel.sock` and `state/claude-mcp.json` for the attachment, `state/daemon.lock` (the single-instance lock), `bin/claude` when the shim is installed, `logs/` |
| Browser | The session cookie, plus whatever the service worker caches of the built app |

Schema changes on the gateway are additive only: `CREATE TABLE IF NOT EXISTS` cannot add a column to
a database that already exists, so each of the six stores declares the columns it has gained since
its first release and applies the missing ones every time it opens the file. An existing `DATA_DIR`
is migrated in place, which is what makes an image update over a live volume safe. A new store is
the other additive move: `preferences.sqlite3` appears beside the rest on the first start after the
upgrade, with no migration to run. Nothing is dropped or renamed.

A login session is a row rather than a process-local entry, so a token issued before a restart is
still valid afterwards: the first request that presents it misses the memory cache, finds the row,
and hydrates an entry. Signing out writes a revocation that outlives the process, and expired or
revoked rows are pruned at startup.

## Design decisions

**Python for the gateway and the daemon.** The daemon's job is driving `claude-agent-sdk` and a
`codex app-server` subprocess, and the SDK is Python. Sharing the language across both backend
components means one set of conventions, one test style, and protocol code that can be read side by
side. The cost is a heavier runtime than a Go binary would need; for a service that spends its life
waiting on sockets, that has not mattered.

**The device owns history.** The alternative — a gateway that stores every event — would make the
VPS the thing you have to back up, secure and scale, and would put your prompts and tool output on
a machine that has no other reason to hold them. Keeping history on the device makes the gateway
disposable: wipe its volume and you lose sessions' *index*, not their content. The price is that
history is unavailable while a device is offline, and that the replay buffer has to be big enough to
cover ordinary reconnects.

**Opaque forwarding.** The gateway could parse events and serve rich queries. It deliberately does
not: every field it understands is a field it can leak, log or get wrong across versions. Parsing
only the envelope also means a protocol addition needs no gateway change.

**One block timeline instead of raw agent events.** Both agents emit their own event vocabulary, and
more agents are planned. Normalising on the device — where the agent-specific knowledge already
lives — keeps every UI free of agent-specific branches, and makes a third agent a device-side change
with no protocol change.

**PCM16 streaming for voice.** Sending a recorded file is simpler, but the wait after you stop
talking is exactly the moment the interaction feels slow. Streaming raw PCM lets partial transcripts
appear while you speak, and 16 kHz mono is what every speech backend wants anyway. The cost is an
AudioWorklet and a resampler in each client.

**Accounts are people, and nothing is shared between two of them (1.1, A24).** Version 1.0 had one
password and one user. The gateway now keeps accounts — `admin` from `RC_PASSWORD`, members by
registration while the admin allows it, or made by the admin — and every device, pairing code,
session and push registration belongs to exactly one of them. Isolation is enforced in one place:
the hub resolves the owner of a device once and fans every device-derived frame out to that
account's sockets only, and a subscribe or a forward naming another account's session is answered
as if the session did not exist. Passwords are stored as salted scrypt hashes; the admin's is
never stored. Workspaces, sharing a device between two people, and per-project roles are still
deliberately absent: a device is one person's machine.

## The contract

`protocol/PROTOCOL.md` is normative. Its amendments (A1…A55, dated at the end of it) are all part
of the frozen contract; the first eleven, which shaped the architecture above, are:

| Amendment | Ruling |
| --- | --- |
| A1 | The tool category is `tool_kind`; `kind` is only the event discriminator |
| A2 | `input_tokens`, `output_tokens` and `total_tokens` are required; cost and context are optional |
| A3 | Attachments are `{name, mime, size}` on events, `{name, mime, data_base64}` on `session.send` |
| A4 | Close codes: 4401 credential, 4403 forbidden, 4001 device connection replaced, 1008 protocol violation. 4001 is device-only; an app that sees it must not auto-reconnect |
| A5 | The gateway stamps `device_id` on `session.event` and `session.removed` frames to apps |
| A6 | The `session.subscribe` reply carries the latest `queue` snapshot |
| A7 | `readonly` means terminal-controlled *and* idle; the composer is gated on `control` |
| A8 | Blocks order by `first_seq ?? seq` |
| A9 | `session.history` accepts `after_seq`, and the gateway uses it to backfill after a device outage |
| A10 | `control: "shared"` for an attached terminal session; `user_message.delivery`; the relayed approval offers only Allow and Deny |
| A11 | Codex attaches through its shared app-server daemon; `shared_settings` and `shared_attachments` say what a shared session carries; a Codex approval offers the daemon's own options and ends as `elsewhere` when the terminal answered |

Every component's tests decode the fixtures in `protocol/fixtures/`, which is the cheapest way to
catch a diverged model before integration.
