# Live voice assistant (EXPERIMENTAL)

Status: experimental. It is off unless switched on in `.env`, and it is built to be changed or
deleted whole. Who may call and what a call costs are a policy (`live/policy.py`): the default one
serves the accounts in `LIVE_ALLOWED_USERS`, each held to a daily call-time cap, and bills nothing,
and a deployment that bills its calls gives the service a policy of its own (see "Policy"). It has
been tried on real calls (see "What real calls showed"); apart from what those showed, every
behavior is tested against a fake server written from the documents listed at the end.

Clients: the web panel and the iOS, Android, Mac and Windows apps (closed source) are clients of the
same socket and the same status route. The web panel is the reference and is described under "The
web panel"; an app does what the panel does, with its words. Where a sentence is about the wire,
"the browser" is the client end of the socket, the web page or an app's call, and the wire's own
names keep the word (`browser_closed`, `browser_silent`, `browser_underruns`); what is only true of
the web says "the page".

## What it does

A developer with several coding-agent sessions opens the assistant in the web app or in one of the
apps, taps to start and talks. It answers what is running and in what state, what is waiting for a
decision (project, decision, options), short summaries of a session's recent context, and drafts a
message to a session. It supervises; it never codes, and it cannot approve or answer anything in
v1. When the client says which session's page is open, "this session" means that one.

## What leaves the gateway

While a call is open the gateway sends OpenAI: all the microphone audio (silence included), and
what the tools return when the model asks for it: session titles, project folder names, agent
states and details, the options and commands of pending approvals and questions, the trimmed tail
of a session's transcript (the agent's output and the developer's own earlier messages), and the
text of the drafts the model prepares. That text can contain whatever a coding agent printed,
secrets included; the prompts tell the model never to read secrets aloud, which is not a control.
The ids of the session page the browser says is open (`focus`) stay in the gateway, but they
decide one thing that does leave it: the `list_sessions` result carries `"on_screen": true` on
that session, so OpenAI learns which of the listed sessions the developer has open.
Do not use the assistant on sessions whose content must not reach OpenAI. What OpenAI keeps is set
by the OpenAI account the key belongs to, not by this gateway.

The gateway itself keeps none of it: nothing said, transcribed or returned by a tool is stored or
logged (logs hold ids, counts, event types, tool names and close codes), and a draft lives in memory
for at most 3 minutes.

## Architecture

```
browser <== WS /ws/live ==> gateway <== wss://api.openai.com/v1/live/sessions ==> Live API
  PCM16 mono 24 kHz           tools, limits, pacing,         PCM16 mono 24 kHz in base64
  + small JSON messages       barge-in, usage record         + the Live API's events
```

No client talks to OpenAI. The gateway holds one WebSocket to the Live API for the whole
call (the Live API's "primary WebSocket" transport), authenticated with `LIVE_OPENAI_API_KEY`; the
session lives exactly as long as that socket, so there is no separately created session that could
be left behind. Tools run in the gateway, on that same connection, as the calling account.

### Browser to gateway: `WS /ws/live`

Authenticated and origin-checked exactly like `/ws/stt` (bearer or cookie; a cookie from a foreign
origin closes `4403`, no login closes `4401`). The upgrade is rate limited per address. Binary frames
are raw mono signed 16-bit little-endian PCM at 24 kHz; text frames are JSON with a `type`.

| Direction | Message | Meaning |
| --- | --- | --- |
| up | binary, 10 to 100 ms (20 ms advised) | Microphone audio. Odd or out-of-range sizes and anything faster than 1.25x real time (after a 2 s burst) close the call with `1008` |
| up | `hangup` | End the call |
| up | `captions {enabled}` | Whether the gateway should send `caption` messages (off until asked; the web panel asks, once, right after `ready`) |
| up | `draft.send {draft_id}` / `draft.cancel {draft_id}` | The panel's Send and Cancel buttons |
| up | `focus {device_id, session_id}` | The session page that is on screen: two strings of at most 200 characters, or both `null` when there is none. The latest is kept for the call, and anything else means no focus. It marks one row of `list_sessions` (see "Tools and drafts") and decides nothing else |
| up | `playback {underruns, gap_ms, restarts, delay_ms}` | The browser player's report: its cumulative counters (three non-negative integers, clamped to 10^9) and its playout delay in ms (0 to 2000, clamped), the time it holds each frame before playing it. Sent right after `ready`, then at most every 5 s while anything changed, and once before `hangup`. The counters count only together and the delay on its own; whatever is not a non-negative integer is ignored. The latest are kept for the usage line, and the delay is added to the end of the assistant's voice (see "Playback and barge-in"); 400 ms is assumed before the first report |
| down | `ready {sample_rate, frame_ms, seconds_left}` | The session has started; the browser may stream audio |
| down | binary, at most 40 ms | The assistant's voice, paced (see "Playback") |
| down | `flush` | Drop everything queued for playback now |
| down | `state {speaker}` | `user`, `assistant` or `none` |
| down | `working {on}` | `true` while a backend response is in flight or a tool is being answered, `false` when neither is; sent only when that changes, so the first one of a call is a `true`. `ended` means it is over |
| down | `caption {speaker, delta}` | A transcript fragment, only when asked for and never kept |
| down | `draft {draft}` | The pending message `{id, session, text}`, or `null` |
| down | `draft.result {draft_id, ok, session \| error}` | What a Send press came to. A spoken yes that sends the draft sends the same message with `ok: true` and the session's label, so the panel shows it as sent; a refused or failed spoken send sends nothing. Either way it comes before the `draft` update that clears the card |
| down | `notice {code}` | The Live API reported an error and the call goes on; the code only, never its text |
| down | `time {seconds_left}` | Every 3 s; a browser that hears nothing for 10 s drops the call |
| down | `ended {reason}` | Always last, then the socket closes |

Text frames from the browser are commands: at most 4096 characters, JSON objects, at most 20 a
second (a burst of 20), and at most 4 buttons' work unfinished at once; anything else closes the
call with `1008`. A person pressing buttons never meets those limits; a client that floods, or
that stops reading what the gateway answers, does. `focus` and `playback` count against the same
rate, are never answered, and never end a call because of what is in them: a `focus` with bad
values means no focus, the unusable parts of a `playback` are ignored, and a command of a type the
gateway does not know is ignored. Close codes: `1000` normal, `1001` gateway stopping, `1008`
protocol error, `1011` internal error, `4402` the account's plan or allowance does not cover the
call (`subscription_required`, `quota_exceeded`), `4403` not allowed, `4409` a call is already
open, `4429` busy, cooling down or out of the day's time, `4502` the Live API refused, dropped or
kept failing, `4503` billing cannot be asked right now, or failed twice in a row during a call
(`billing_not_configured`).
The default policy never sends the reasons that come with `4402` and `4503`; a policy that bills
its calls does.
`GET /api/live/status` is the only HTTP route (404 when the feature is off). It answers
`{enabled, available, reason}` for the caller: whether the entry is shown, whether a call can start
now, and why not (`reason` is `null` while it can, else the reason a call started now would be
refused with). With the default policy that is `not_allowed` for an account that is not listed,
where `enabled` is `false` too, so the entry stays hidden. A policy that bills its calls can say
`enabled` for every signed-in account and give `subscription_required`, `quota_exceeded` or
`billing_not_configured` as the reason, so that the panel can tell the account why instead of
offering Start. The status is for the panel; the check at the socket is what enforces.

### Gateway to the Live API

- URL `wss://api.openai.com/v1/live/sessions` (the model is named in `session.start`, never in the
  URL), header `Authorization: Bearer <key>`. Compression is off. The library's own logger for this
  socket (`rc_gateway.live.wire`) is pinned at WARNING, so no log level shows its frames or its
  handshake (which carries the key).
- A connection the Live API refuses outright (an HTTP status instead of the upgrade, such as 403
  for a country, region or territory it does not serve) ends the call `upstream_refused` (4502). The
  line `live upstream refused the connection` carries the status and the `code` of the error in
  the answer's JSON body (`unsupported_country_region_territory`, say), only when it looks like an
  identifier; nothing else of the answer is read, neither its message nor its headers.
- First message `session.start` with the `session` configuration; audio starts after
  `session.started`. The configuration (`live/prompts.py`): `model`, `instructions` (the voice
  model's prompt, built as the Live prompting guide describes, with the backend's capabilities and
  when to generate `<|SpawnThinking|>`), `audio.format` `{type: "audio/pcm", rate: 24000}`,
  `audio.output.voice` only when `LIVE_VOICE` is set, and Responses delegation with the backend
  model, its own prompt, six function tools, `tool_choice: "auto"` and `parallel_tool_calls: false`.
  The prompts name the product SideCode when speaking English and 随码 when speaking Chinese, from
  the names kept in `live/prompts.py`.
- What the prompts say about waiting and about sessions. The voice model's delegation policy has
  it say one very short acknowledgement in the developer's language ("Let me check.") whenever it
  generates `<|SpawnThinking|>`, then wait quietly: nothing more while the backend works, and no
  filler while the developer is talking. It is also told that the backend knows which session is on
  screen, so "this session" is a clear request and needs no clarifying question. The backend's
  prompt has it pass a session to a tool by the id `list_sessions` returned, never by a title or a
  project name (those can match several sessions, and the tool then refuses with
  `ambiguous_session`, which costs another backend response), never say an id aloud, and call
  `list_sessions` first when it has no id. "This session", "this one" and "the one I am looking at"
  mean the entry marked `on_screen: true`; with none marked, it asks which. The `session`
  argument's schema description says the same as the first rule.
- Up: `session.input_audio.append {audio}` (base64) for every microphone frame, silence included,
  as the guide asks ("keep input audio running during silence").
- Down, used: `session.output_audio.delta {delta}`, `session.input_transcript.delta` and
  `session.output_transcript.delta`, `response.event` (the backend's nested Responses events),
  `session.usage.updated`, `session.closed` and `error`. Everything else is ignored.
- A function call arrives as an inner `response.output_item.done` with a `function_call` item; it is
  answered once (by `call_id`) with `response.item.create` (`function_call_output`) followed by
  `response.create`.
- `error` events are logged by identifier only (`code`, `type` and `param`, and only when they look
  like identifiers) and passed to the browser as `notice {code}`; the text is never forwarded.
  Three in a row, with nothing else heard between, end the call (`upstream_errors`).
- `session.thinking.append` (quiet, application-written context; `delegation_id` is required and
  nullable, and `null` means general session context, per the SDK's `ThinkingAppendEvent`) tells the
  voice model when the panel's Send or Cancel button was used, so it does not ask again.
- `session.close` ends it; the gateway waits up to 5 s for `session.closed` (final usage) and then
  closes the socket regardless. A call that ended before the gateway began reading the socket (the
  browser left while the session was being started) has nobody to hear `session.closed` and closes
  at once.

### Playback and barge-in

The Live API has **no speech-started, speech-stopped, interruption, truncation, cancel or
response-done event** (not in the guides, and not in the SDK's server-event union), takes no
turn-detection setting, and says playback is the client's job: "a sideband alone does not control
playback" and "track playback in your own audio queue". The model decides itself when to stop
talking; what the application must do is stop playing audio the person has already talked over.
Without an event to react to, the gateway listens for itself:

- **Pacing** (`live/playback.py`). The gateway holds the assistant's audio and releases it at the
  speed of speech, keeping the browser's queue up to 1.5 s deep (`LEAD_SECONDS`): the first 1.5 s
  of a reply goes out at once, and the queue is then topped up as it plays. The depth costs nothing
  at a barge-in, because the browser's `flush` stops every scheduled source at once and the
  gateway drops what it still holds. It helps only when the model runs ahead of real time, which
  real calls have not shown it to do (its audio arrives at exactly real time, see "What real calls
  showed"): there the pacer passes the stream through. It stays, being harmless and what a service
  that does run ahead would need. At most 30 s is held; if the model runs further ahead
  than that, the oldest audio is dropped and the call goes on, so the newest words are the ones
  heard. The jitter buffering is the browser's, by its playout delay (next).
- **Playout delay** (`playback`'s `delay_ms`). The browser holds each frame for its playout delay
  before it plays it, so the voice is heard that much later than the gateway sent it. The gateway
  adds the delay the browser reported (400 ms before the first report) to the end of the voice:
  `Playback.audible()` counts the voice as heard until the browser has played it. It answers what
  the `state` message says, whether a person starting to speak may be interrupting (barge-in) and
  so whether the confirm gate takes the assistant to be speaking (a spoken yes is not an answer
  while the read-back is still being heard), whether the call is idle, and whether the assistant
  was already audible when the reply timer starts. The delay moves nothing else: not the pacing,
  not the starvation estimate, and not the model's own silence that confirms a barge-in.
- **Starvation** (`OutputPacer`). The pacer estimates when the browser will have played everything
  it was sent, taking each frame to play in real time from the moment it was sent. Audio that
  arrives while nothing is held, after something was sent since the last interruption, and when
  that playhead passed between 50 ms and 2 s ago, is the model running late in the middle of a
  reply: it counts as one starvation of the time between the two, and the longest such time is
  kept. A gap under 50 ms is not counted: on a stream that arrives at real time it is timer and
  network jitter, which the browser's playout delay absorbs, and counting it would bury the
  stalls. A longer silence is a new reply, and an interruption starts the count over (the empty
  queue that follows one is the person's doing). It is an estimate from the gateway's side only;
  the browser reports its own counters (`playback`), and the usage line carries both. The stream
  seen on real calls is continuous, at real time and with stalls that are never caught up, so
  `starved` mostly counts stalls upstream of the gateway.
- **Voice or silence.** The output is a continuous stream with digital silence between replies:
  the third-party guides describe "a continuous output stream" and say "a frame of output audio
  doesn't count as speech", and real calls show it (see "What real calls showed"). So the gateway
  classifies every 40 ms frame of the assistant's audio with the same kind of energy detector
  (absolute floor about -47 dBFS, 3x the stream's own noise floor, which is capped so a long
  stretch of speech cannot become the floor). Silence is still sent to the browser, in order, but
  "the assistant is speaking" (the `state` message, the barge-in rule, the idle hang-up and the
  read-back gate) means voice held or being heard, with the browser's playout delay, plus a
  half-second pause.
- **Detection** (`live/vad.py`). An energy detector on the microphone frames the gateway already
  receives: speech is audio above both an absolute floor (about -38 dBFS) and 3x the room's level
  (the quietest of the last 3 s), for 100 ms; it ends 300 ms after the last loud frame. While the
  assistant is audible, speech begins only at about -30 dBFS (`OVER_ASSISTANT_THRESHOLD`), held for
  300 ms without a dip (`OVER_ASSISTANT_ONSET_MS`): what a loudspeaker brings back of the
  assistant's own voice through the echo canceller is mostly fainter (a Mac call measured -40 to
  -43 dBFS) and comes in short peaks, while a person who means to cut in speaks up and goes on for
  a syllable or two. Speech already under way goes on at the usual level.
- **Barge-in** (`live/barge_in.py`). Speech that begins while the assistant is audible is only a
  suspicion: on a loudspeaker the assistant's own voice, coming back through the microphone, can
  start the detector's speech (see "What real calls showed"). The model shows which it was. Its
  stream is continuous, so a model that stops for the person streams silence, and one that sends
  nothing at all instead is as quiet: time past the end of the audio received, each piece counted
  from its arrival at real time (so audio sent ahead of time is no gap), counts as silence. The
  suspicion is confirmed when the model, still talking as the person began (quiet for less than
  0.5 s), goes quiet within 1 s of the speech start (`CONFIRM_WINDOW_SECONDS`) and stays quiet for
  0.5 s (`YIELD_SILENCE_SECONDS`, the pause after which the assistant no longer counts as
  speaking, so a pause between its words does not count). Only then does the gateway do what it
  once did at the speech start: drop what it holds, send `flush`, and discard what the model still
  sends while the person goes on talking (0.3 s past the last loud frame, at most 3 s from the
  confirmation). The browser's player stops every scheduled source at once. A real interruption is
  so cut off at most 1.5 s after the speech start, and as soon as the model has been quiet for
  half a second; the window is no longer because a longer one would let more pauses between the
  model's sentences pass for stopping. A model still talking as the window closes, or one that had
  already stopped (only its last words were still being heard), was not interrupted: nothing is
  flushed or discarded, the browser plays on, and the suspicion counts as a false interruption. A
  new speech start over the assistant before the last one is decided gives that one up as false and
  gets a window of its own, so a person who cuts in soon after an echo is judged on their own
  speech. While a suspicion waits, the model's audio is taken in and sent as usual, and a new
  suspicion ends an earlier interruption's discard, so that discard cannot be drawn out over audio
  that is only suspected. The decision is taken on every microphone frame and every piece of the
  model's audio, and on the call's watch tick in case neither comes. Nothing is sent to the Live
  API: there is nothing to cancel or truncate, and the model, which hears the person, stops itself.
  It is not told what the person heard (the guide: "the model doesn't know what the user heard").
- **What it still cannot tell.** An echo whose speech start falls within about half a second of
  the end of one of the assistant's sentences, or of a pause in it, is confirmed by that quiet as
  if the model had stopped for a person, and the short tail the browser still had queued is cut
  off; a stall of 0.5 s or more in the stream of a model that is still talking is taken for
  stopping too (real calls have shown stalls of 0.15 to 0.7 s).
- **Tuning.** Four numbers decide all of this, and real calls on a test gateway are what tune them:
  the window (`CONFIRM_WINDOW_SECONDS`, 1.0 s) and the quiet that confirms (`YIELD_SILENCE_SECONDS`,
  0.5 s) in `live/barge_in.py`, and the onset gate while the assistant is audible
  (`OVER_ASSISTANT_THRESHOLD`, about -30 dBFS, and `OVER_ASSISTANT_ONSET_MS`, 300 ms) in
  `live/vad.py`. The `live barge-in` lines say how each speech start over the assistant went
  (`confirmed`, `wait_ms`, `voice_ms`, `start_level`, `segment_ms`, `discarded_ms`; see "Usage
  record"), and the usage line counts them (`interruptions`, `false_interruptions`) beside
  `speech_ms` and `discarded_audio_ms`. A real interruption decided false with `voice_ms` well
  short of `wait_ms` says the model stopped too late for the window; confirmed lines where nobody
  cut in say the quiet is too short or the window too long; `start_level` against the threshold
  says how much echo the onset gate keeps out, and many `false_interruptions` that it keeps out too
  little.
- The same detector and the transcripts drive the idle hang-up; the guide warns that gaps between
  transcript events are not silence.
- **Echo.** The client's own echo cancellation, noise suppression and level control are on (the
  browser's, or an app's own). Echo that still gets past the onset gate starts the detector's
  speech, but the model talks on through it, so it no longer cuts the assistant off (but see "What
  it still cannot tell"); it shows as `false_interruptions`. With `LIVE_BARGE_IN=false` the gateway
  never cuts the assistant off (the model's own handling of an interruption still applies, but
  queued audio is no longer flushed), and an interruption the model stops for still spoils a draft
  being read back.

### Tools and drafts

- `list_sessions`, `get_session_status`, `list_pending_decisions`, `get_session_context`,
  `prepare_message`, `confirm_send` (`live/tools.py`). Every session reference is resolved against
  the caller's own devices only (`live/access.py`: reading a session's events needs a session that
  the ownership check returned); a foreign or unknown id answers `unknown_session`. Session data
  comes from the gateway's session index and replay buffer. Nothing answers an approval or a
  question. The browser's `focus` is kept by the tool runner and does one thing: `list_sessions`
  adds `"on_screen": true` to the one row whose device and session ids both equal it, and only to
  a session the account owns. That row is moved to the front of the list, so the cap of 30
  sessions cannot cut it. A focus on a session the account does not own marks nothing and is never
  echoed, the other tools' answers do not change with it, and no access check, name resolution or
  send looks at it. A tool never raises to the model: a refusal is `{"error": code}`. Logs name a
  tool only when it is one of ours (`unknown` otherwise), so a made-up name cannot reach a log
  line.
- **Everything a coding agent wrote is untrusted** (`live/digest.py`): titles, project names,
  details, actions, prompts, option labels and transcript lines. Each result that carries any of it
  (all six tools' results, drafts included, since they name the session by its title) starts with a
  `notice` saying so; every such string has control and formatting characters stripped and is
  capped (title 80, project, agent, state and device 60, detail 120, action 200, prompt 300, option
  label 80; at most 6 options, 5 decisions per session, 10 waiting sessions, 30 listed sessions; a
  transcript tail 3 500 characters in lines of at most 700). The backend prompt repeats the rule.
- Sending is two-step. `prepare_message` stores one draft per call (replaced by a newer one, expires
  after 3 minutes, at most 2 000 characters, at most 5 sent per call; a failed send restores the
  draft and is not counted). Sending goes through `Hub.send_session_as`: the same checks and side
  effects as an app's `session.send` (mode `auto`) under the caller's account, with no app
  connection attached.
- **`confirm_send` is a soft gate** (`live/gate.py`), built only from what the gateway itself
  observes, in this order, for the draft in question: (1) the model has been handed the draft; (2)
  at least 2 s of the assistant's voice then arrives (the read-back), summed from the draft being
  handed over, without the silence the service streams between replies and without voice from
  before the draft existed; (3) the assistant has been silent for at least 0.5 s after the
  read-back started (no voice held, and 0.5 s since the last of it finished playing), and the
  person's voice then begins, seen by the gateway's own detector on the microphone; (4) an input
  transcript arrives after that. The sum counts voice, not words: it cannot tell a read-back from
  two short remarks that add up to as much (and a sound in a pause between the read-back's
  sentences that is longer than 0.5 s looks like an answer once 2 s have been counted). A refusal
  leaves the sum as it is, so a short read-back that was refused and is read again arms it.
- **An interjection spoils the draft for voice.** The person talking over the assistant, or within
  that half second after it, spoils the draft when the model stops for them (the barge-in's
  confirmation, which decides this with `LIVE_BARGE_IN=false` too): from then on `confirm_send` for
  it is refused with `read_back_interrupted`, whatever the assistant says next (the sum is emptied
  and takes no more voice), until a new draft is handed over, which starts a fresh sum. Speech the
  model talks through (its own voice coming back, say), or that begins after it had already
  stopped, neither spoils the draft nor answers it. A `confirm_send` that comes while the model
  has yet to show which it was is held until it has (at most 1.5 s): a yes followed by a "wait"
  the model stops for is spoiled and sends nothing, as it would have been at the speech start
  before; a yes followed by an echo the model talks through goes ahead. The refusal tells the
  model that the developer interrupted the read-back, to call `prepare_message` again and read the
  new draft back in full, or that the developer can press Send. The other refusals,
  `not_read_back` and `not_confirmed`, tell it to read the whole draft back, that a yes from before
  the read-back did not count, and that the developer can press Send. A late fragment of the
  original request's transcript, and a transcript of noise after the read-back, do not answer it.
  Each refused spoken confirmation logs one `live tool refused` line with the tool, the reason and
  the seconds of voice the gate had counted (codes and numbers only), so the 2 s can be checked
  against real calls. The gate cannot tell a yes from a cough: it only makes sure that the shape of
  an answer happened.
- **The panel is the hard confirmation.** It opens by itself when a draft is waiting: its Send and
  Cancel buttons need no gate. When a spoken yes does send, the panel is told as it is for a press
  of Send: `draft.result` with `ok: true`, then the `draft` update with `null`. The card is not
  cleared while a send is under way (the computer has it until it replies), by either route
  (`live/draft_panel.py`), so that order holds however long the computer takes, and a send that
  fails leaves the card where it was.
- **The `working` hint** (`live/work.py`). A backend response is in flight from the first nested
  event that names its id (`response.created`, in practice) until its final event, and a tool is
  work from the moment the model's call is taken up until its answer has been handed back. The
  response the gateway then asks for (`response.create`) counts as in flight until its first event
  arrives, for 10 s at most, so the hint does not go off and on again between a tool's answer and
  the response that uses it. A response with no final event stops counting after 60 s (its end,
  if it does come, is still timed). The browser is told when the state changes and only then;
  events that name no response count for nothing, so a backend that forwards none leaves `working`
  following the tools alone.

### Every way out closes both ends

One place ends a call (`live/call.py`, `_finish`), and each step runs whatever the one before it did
and is bounded, so neither socket can hold the other (or the call's slot) up:

1. the call's tasks are cancelled and awaited (at most 8 s);
2. the call slot and the cooldown are settled (at most 8 s);
3. side by side, each bounded: the browser is told why and closed (its farewell and its close are
   given 2 s each by the socket link, 8 s at most for the step), and `session.close` is sent to the
   Live API, its final usage waited for (5 s at most) and its socket closed (9 s at most for the
   step);
4. the call's meter is stopped, which settles the open seconds (the default policy's meter
   charges the day's call time, a meter that bills pays its last bill), and its answer is waited
   for, 8 s at most, only so the usage line can say how it came out;
5. one usage line is logged.

A browser that stops reading therefore cannot keep the session open and billing, and a slow Live
API cannot keep the browser waiting. A test runs this against a real server and a client that stops
reading (`tests/live/test_wedge.py`). Stopping the gateway waits for open calls for 8 s at most
before the hub and the index shut down; a call that is still closing then finishes in the
background.

| Trigger | End reason | Browser close |
| --- | --- | --- |
| Hang up pressed, or the browser socket closes | `hangup`, `browser_closed` | 1000 |
| Browser silent for 15 s, a stalled browser | `browser_silent`, `browser_stalled` | 1000 |
| No speech, no playback and no tool running for `LIVE_IDLE_TIMEOUT_SECONDS` | `idle` | 1000 |
| `LIVE_MAX_CALL_SECONDS`, or the session's own `expires_at` if sooner | `max_duration` | 1000 |
| The account's `LIVE_DAILY_SECONDS` run out during the call | `daily_limit` | 4429 |
| The Live API sends `session.closed` (`expired`, `content`, anything else) | `expired`, `content`, `upstream_lost` | 1000 / 4502 |
| The Live API socket drops or stops reading, or reports errors three times in a row | `upstream_lost`, `upstream_stalled`, `upstream_errors` | 4502 |
| Malformed or too-fast audio, a bad or too-frequent control message | `protocol_error` | 1008 |
| The login is signed out | `session_revoked` | 1000 |
| The meter of a policy that bills names a reason: the allowance is used up, the account no longer qualifies, or billing failed twice in a row | `quota_exceeded`, `subscription_required`, `billing_not_configured` | 4402, 4402, 4503 |
| The gateway stops: uvicorn closes the browser socket first (`1012`, so the panel shows the call as interrupted) and the call closes the Live API socket; `live.close()` in the app lifespan, before the hub, covers any call still open | `browser_closed`, `gateway_stopping` | 1012, 1001 |
| The handler is cancelled | `cancelled` | 1000 |
| Refused before it starts, or the Live API cannot start | `not_allowed`, `call_active`, `cooldown`, `too_busy`, `daily_limit`, `upstream_*` | 4403, 4409, 4429, 4502 |
| Refused before it starts by a policy that bills its calls | `subscription_required`, `quota_exceeded`, `billing_not_configured` | 4402, 4402, 4503 |

The slot is freed before either socket finishes closing. A process crash leaves no session behind
either: the Live API ends the session when the socket goes away.

### Policy

Two kinds of rule surround a call. The service's own (`live/service.py`) apply everywhere: one call
per account; `LIVE_MAX_CONCURRENT_CALLS` gateway-wide; a `LIVE_COOLDOWN_SECONDS` pause after a call
ends. Who may call and what a call costs are a `LivePolicy` (`live/policy.py`), which the service is
given:

- `status(username)` is what `GET /api/live/status` answers for the account.
- `admit(username)` lets the account start a call, or raises a `LiveError` whose code is the reason
  (the call's `ended` reason and close code). It is asked first, before a slot is held and before
  anything is dialled, so a refusal costs nothing. It returns an `Admission`: the **meter** the call
  reports to, and the cap on the call's length, if the policy has one.
- The meter (`live/meter.py`) is started when the session has started, ticked once a second with
  the seconds the call has been open, asked for a reason to end the call (a `verdict`), and stopped,
  once, with the seconds the call was open, however it ended. Nothing in a call waits on it: what it
  has to pay for it pays behind the call.

The default policy, `AllowlistPolicy`: only accounts in `LIVE_ALLOWED_USERS`
(empty means nobody; names are matched the way logins are), and `LIVE_DAILY_SECONDS` of call time
per account per UTC day. The day's time is counted in memory (a restart starts the day afresh) from
the wall time of each call, charged when it ends; a call is held to what is left of the day (it ends
`daily_limit`), and a call is refused with `daily_limit` once nothing is left. A call whose browser
sends nothing is dropped after 15 s, so a client that only opens the socket costs at least 15 s of
Live API session.

A deployment whose calls are billed passes `mount_live` a policy of its own, and `live/` imports
none of it.

### Usage record

Every call that reached the Live API logs one line, `live call usage`, with ids and numbers only
(never what was said, transcribed or sent): `account`, `call_id`, `session_id`, `reason`, `seconds`
(wall time), `interruptions`, `false_interruptions`, `messages_sent`, then what OpenAI reported,
what the gateway measured and what the meter billed. `interruptions` counts the speech over the
assistant that the model stopped for and the gateway cut off (`flush`; none with
`LIVE_BARGE_IN=false`), and `false_interruptions` the speech starts over the assistant that it did
not stop for (see "Playback and barge-in").

What OpenAI reported:

- `voice_seconds`: the Live API reports its own audio **only as cumulative voice seconds**
  (`session.usage.updated`, final in `session.closed`; each value replaces the last), no tokens and
  no audio/text split. `usage_final` says whether `session.closed` confirmed it.
- `backend_responses`, `backend_input_tokens`, `backend_output_tokens`, `backend_cached_tokens`,
  `backend_reasoning_tokens`: the delegated backend's ordinary Responses usage, summed from the
  nested `response.completed` (also `incomplete`, `failed`) events, once per response id.

What the gateway measured, to show where the time of a call went. Whatever did not happen is `0`;
a median, a maximum or a latest value with nothing behind it is `null`:

- `discarded_audio_ms`: assistant audio thrown away because the person talked over it and the model
  stopped for them (what the gateway still held when that was confirmed, less the quiet it ended
  with, which said the model had stopped, plus what the model sent during the discard window that
  followed).
- `dropped_audio_ms`: audio dropped from the front because the model ran more than 30 s ahead.
- `starved`, `starved_ms`, `starved_max_ms`: how many times the browser's queue ran dry in the
  middle of a reply because the model's audio came late (gaps of 50 ms or more), the silence that
  cost, and the longest single gap (`null` when there was none), by the pacer's own estimate (see
  "Playback and barge-in").
- `replies`, `reply_ms_median`, `reply_ms_max`: how long the assistant took to start speaking once
  the person stopped, from the last loud microphone frame of their utterance (when the detector
  ends it, so the detector's 0.3 s hangover is inside the figure) to the first voiced frame of the
  assistant's audio reaching the gateway. An utterance counts only when the assistant was not
  already audible as the detector ended it, the person did not speak again before it answered, and
  its voice starts within 30 s. With delegation the first voice is usually the acknowledgement, so
  this is the time to a first word, not to the answer. It is measured at the gateway: the network
  to the browser (about one round trip, there and back) and the player's start delay come on top.
- `backend_ms_median`, `backend_ms_max`: per backend response, from the first nested
  `response.event` that names the response's id to its final event (`completed`, `incomplete` or
  `failed`). A response whose start was not seen is not timed.
- `tool_ms_max`: the longest a tool took to run.
- `browser_underruns`, `browser_gap_ms`, `browser_restarts`, `browser_delay_ms`: the browser
  player's own cumulative counters and its playout delay as it last reported them (`playback`), or
  `null` when it never did.
- `speech_ms`: how long the person spoke by the detector's reckoning, the microphone audio it held
  to be speech from each utterance's onset to the end it found (its 0.3 s hangover included).
- `input_transcripts`: how many `session.input_transcript.delta` events came back, whatever they
  said.

Reading it after a call that felt slow: `reply_ms_*` and `backend_ms_*` are time spent in the voice
service and the model, which the gateway adds nothing to (apart from the detector's 0.3 s).
`starved` (and `starved_max_ms`, the longest stall) agreeing with `browser_underruns` says the
model's audio was late; a browser that reports more underruns than `starved` lost audio after the
gateway had sent it on time (the network between them, or the tab). A large `discarded_audio_ms`
with few `interruptions` is long replies cut off once; many `false_interruptions` are echo or
noise reaching the detector, which the model talked through and which cost no audio; many
`interruptions` with little speech still point at echo (`LIVE_BARGE_IN`). For a long call with
almost no assistant audio: a `speech_ms` of 0 says the person never spoke (or the detector never
heard them); `speech_ms` with no `input_transcripts` says the speech did not reach the service as
words; and both present with `replies` of 0 say the model did not answer.

Each speech start over the assistant also logs one `live barge-in` line once it is decided, with
numbers only:

- `call_id`, and `confirmed`: whether the model stopped for it.
- `segment_ms`: from the first voice of what the assistant was saying reaching the gateway to the
  speech start. The browser plays that voice its playout delay later, so the delay is inside it.
- `start_level`: the mean absolute amplitude (of 32 768) of the 100 ms of loud audio that started
  the speech. The detector's floor is 400, about -38 dBFS.
- `wait_ms`: from the speech start to the decision. On a confirmed one the model stopped about
  500 ms before it, when the silence that confirmed it began; a false one decided at `0` began
  after the model had already stopped.
- `voice_ms`: the model's voice that reached the gateway meanwhile. On a false one, a `voice_ms`
  close to `wait_ms` is a model that talked through it.
- `discarded_ms`: what the interruption threw away, less the quiet that confirmed it, and `0` for
  a false one. A confirmed one is written when its discard is over, so this is all of it.

Whatever is undecided when the call ends is written then, as a false one.

What the meter reported (`record()` in `live/meter.py`):

- `billed_seconds`: the seconds the meter sent to be billed, if it bills (the meter stops after both
  sockets are closed). `0` with the default policy, which bills nothing.
- `billing`: how the meter's latest billing request that has an answer came out, or `null` while
  none has (nothing is billed, or the call ended before the first answer came). Always `null` with
  the default policy. The usage line is written after the meter's last answer, waited for 8 s at
  most, so for a call that bills it normally says how the call's final seconds came out.

## The web panel

`web/src/features/live/`. The entry is a button that renders nothing unless `GET /api/live/status`
says `enabled`. The status (`useLiveStatus.ts`) is asked once per page load for each account: an
answer that was a failure is not kept, one account's answer is never taken for another's, and a
call is offered only by an answer that says `available: true`: one with `enabled` alone gets the
generic note, "not enabled for this account", and no Start.

An account that is offered the assistant but cannot have a call now (`enabled`, not `available`,
which only a policy that bills its calls says) gets the panel with the reason in place of Start (and
no empty history). Opening the panel asks the gateway again, so a reason that has stopped being true
needs no reload. The reason is worded the same way wherever it arrives: in the status, as the
refusal of a call before it starts, or as the `ended` reason of one that was under way.

| Reason | The panel says (English; 中文 in `strings.ts`) |
| --- | --- |
| `quota_exceeded` | "The voice assistant is paused on this account for now", saying remote control and typing are unaffected, and no figures. A call that ends for it says the assistant was paused |
| `billing_not_configured` | "The voice assistant is temporarily unavailable. Try again later." A call that ends for it says the assistant became unavailable |
| `subscription_required`, `not_allowed`, and any reason the page does not know | "The voice assistant is not enabled for this account." A call that ends for `subscription_required` says the assistant is no longer available for the account |

### The call belongs to the page

- The call (`liveCall.ts`) is page-level state, not a screen's: the socket, the microphone, the
  audio context and the voice live in a module, and the call's view, the account's saved
  conversations and the draft the panel was last brought up for live in a store (`store.ts`). A
  route change, StrictMode's double mounting and a change of interface language (which only
  re-words the panel) leave it alone. `useLiveHost`, called once in `App` inside the router, tells
  it the two things only its surroundings know: which session's chat is on screen, and when the
  account or the page goes away.
- It ends when the person hangs up (the microphone and the voice stop at once and the gateway's
  goodbye is waited for 1.5 s), when the gateway ends it, when the signed-in account changes or
  signs out (`hangup` goes first, the conversation is saved, and the page drops that account's
  history from memory), and when the page is closed or reloaded (`pagehide`: `hangup` first, the
  conversation saved). Nothing else lets go of it and nothing resumes it: a reload ends it.
- Start asks for the microphone with echo cancellation, noise suppression and level control, dials
  `/ws/live`, and streams 20 ms frames only after `ready`; a frame is dropped rather than queued
  when 96 KiB are already waiting to go. Hang up is available from the moment the call starts
  connecting (a click in the first 0.7 s is taken for the second half of a double click on Start
  and ignored); it releases the socket, the microphone and the audio context, and a microphone
  still being asked for is let go of when the browser grants it. A gateway that has not said
  `ready` in 25 s, or that is silent for 10 s, is taken as gone.

### The entry and the panel

- The entry sits in the top bar, in the chat page's header and in the terminal page's header, one
  at a time as the route changes, and each shows the same call. The panel is a portal on the page's
  body, placed under the enclosing `header` (the bar the button is in), so no bar's clipping moves
  it. With the panel closed the button says a call is on (a dot in the colour of who is speaking,
  and a different accessible name; a muted call shows a crossed microphone). A draft waiting for a
  yes opens the panel, once for each draft, on whatever page the person is on. A change of page
  closes the panel, except that while a draft is still waiting for a yes the entry on the new page
  opens it again.
- During a call the panel shows the status line, the conversation, the draft card, Mute and Hang
  up. The status line says Connecting…, Ending…, Speaking, Hearing you, **Checking…** (the
  gateway's `working` is on, and nobody is speaking), then Muted or Listening, with the minutes
  left. A `notice` shows as a quiet line for 6 s; the call goes on. Refusals and endings are worded
  in English and Simplified Chinese (`strings.ts`), including the daily limit and the three
  reasons about the account above.
- **Mute** replaces each microphone frame with zeros of the same length, so the 20 ms cadence goes
  on and nothing of the person is heard. To the gateway a muted call is a silent one (the idle
  hang-up applies). Nothing mutes it for the person: dictation does not.

### The conversation and its history

- `captions {enabled: true}` is sent after `ready`, always; there is no captions setting. The
  conversation is the turns of each side (a fragment joins the last turn while the same side is
  still talking) and a "Sent to <session>" entry for every successful `draft.result`, whether a
  press of Send or a spoken yes asked for it. The view keeps the last five drafts it showed, by
  id, so the result and the update that clears the card can arrive in either order; a result for a
  draft the page never showed adds no entry, having no text to show. The card then says Sent.
- History is kept in this browser's `localStorage` under `live.history:<username>`: the newest 20
  calls, in about 200 KiB, the oldest call's oldest turns trimmed first (a call with no text left
  goes with them). A running call is saved at most every 2 s after its conversation changes, when
  it ends, on `pagehide` and on sign-out; saves are merged by call id, and a browser that cannot
  store (private mode, a full quota) leaves the page holding them in memory until it closes. A call
  in which nothing was said leaves no record. Nothing of it goes to the gateway.
- With no call on, the panel shows the latest conversation (when it was, how long, its turns, and
  a line when it was cut short, ran out or was interrupted), the other saved calls to open one
  from, "Conversations are kept in this browser only." and Clear history, which asks first and
  deletes the account's saved conversations. The browser is read afresh each time the panel opens,
  so a call another tab saved, or a clear it made, shows.

### What the page tells the gateway

- `focus` after `ready` and whenever it changes: both ids on a session's chat page (the route
  `/sessions/:deviceId/:sessionId`), both `null` anywhere else. It is remembered for a call that
  starts later.
- `playback` after `ready`, then every 5 s but only when something changed, and once more before
  every hang-up of a call that had got as far as `ready`: the player's `underruns`, `gap_ms`,
  `restarts` and `delay_ms`. The player (`player.ts`) is the only jitter buffer in the path, since
  the gateway can build no lead on a stream that arrives at real time. Each frame is one
  `AudioBufferSourceNode` scheduled on the audio context's own clock. A frame that finds the queue
  dry starts a target ahead of now: 0.4 s at the start of a call, growing by 0.2 s after each
  underrun, to 1.0 s (`delay_ms` is the target in force). An underrun is a dry start less than 2 s
  after the last audio ended (a longer wait is a pause between replies, and the start after a
  `flush` is neither); it deepens the target at once, and the frame that found the queue dry
  already starts at the deeper one. `gap_ms` is the silence the underruns made as it was heard
  (the dry time, then that delay), and `restarts` counts every dry start after the call's first.
  Silence is what the buffer is adjusted with: a silent frame (mean level at most 32 of 32 768)
  that finds the queue shallower than the target is moved out to the target, which rebuilds the
  buffer after a stall, and one that finds it more than 0.3 s deeper than the target is dropped,
  which bounds the delay after a burst. `flush` stops everything not yet heard.
- `draft.send`, `draft.cancel` and `hangup`, from the card and the footer.
- The page reuses `web/public/audio-worklet.js` (the dictation microphone's worklet) and
  `web/src/lib/ws.ts` unchanged.

## Configuration (root `.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIVE_ASSISTANT_ENABLED` | `false` | Switch |
| `LIVE_OPENAI_API_KEY` | empty | The key; stays on the gateway. No key, no feature |
| `LIVE_ALLOWED_USERS` | empty | Read by the default policy: comma-separated usernames; empty means nobody |
| `LIVE_MODEL` | `gpt-live-1` | The voice model |
| `LIVE_BACKEND_MODEL` | `gpt-6-luna` | The text model the voice model delegates to |
| `LIVE_VOICE` | empty | `audio.output.voice`; left out when empty (the service default) |
| `LIVE_IDLE_TIMEOUT_SECONDS` | `90` | Hang up after this long with no speech |
| `LIVE_MAX_CALL_SECONDS` | `1200` | Hard cap per call |
| `LIVE_DAILY_SECONDS` | `7200` | Read by the default policy: call time per account per UTC day |
| `LIVE_MAX_CONCURRENT_CALLS` | `2` | Gateway-wide |
| `LIVE_COOLDOWN_SECONDS` | `10` | Per account, after a call ends |
| `LIVE_BARGE_IN` | `true` | Cut the assistant off when the person talks over it |
| `LIVE_OPENAI_BASE_URL` | `https://api.openai.com` | For a proxy or a test double: `https` on any host, or `http` on a loopback address only (the key would otherwise cross the network in the clear). Anything else, or a URL with credentials, keeps the feature off and logs `live assistant stays off` with the reason |

The reverse proxy needs nothing new: `/ws/live` is under `/ws/`, so the WebSocket upgrade
settings that `/ws/app` and `/ws/stt` need apply. The microphone stream keeps the connection busy.

## Where it lives and how to remove it

- `gateway/rc_gateway/live/`, `gateway/tests/live/`
- `web/src/features/live/`, `web/tests/live/`
- `docs/LIVE_ASSISTANT.md`

Outside those: `gateway/rc_gateway/app.py` (the `mount_live` import and call, and the
`live.close()` lines in the lifespan); in the web app, `web/src/layout/AppLayout.tsx`,
`web/src/features/chat/ChatHeader.tsx` and `web/src/features/devices/TerminalPage.tsx` (the
`LiveEntry` import and element in each), `web/src/App.tsx` (the `useLiveHost` import, the call and
its comment) and `web/src/features/chat/chat.css` (the `@media (max-width: 480px)` rule for
`.chat-header-actions:has(.live-entry) .usage-chip`); the `LIVE_*` lines in `.env.example`; and the
one core touch, **`gateway/rc_gateway/hub.py` and `gateway/rc_gateway/replay.py`**:
`Hub.send_session_as`, `Hub.session_tail` and `ReplayBuffer.tail`, plus the `_Origin` /
`_forward_from` / `_deliver` refactor that lets `_forward` and the internal sender share one code
path.

To remove the feature: delete the folders above; remove the lines named above in `app.py`, the web
files, `chat.css` and `.env.example`; and reverse-apply what the feature changed in `hub.py` and
`replay.py`, which is what the commit that added `gateway/rc_gateway/live/__init__.py` did to them.
Check first, then apply:

```sh
first=$(git log --diff-filter=A --format=%H -- gateway/rc_gateway/live/__init__.py)
git diff "$first^" "$first" -- gateway/rc_gateway/hub.py gateway/rc_gateway/replay.py | git apply -R --check
git diff "$first^" "$first" -- gateway/rc_gateway/hub.py gateway/rc_gateway/replay.py | git apply -R
```

No device protocol, schema, dependency or other feature changed.

## Documents this follows

OpenAI, developers.openai.com (read 2026-10-27):

- WebSocket connection: `/api/docs/guides/voice-websockets?api=live` (URL, `session.start`, audio
  format, `session.input_audio.append`, `session.output_audio.delta`, transcripts, closing, errors)
- Managing sessions: `/api/docs/guides/live-conversations` (session fields, `session.closed` reasons,
  `session.usage.updated`, idle audio, graceful close, no truncate or interruption events)
- Delegation and tools: `/api/docs/guides/live-delegation` (function tools, `response.event`,
  `function_call_output`, `response.create`, `session.thinking.append`)
- Prompting GPT-Live: `/api/docs/guides/live-prompting` (instruction structure, `<|SpawnThinking|>`,
  interruption policy)
- Server controls: `/api/docs/guides/voice-server-controls?api=live` (playback is the client's;
  commands and events)
- Cost and usage: `/api/docs/guides/voice-latency-cost?api=live` (voice seconds, backend usage in
  `response.completed`, silence is billed)
- Migrating to GPT-Live: `/api/docs/guides/live-migration` (event mapping; no response-done event)
- The typed event definitions in the OpenAI Python SDK, `src/openai/types/live/` (`SessionStartEvent`,
  `SessionConfig`, `FunctionTool`, `ResponsesDelegationConfig`, `OutputAudioDeltaEvent`,
  `SessionUsage`, `SessionClosedEvent`, `ErrorEvent`, `ThinkingAppendEvent`,
  `SessionResource.expires_at`, and the server and client event unions, which contain no speech or
  interruption events)
- Third-party, for how others handle barge-in: LiveKit's GPT-Live plugin page (the model owns turn
  detection; interruption playback is cut by a VAD on the application's side)

## What real calls showed

The Live API's output is a continuous stream: deltas of 100 ms arriving at exactly real time, with
digital silence between replies, never ahead of real time, and with stalls of 0.15 to 0.7 s that
are never caught up. The first voiced audio of an answer comes about 1.3 s after the end of the
question. So the gateway cannot build a lead and the pacer is effectively a pass-through (its 1.5 s
lead is harmless and stays); the only jitter buffer there is sits in the browser, as its playout
delay (see "The web panel"), which the browser reports to the gateway so that the voice counts as
heard until it has been played. `starved` mostly measures stalls upstream of the gateway.

Delegation adds backend round trips to every answer that uses a tool: the backend calls the tool
and ends one response, and a second response then uses the tool's answer, each a separate call to
the backend model with its own wait for a first byte and its own input tokens. An assistant that
delegates often pays that more than once per answer, which is why the backend is told to pass
session ids to tools: a name that matches several sessions is refused, and the retry is another
round trip.

A call on a Mac's loudspeaker, voice processing on, had the gateway take speech for an
interruption 16 times over 19 replies, back when a barge-in cut off at the speech start. Most of
them were not: the reply timer found the assistant's voice a median 663 ms after such speech
ended, where the discard window ends and well before a real answer comes (about 1.3 s), so the
model had talked on, and the gateway had cut the start or the end out of long replies (21.6 s
discarded at the gateway, on top of what the app flushed) while the captions showed all of it.
Echo of the assistant's own voice is the likely cause; no line per interruption was logged then to
prove it, which the `live barge-in` lines now are. That is why a barge-in now waits for the model
to stop (see "Playback and barge-in").

What makes a call feel slow (a false barge-in, a stall, the backend's round trips, the network
between the browser and the gateway) cannot be read from the logs a call leaves by default. That
is what the timings and counters on the usage line, the `working` hint, the browser's `playback`
report, the spoken acknowledgement and the id rule above are for. A call in which the assistant
hardly spoke leaves one more question the line alone could not answer, whether the person did not
speak or the model did not answer, and `speech_ms` and `input_transcripts` are there to tell them
apart.

## What only a real call can verify

- That the `session.start` body above is accepted as written (the function tools' schema, the
  prompt, `audio.format` 24 kHz, the backend model name `gpt-6-luna`), and that the voice model
  delegates with `<|SpawnThinking|>` as the prompt asks, saying its one short acknowledgement
  first and nothing more while the backend works.
- The nested event shapes: `response.output_item.done` carrying `call_id`, `name` and `arguments`;
  `response.completed` carrying `usage` (the SDK types the nested event only as a dictionary); the
  `error` event's `code`, `type` and `param` that the notice and the logs rely on; and that the
  events a backend response begins with (`response.created`) carry `response.id`, which the
  `working` hint and the backend timings key on (an event that names no response is attributed to
  none, so without it `working` follows tool runs only and `backend_ms_*` stay `null`).
- How long the model's audio takes to stop after an interruption, and whether the browser's
  playout delay and the discard window feel right: `starved` and the browser's `browser_underruns`
  on the usage line say whether the delay was enough. That the audio arrives at exactly real time
  has been seen, but not under load or for long replies.
- That a model which stops for the person goes quiet soon enough to be confirmed, whether it
  streams silence or nothing: the `live barge-in` lines' `wait_ms` and `voice_ms` show whether
  quiet that begins within 1 s of the speech start and lasts 0.5 s fits real interruptions (false
  ones with `voice_ms` well short of `wait_ms` are a model that stopped too late), whether pauses
  between its sentences or stalls in its stream pass for stopping (confirmed ones on speech that was
  not the person's), and whether the onset gate while the assistant is audible keeps echo out and
  lets a person in (`start_level`, `false_interruptions`); see "Tuning".
- That the backend passes ids, so that `ambiguous_session` stops coming up, and that it reads "this
  session" as the `on_screen` entry (a few calls with the panel open on a session page show it).
- The detector's thresholds and the browser's echo cancellation on real microphones and speakers.
- That a session's `expires_at` and any maximum session length match `LIVE_MAX_CALL_SECONDS`, and
  what idle silence costs.
- That the read-back, the person's voice and `session.input_transcript.delta` arrive in the order
  the confirm gate assumes, and that 2 s of voice is the right size for a read-back (the
  `live tool refused` lines say how much the gate had counted). The gate fails closed: a spoken
  yes that does not fit is refused, and the person presses Send.

## Known limits

- No wake word. Reloading the page or closing the tab ends a web call, and nothing resumes it. A
  phone browser that suspends the page in the background is likely to stop the microphone frames,
  and the gateway then drops the call after 15 s of silence from it. The apps keep an open call
  going where the platform allows it, a phone with the screen locked or another app in front and a
  computer with its window minimized; hanging up, closing the last window, quitting or locking the
  app ends a call, and nothing resumes one that dropped.
- The spoken-yes gate is soft: it checks the shape of an answer, not the words. A transcript can
  lag the audio it transcribes, and the assistant is not told what the person actually heard of an
  interrupted reply.
- Captions go through the gateway while they are on, and the gateway never stores them. The
  conversation history is the client's own: per browser or per app install, and per account, never
  shared and never sent to the gateway.
- On the web, dictation on the chat page is heard by the assistant unless the person mutes; nothing
  mutes it for them. The apps keep the two apart: a dictation and a call exclude each other.
- A chat page for a session that does not exist shows no header, and so no entry, though a call
  that is on goes on.
- `starved` is the gateway's estimate: the pacer assumes the browser plays in real time from the
  moment it receives a frame, and how long the voice counts as heard is only as good as the
  browser's own playout delay report (400 ms is assumed until it comes).
- The assistant reads the gateway's in-memory replay buffer, so right after a restart a pending
  decision's options may be unavailable until the device sends events again.
- One call per account, so a second tab or a second device is refused rather than taking over. With
  the default policy the day's call time is in memory and starts over with the gateway.
- A phone keeps an open call going in the background and Mute sends silence, so a call forgotten in
  a pocket stays open, and OpenAI bills it, silence included, until the idle hang-up
  (`LIVE_IDLE_TIMEOUT_SECONDS`, 90 s by default) ends it.
