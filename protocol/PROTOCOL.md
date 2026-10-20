# remote-control wire protocol v1

Normative specification for every component: the **gateway** (VPS service `rc_gateway`), a **device**
(a machine running the client daemon `rc-client`), and an **app** (the web UI, the iOS app, the
macOS app, the Android app or the Windows app).

This document is the reader-facing form of the frozen contract. It is paired with two things that
make it machine-checkable, and all three must agree:

| Artifact | Purpose |
| --- | --- |
| `schema/*.json` | JSON Schema draft 2020-12 for every object, event and frame |
| `fixtures/**/*.json` | A valid example of every frame type, event kind and HTTP body |
| `scripts/validate_fixtures.py` | Checks the fixtures against the schema and reports coverage |

"MUST", "MUST NOT" and "SHOULD" carry their usual meaning. Only a deliberate protocol change alters
this contract; a component that finds a gap reports it instead of deviating.

## Conventions

- All frames are UTF-8 JSON text messages on a WebSocket, one object per message. Binary frames
  appear only on the speech-to-text socket.
- Field names are `snake_case`. Enumerations are lowercase strings.
- Timestamps are Unix **milliseconds** as integers.
- Ids are strings, UUID v4 unless stated otherwise.
- **Unknown fields MUST be ignored, never rejected.** New optional fields may be added later; a
  party that validates strictly will break on the next revision. Every schema in `schema/` therefore
  leaves `additionalProperties` open.
- Protocol version is `1`. Every `hello` carries `protocol: 1`; a mismatch is a hard error with code
  `unsupported`.

The contract carries amendments, listed with their wording in section 11. Section 5 uses the
amended field names throughout.

---

## 1. Overview

### 1.1 Vocabulary

| Term | Meaning |
| --- | --- |
| gateway | The VPS service. Authenticates users and devices, routes frames, indexes sessions, buffers events for replay, proxies speech-to-text, dictation polish and push. It never runs an agent and never holds an agent's credentials; the only model keys it may hold are the operator's own for speech-to-text and for polishing dictation (A29). |
| device | A developer machine running `rc-client`. It drives the locally installed agents and is the source of truth for session history. |
| app | The web UI or the iOS app. Apps never talk to devices directly. |
| agent | `"claude"` (Claude Code), `"codex"` (Codex CLI), `"grok"` (Grok Build) or `"pi"` (the pi coding agent) (A25, A26). The field is an **extensible string**: a UI that meets an unknown agent renders it generically, using the id as the label. |
| session | One conversation with one agent on one device. `session_id` is device-local (for Claude it equals the Claude session id, for Codex the thread id). Global identity is the pair (`device_id`, `session_id`). |
| block | One renderable unit in a session timeline. |
| seq | Per-session, monotonically increasing integer assigned by the device to every session event. It survives device restarts and is the replay cursor. |
| turn | One user message plus the agent's work until it goes idle. |

### 1.2 Topology

```
web browser ──┐  wss /ws/app                       wss /ws/device  ┌── rc-client (device A) ── claude / codex
              ├────────────► gateway (rc_gateway) ◄────────────────┤
iOS app     ──┘              HTTP /api/*, /ws/stt                  └── rc-client (device B) ── claude / codex
```

Devices dial out only. The gateway parses just the envelope fields it needs for routing (`type`,
`id`, `device_id`, `session_id`, `seq`) plus the session summaries it indexes; every other payload is
forwarded opaquely.

---

## 2. Transport and envelope

### 2.1 Frame shape

Every frame is `{"type": "<domain.action>", ...fields}`. A request additionally carries `id`, chosen
by the requester and echoed in the reply:

```json
{"type": "reply", "id": "…", "ok": true,  "result": { }}
{"type": "reply", "id": "…", "ok": false, "error": {"code": "not_found", "message": "human readable"}}
```

### 2.2 Error codes

| Code | Used when |
| --- | --- |
| `bad_request` | The frame or body is malformed, or a value is out of range. |
| `unauthorized` | Missing or invalid credentials. |
| `forbidden` | Authenticated, but not allowed to touch this resource. |
| `not_found` | No such device, session, block, request or pairing code. |
| `device_offline` | The owning device has no live socket. Produced by the gateway. |
| `agent_unavailable` | The device has the session but the agent binary is missing or will not start. |
| `conflict` | The action contradicts current state, for example sending to a session the terminal controls. |
| `timeout` | No reply arrived in time. Produced by the gateway after 60 s. |
| `internal` | Unexpected failure. |
| `unsupported` | Protocol version mismatch, or a feature the deployment does not provide. |
| `too_large` | Payload over a documented bound. |

`error.message` is optional and is for humans; clients MUST branch on `error.code` only.

### 2.3 Routing of forwarded requests

The gateway forwards an app request to the owning device unchanged, except that it

1. adds `device_id` when the request is addressed by `session_id`, and
2. stamps `from`, an opaque app-connection id.

The device echoes `id` and `from` in its `reply`; the gateway delivers that reply only to the
originating connection and strips `from` before doing so. If the device is offline the gateway
answers `device_offline` itself. If no reply arrives within 60 s the gateway answers `timeout`.
`session.create` and `session.history` may use the full 60 s; every other request SHOULD reply in
under 5 s.

### 2.4 Idempotency

A device MUST treat a repeated `session.send` carrying the same `id` within the same session as a
duplicate: it returns the original result and does not send the message again. This is what makes an
app's retry of an unconfirmed send safe.

### 2.5 Liveness and close codes

The gateway sends `ping` every 25 s on both socket types and closes a connection silent for 90 s.
Any party that receives no frame at all for 60 s MUST treat the connection as half-open and
reconnect: mobile NAT never delivers `onclose`. These are the **only** keepalives on the link: the
gateway disables its WebSocket server's own protocol-level ping/pong timeout (amendment A13), so a
device whose event loop stalls for a few seconds is not cut off by a second, shorter clock.

A device link that drops is not offline yet. The gateway keeps reporting `online: true` for a
**grace period of 20 s** after the socket closes for a transient reason, and reports `online: false`
only when no replacement connection arrived in that time; a close with `4401` or `4403`, or a device
that was explicitly removed, turns it `false` at once. Requests addressed to the device during the
grace period wait for the replacement connection and are answered `device_offline` when the period
ends without one. A reconnect within the period is invisible to apps.

The close code says whether reconnecting is worth trying (amendment A4). It applies to `/ws/app` and
`/ws/device` alike.

| Code | Meaning | What the client does |
| --- | --- | --- |
| `4401` | The credential is missing, invalid, expired or revoked | **Stop reconnecting.** An app returns to login; a device re-enrolls. |
| `4403` | The credential is valid but not allowed for the requested resource | Do not retry the same resource. |
| `4001` | This device connection was replaced by a newer one | The device does not reconnect immediately. |
| `1008` | Protocol violation, and nothing else | Fix the client; retrying unchanged will fail again. |
| anything else | Transient | Reconnect with backoff. |

An app that retries through a `4401` will loop against a dead token, so treat the code as
authoritative and clear stored credentials before returning to login.

### 2.6 Size bounds

| Limit | Value |
| --- | --- |
| Session event frame | 64 KiB. The gateway drops larger frames and logs. |
| `tool_call.input` | 8 KiB of JSON, then `input_truncated: true` |
| `tool_call.output` | 16 KiB, then `output_truncated: true` |
| `diff.patch` | 32 KiB, then `patch_truncated: true` |
| `session.block` reply | 1 MiB |
| `session.send` text | 64 KiB |
| `session.send` attachments | 8 attachments, 6 MiB each decoded |
| `session.history` limit | 200 by default, 1000 maximum |
| Gateway replay buffer | 2 000 events or 4 MiB per session, whichever is smaller |
| Streaming delta flush | at most one flush per 80 ms per block |
| Speech-to-text utterance | 120 s of audio, 4 MiB total |

Truncated content is not lost: the full block is fetched with `session.block`.

---

## 3. HTTP API

Every person on a gateway has an account (A24). The account named `admin` is the operator's, and
its password is `RC_PASSWORD`; every other account is made by registering while the admin allows
it, or by the admin. An account sees only its own devices, the sessions on them, its own pairing
codes and its own push registrations: nothing in this section and nothing on `/ws/app` ever
returns another account's. Browsers authenticate with the HttpOnly cookie `rc_session`
(`SameSite=Strict`, `Secure` under https). Native apps send `Authorization: Bearer <token>` with the
same token value. WebSocket upgrades accept either.

**Origin rule.** Cookie-authenticated upgrades and cookie-authenticated mutating requests MUST carry
an `Origin` header equal to `PUBLIC_ORIGIN`. Bearer-authenticated requests need no `Origin`.

### 3.1 Unauthenticated

| Method | Path | Request | Response | Errors |
| --- | --- | --- | --- | --- |
| GET | `/api/health` | – | `HealthResponse` | – |

`HealthResponse`, `ConfigResponse` and `hello` all carry `apps` (amendment A31): the oldest build
of each separately installed app this gateway still works with — `apps.ios` for the iPhone app,
`apps.macos` for the Mac app (A45), `apps.android` for the Android app and `apps.windows` for the
Windows app (A46) — each a `minimum_version` as `major.minor.patch` with an optional `update_url`
naming where a newer build is (TestFlight, an app store or a download page).
An app reads its own entry and no other, and an entry that is absent states no requirement for
that app. It is here, on the one unauthenticated endpoint, so an app can refuse
to sign in before it has a credential; `hello` repeats it so a gateway upgraded under a connected
app is caught at the next connection. A gateway that omits `apps` states no requirement. The web
app is served by the gateway itself and never needs it.
| POST | `/api/login` | `LoginRequest` | `LoginResponse` + `Set-Cookie: rc_session` | `401 unauthorized` for a wrong password or an unknown account, `403 forbidden` for a disabled one. Rate limited to 5 per minute per IP. |
| POST | `/api/register` | `RegisterRequest` | `LoginResponse` + `Set-Cookie: rc_session` | Creates a `member` account and signs it in (A24). `403 forbidden` while registration is closed, `409 conflict` for a taken username, `400 bad_request` for a username or password outside the rules below. Rate limited to 5 per minute per IP. |
| POST | `/api/devices/enroll` | `EnrollRequest` | `EnrollResponse` | `404` unknown or expired code, `409` code already used |
| GET | `/install.sh` | – | The client install script with `__GATEWAY_ORIGIN__` replaced by `PUBLIC_ORIGIN` | – |
| GET | `/dist/rc_client-latest.whl` | – | The client wheel built into the image. The versioned filename also resolves. | – |
| POST | `/api/pairing/requests` | – | `PairingRequestResponse` | A host asks to be claimed by scanning (A23). `429 too_many_requests` beyond 6 per minute per IP or 50 outstanding tokens. |
| GET | `/api/pairing/requests/{token}` | – | `PairingRequestStatusResponse` | Long-poll up to 25 s. `waiting` until claimed; `claimed` carries the code once, then the token is spent. `404` unknown, `410` expired. |
| GET | `/` and any non-API path | – | The web app (single-page-app fallback) | – |

The pairing code is the only credential `POST /api/devices/enroll` needs. `device_token` is returned
once and stored hashed.

```json
{"ok": true, "version": "0.1.0", "protocol": 1, "auth": {"mode": "password", "registration_open": false}}
```

`auth.registration_open` says whether `POST /api/register` is taking accounts; an app offers
"Create an account" only when it is true. A username is 3 to 32 characters matching
`^[a-z0-9][a-z0-9._-]{2,31}$` — the gateway lower-cases what it receives before matching or
comparing, so `Alice` and `alice` are one account — and a password is 8 to 128 characters.
Passwords are stored hashed and never returned. `LoginRequest.username` is required.

`fixtures/http/login.request.json`

```json
{
  "password": "correct horse battery staple",
  "username": "admin"
}
```

`fixtures/http/login.response.json`

```json
{
  "ok": true,
  "token": "rc1.7f3c2a19d84b4e0f9a6c1b25e30d7a48.5c9e1f",
  "exp": 1791536400000,
  "user": {
    "username": "admin",
    "role": "admin"
  }
}
```

`fixtures/http/register.request.json`

```json
{
  "username": "alice",
  "password": "correct horse battery staple"
}
```

`fixtures/http/devices.enroll.request.json` (abridged)

```json
{
  "code": "RC-7K42-QX9M",
  "name": "mac-studio-office",
  "platform": "macos",
  "hostname": "mac-studio.local",
  "arch": "arm64",
  "client_version": "0.1.0"
}
```

`fixtures/http/devices.enroll.response.json`

```json
{
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "device_token": "rcd1.c5efb1ec29124619.9f4a2d7b1e6c08f35a2d",
  "gateway_ws_url": "wss://rc.example.com/ws/device"
}
```

### 3.2 Authenticated — account and configuration

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| POST | `/api/logout` | – | `OkResponse`, and the token is revoked |
| GET | `/api/session` | – | `AuthSessionResponse` |
| POST | `/api/password` | `PasswordChangeRequest` | `OkResponse`. Changes the caller's password; `401 unauthorized` when `current_password` is wrong, `400 bad_request` when the new one is outside the rules, `403 forbidden` for `admin`, whose password is `RC_PASSWORD` (A24). Other sign-ins of the account stay valid. |
| DELETE | `/api/account` | `{"current_password": string}` | `OkResponse`, and the session cookie is cleared. Deletes the caller's own account as `DELETE /api/users/{username}` does: the account is disabled first, then its devices, pairing codes, push registrations, preferences and login sessions go. `400 bad_request` for any other field, `401 unauthorized` when `current_password` is wrong, `403 forbidden` for `admin` and for a foreign `Origin`, `429 too_many_requests` under the login limit. |
| GET | `/api/config` | – | `ConfigResponse` |
| GET | `/api/preferences` | – | `PreferencesResponse` — the caller's account preferences (A35) |
| PATCH | `/api/preferences` | `PreferencesPatchRequest` | `PreferencesResponse`. Every field is optional and the ones present are set; the change goes out as `preferences.updated` to the account's app sockets and as `preferences` to its devices (A35) |

`fixtures/http/auth.session.response.json`

```json
{
  "ok": true,
  "user": {
    "username": "admin",
    "role": "admin"
  },
  "exp": 1791536400000
}
```

`user` is the `User` object of 4.10: the account the token belongs to and its `role`.

`client` names the wheel the gateway serves: its `version`, its `build` (the SHA-256 of the file)
and its `url`; a device whose `client_build` differs is brought to it with `device.update` — by the
gateway itself, as soon as it can (A36), or by an app retrying after a failure (A22).

`fixtures/http/config.response.json`

```json
{
  "public_origin": "https://rc.example.com",
  "stt": {
    "enabled": true,
    "languages": [
      "auto"
    ]
  },
  "polish": {
    "enabled": true
  },
  "apps": {
    "ios": {
      "minimum_version": "0.1.0",
      "update_url": "https://testflight.apple.com/join/EXAMPLE"
    },
    "macos": {
      "minimum_version": "1.11.0"
    },
    "android": {
      "minimum_version": "1.12.0"
    },
    "windows": {
      "minimum_version": "1.12.0"
    }
  },
  "push": {
    "web_enabled": true,
    "apns_enabled": false
  },
  "version": "0.1.0"
}
```

An account's preferences (amendment A35) are the settings that must read the same on the phone,
in the browser and on every device of the account, so they live on the gateway and not in an app.
The first was `resume_after_limit`: whether a session that stopped because the vendor's usage limit
was reached is resumed by its device once the limit resets (7.2); off until the person turns it on.
Since A41 the Settings screen's own preferences are there too: `language` (the app's interface
language, `en` or `zh-Hans`), `stt_language` (the language a phone that recognises speech itself
listens for — `zh`, `en`, `ja`, … — Chinese when unset; an `auto` written before A44 reads as
unset, because the gateway's provider is the only recogniser that detects the language and it
needs no setting, A44), `polish_enabled`, `polish_model`, `polish_strength` (`moderate` or `strong`,
A29) and `timeline_detail` (`simple` or `detailed`). Every field but `resume_after_limit` is
optional: absent means nobody has set it yet, and an app then keeps the value it has and writes it
up the first time it connects, so an account that arrives at A41 keeps what its first app had.
The gateway is the one writer and the order of arrival is the order of truth: a `PATCH` sets the
fields present, and the whole object goes out at once as `preferences.updated` to every app socket
of the account and as `preferences` to its devices — the latest write to reach the gateway wins,
on every screen, within the round trip. Apps take `hello.preferences` and every
`preferences.updated` as the truth and keep a local copy only for the moments before `hello` and
for a gateway older than the field. What stays on the device is what belongs to the device:
notifications (a permission of that device), the app lock, the transcription backend, the terminal
font size and which groups a list has folded. `hello` on `/ws/app` carries the object as
`preferences`; a gateway older than A35 sends none, and an app shows the resume switch disabled
with a note. Only the caller's own preferences are readable or writable.

`fixtures/http/preferences.response.json`

```json
{
  "preferences": {
    "resume_after_limit": true,
    "language": "zh-Hans",
    "stt_language": "zh",
    "polish_enabled": true,
    "polish_model": "gpt-5.4-mini",
    "polish_strength": "moderate",
    "timeline_detail": "detailed"
  }
}
```

### 3.3 Authenticated — devices and pairing

| Method | Path | Request | Response | Notes |
| --- | --- | --- | --- | --- |
| GET | `/api/devices` | – | `DeviceListResponse` | |
| PATCH | `/api/devices/{device_id}` | `DevicePatchRequest` | `DeviceResponse` | Renames the device. The name holds until the next rename; the device's `hello` never changes it (A50) |
| DELETE | `/api/devices/{device_id}` | – | `OkResponse` | Revokes the device token, closes its socket, drops its sessions from the gateway index |
| POST | `/api/devices/pairing` | – | `PairingResponse` | Code format `RC-XXXX-XXXX`, Crockford base32 without I, L, O and U. Single use, 10-minute lifetime. |
| DELETE | `/api/devices/pairing/{code}` | – | `OkResponse` | Cancels an outstanding code |
| POST | `/api/pairing/requests/{token}/claim` | – | `PairingClaimResponse` | Binds a host's request to the caller and mints its pairing code (A23); `pairing.progress` follows for that code. `404` unknown or expired, `409` already claimed. |

`fixtures/http/devices.patch.request.json`

```json
{
  "name": "studio"
}
```

`fixtures/http/devices.pairing.response.json`

```json
{
  "code": "RC-7K42-QX9M",
  "expires_at": 1788945000000,
  "install": {
    "macos": "curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M",
    "linux": "curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M"
  }
}
```

### 3.4 Authenticated — sessions

| Method | Path | Query | Response |
| --- | --- | --- | --- |
| GET | `/api/sessions` | `device_id`, `archived` | `SessionListResponse` |

This reads the gateway index, so it renders the last known summaries even while a device is offline.

### 3.5 Authenticated — speech to text and dictation polish

| Method | Path | Request | Response | Errors |
| --- | --- | --- | --- | --- |
| POST | `/api/stt/transcribe` | multipart form: `audio` (`wav`, `webm`, `m4a`, `mp3`) | `SttTranscribeResponse` | `503` with code `unsupported` when speech to text is not configured |

The gateway's provider detects the language of what it transcribes; nobody chooses it (A44). A
`language` field an older client sends is ignored. `language` in the response is the language the
provider reported, or `auto` when it reports none. `stt.languages` in `hello` and
`GET /api/config` is therefore always `["auto"]`, and stays in both for the apps that read it.

`fixtures/http/stt.transcribe.response.json`

```json
{
  "text": "Fix the flaky refresh test in tests slash test underscore auth dot py.",
  "language": "en"
}
```

| Method | Path | Request | Response | Errors |
| --- | --- | --- | --- | --- |
| GET | `/api/polish/models` | — | `PolishModelsResponse` | `503` with code `unsupported` when no polish model is configured; `502` with code `upstream` when the provider fails |
| POST | `/api/polish` | `PolishRequest` | `PolishResponse` | `503` `unsupported`; `400` `bad_request` for an empty or over-long text, an unknown strength or a malformed context; `502` `upstream` when the provider fails or times out |

Dictation polish (amendment A29) is the one place the gateway calls a language model, and it does so
only on an app's request: the operator configures an OpenAI-compatible base URL and key for it, the
gateway lists that provider's models, and an app whose user turned the feature on sends the text a
dictation produced together with the recent conversation and gets the same request back said
cleanly. `hello` and `GET /api/config` report `polish.enabled` so an app can show or disable the
setting. The gateway stores nothing from either call and never forwards the polished text to a
device: the result is a draft in the app's composer, and sending it is the user's own action.

`PolishRequest.strength` is `moderate` or `strong`. Moderate removes fillers, false starts and
repetitions, corrects what the recogniser plainly misheard, punctuates, and otherwise keeps the
speaker's words and order. Strong also restructures for clarity and precision and resolves vague
references from the conversation, while adding no request the speaker did not make. Both keep the
language the text was spoken in and return text only. `context` carries at most twenty of the
session's most recent user and assistant messages as the app already shows them, oldest first, each
trimmed by the app; the gateway passes them to the model as conversation and nothing else.
`language` is a hint for the model: `auto` for words the gateway transcribed, the language the
phone's own recogniser listened for when it transcribed them (A44).

`fixtures/http/polish.models.response.json`

```json
{
  "models": [
    {
      "id": "gpt-4.1-mini",
      "label": "gpt-4.1-mini"
    },
    {
      "id": "gpt-4.1",
      "label": "gpt-4.1"
    }
  ]
}
```

`fixtures/http/polish.request.json`

```json
{
  "text": "um so the the green dot the one that blinks it should stop blinking when when it's done and just stay green",
  "model": "gpt-4.1-mini",
  "strength": "strong",
  "language": "en",
  "context": [
    {
      "role": "user",
      "text": "Make the session status dot pulse while a turn is running."
    },
    {
      "role": "assistant",
      "text": "Done: the dot now breathes while the state is running and stays solid green when idle."
    }
  ]
}
```

`fixtures/http/polish.response.json`

```json
{
  "text": "The pulsing status dot should stop pulsing when the turn finishes and stay solid green."
}
```

### 3.6 Authenticated — push registration

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/api/push/web/vapid` | – | `VapidResponse` |
| POST | `/api/push/web/subscribe` | `WebPushSubscribeRequest` | `OkResponse` |
| DELETE | `/api/push/web/subscribe` | `WebPushUnsubscribeRequest` | `OkResponse` |
| POST | `/api/push/apns/register` | `ApnsRegisterRequest` | `OkResponse` |
| DELETE | `/api/push/apns/register` | `ApnsUnregisterRequest` | `OkResponse` |

`fixtures/http/push.apns.register.request.json`

```json
{
  "token": "a8f3c91d4b7e250689fc13ad5e207b46c8d90a1f3e7b5c249d06f8a1b3c5d7e9",
  "environment": "production",
  "bundle_id": "com.example.sidecode"
}
```

### 3.7 Push payload

Both the Web Push JSON body and the APNs custom data carry the same object. The visible notification
text is generic, for example "mac-studio-office: approval needed". **No prompt text, message text or
tool output ever appears in a push.**

`fixtures/http/push.payload.json`

```json
{
  "rc": {
    "v": 1,
    "kind": "needs_approval",
    "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
    "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
    "device_name": "mac-studio-office",
    "title": "mac-studio-office: approval needed"
  }
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `rc.v` | `1` | yes | Payload version |
| `rc.kind` | `needs_approval` \| `needs_input` \| `turn_completed` \| `error` \| `limit_reached` \| `resumed` \| `resume_dropped` \| `badge` | yes | Why the user is being notified. `limit_reached`, `resumed` and `resume_dropped` (A35) follow the `resume` events of 5.15; `badge` (A47) changes the count and shows nothing |
| `rc.device_id` | uuid | yes | Deep-link target |
| `rc.session_id` | string | yes | Deep-link target |
| `rc.device_name` | string | yes | Shown in the notification text |
| `rc.title` | string | yes | The generic notification text; empty for `badge` |
| `rc.badge` | integer ≥ 0 | no | How many of the account's unarchived sessions carry `unseen` (4.4) once this push's own change is made — the app icon's badge (A47). APNs payloads set `aps.badge` to it |

Three kinds follow what a device does about a session the usage limit stopped (amendment A35, 7.2):
`limit_reached` when it schedules the resume (`resume {status: "scheduled"}`), `resumed` when the
resume starts a turn (`fired`), and `resume_dropped` when it could not resume (`dropped`). The
title stays generic — "mac-studio-office: paused by the usage limit", "mac-studio-office: resumed
after the limit reset", "mac-studio-office: not resumed" — and the time is read in the app, which
has the session's `resume`.

`fixtures/http/push.payload.limit.json`

```json
{
  "rc": {
    "v": 1,
    "kind": "limit_reached",
    "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
    "session_id": "6d1f3c58-8b2e-4d67-9a4f-2e7c1b0d5a93",
    "device_name": "mac-studio-office",
    "title": "mac-studio-office: paused by the usage limit"
  }
}
```

**The count travels with the push (A47).** Every push carries `rc.badge`, and an APNs push sets
`aps.badge` from it, so the iPhone's home-screen badge holds while the app is closed. When the
account's count changes without a push — a mark cleared by `session.seen` or by the session working
again, or set while an app watches the session and the push is suppressed — the gateway sends each
of the account's APNs registrations a badge-only notification once the count has been still for
3 seconds: `aps.badge` and `rc` with `kind: "badge"`, the session whose mark changed and an empty
`title`, no alert and no sound. Web Push gets none: a browser shows every push it receives.

`fixtures/http/push.payload.badge.json`

```json
{
  "rc": {
    "v": 1,
    "kind": "badge",
    "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
    "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
    "device_name": "mac-studio-office",
    "title": "",
    "badge": 0
  }
}
```

### 3.8 Speech-to-text streaming socket

`WS /ws/stt`, authenticated as above. The provider detects the language (A44); a `language`
query parameter an older app sends is ignored, and `stt.final.language` is the language the
provider reported, or `auto`.

The client sends **binary** frames of PCM16LE, 16 kHz, mono, 100–200 ms per frame. It sends
`stt.stop` to finish and transcribe everything received, or `stt.cancel` to discard the utterance.
The gateway sends `stt.partial` about every 2 s while audio is arriving, then `stt.final` after
`stt.stop`, or `stt.error` on failure. The gateway closes the socket after `stt.final` or
`stt.error`.

`fixtures/stt/stt.stop.json`

```json
{
  "type": "stt.stop"
}
```

`fixtures/stt/stt.cancel.json`

```json
{
  "type": "stt.cancel"
}
```

`fixtures/stt/stt.partial.json`

```json
{
  "type": "stt.partial",
  "text": "fix the flaky refresh"
}
```

`fixtures/stt/stt.final.json`

```json
{
  "type": "stt.final",
  "text": "Fix the flaky refresh test in tests/test_auth.py.",
  "language": "en"
}
```

`fixtures/stt/stt.error.json`

```json
{
  "type": "stt.error",
  "message": "speech to text backend returned 502"
}
```

---

### 3.9 Authenticated — accounts (admin only)

Every route here answers `403 forbidden` to a caller whose `role` is not `admin` (A24).

| Method | Path | Request | Response | Notes |
| --- | --- | --- | --- | --- |
| GET | `/api/users` | – | `UserListResponse` | Every account as a `UserRecord`, oldest first, and whether registration is open. |
| POST | `/api/users` | `UserCreateRequest` | `UserResponse` | `409 conflict` for a taken username, `400 bad_request` outside the rules of 3.1. |
| PATCH | `/api/users/{username}` | `UserPatchRequest` | `UserResponse` | Any of `state`, `role`, `password`. Disabling revokes every login session of the account and closes its device sockets with 4403; enabling lets them back. `admin` cannot be disabled, demoted, given a password or deleted: `409 conflict`. `404 not_found` for an unknown account. |
| DELETE | `/api/users/{username}` | – | `OkResponse` | Removes the account, its login sessions, push registrations, pairing codes and devices — each device exactly as `DELETE /api/devices/{device_id}` would, sessions included. |
| PATCH | `/api/registration` | `RegistrationPatchRequest` | `RegistrationResponse` | Opens or closes `POST /api/register`. A fresh gateway starts closed. |

`fixtures/http/users.list.response.json`

```json
{
  "users": [
    {
      "username": "admin",
      "role": "admin",
      "state": "active",
      "created_at": 1788426000000,
      "last_login_at": 1788944400000,
      "devices": 2
    },
    {
      "username": "alice",
      "role": "member",
      "state": "disabled",
      "created_at": 1788512400000,
      "last_login_at": null,
      "devices": 0
    }
  ],
  "registration_open": false
}
```

`fixtures/http/users.create.request.json`, `fixtures/http/users.patch.request.json`,
`fixtures/http/users.response.json`, `fixtures/http/registration.patch.request.json` and
`fixtures/http/registration.response.json` carry the other bodies.

---

## 4. Shared objects

Schema: `schema/objects.json`. These objects appear in HTTP bodies and in frames on both sockets.

### 4.1 Device

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `device_id` | uuid | yes | Assigned by the gateway at enrollment |
| `name` | string | yes | User-editable display name: the `name` the device enrolled with, changed only by `PATCH /api/devices/{device_id}` (A50) |
| `platform` | `macos` \| `linux` | yes | |
| `hostname` | string | yes | Reported by the device |
| `arch` | `arm64` \| `x86_64` | yes | |
| `client_version` | string | yes | `rc-client` version |
| `client_build` | string \| null | no | SHA-256 of the wheel the client was installed from; null when unknown (A22) |
| `update_state` | `idle` \| `updating` \| `failed` | no | An update the gateway (A36) or an app (A22) asked for, in flight or failed; absent means idle |
| `update_message` | string \| null | no | Why the last update failed (A22) |
| `terminal` | boolean | no | True when the device offers a shell (7.3, A38); absent on a client older than A38 |
| `online` | boolean | yes | True while the device socket is live |
| `last_seen` | timestamp | yes | |
| `created_at` | timestamp | yes | Enrollment time |
| `latency_ms` | integer \| null | yes | Last ping round trip, null when unknown |
| `agents` | `AgentInfo[]` | yes | Agents detected on the device |

`fixtures/app/device.updated.json` (abridged)

```json
{
  "type": "device.updated",
  "device": {
    "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
    "name": "mac-studio-office",
    "platform": "macos",
    "hostname": "mac-studio.local",
    "arch": "arm64",
    "client_version": "0.1.0",
    "online": true,
    "last_seen": 1788944400000,
    "created_at": 1788426000000,
    "latency_ms": 18,
    "agents": [
      {
        "agent": "claude",
        "available": true,
        "version": "2.1.266",
        "path": "/Users/me/.local/bin/claude",
        "models": [
          {
            "id": "claude-opus-4-6",
            "label": "Opus 4.6"
          }
        ],
        "default_model": "claude-sonnet-4-5",
        "permission_modes": [
          {
            "id": "default",
            "label": "Ask before edits"
          }
        ],
        "default_permission_mode": "default",
        "efforts": [
          {
            "id": "low",
            "label": "Low"
          }
        ],
        "default_effort": "medium",
        "capabilities": [
          "worktree",
          "takeover",
          "interrupt",
          "queue",
          "attachments",
          "effort",
          "history"
        ]
      }
    ]
  }
}
```

The agent and option arrays are shortened here; the fixture holds the full object.

### 4.2 AgentInfo

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `agent` | string | yes | `claude`, `codex`, `grok`, `pi`, or a future id. Never assume a closed set. |
| `available` | boolean | yes | False when the binary is missing or will not start |
| `version` | string \| null | yes | |
| `path` | string \| null | yes | Resolved binary path |
| `models` | `LabeledId[]` | yes | Native model ids with human labels |
| `default_model` | string \| null | yes | |
| `permission_modes` | `LabeledId[]` | yes | See 4.3 for the ids each agent exposes. Empty for an agent with no permission system (A25): an app then draws no permission picker and `session.set` refuses `permission_mode` with `unsupported`. No shipped agent is empty today: pi's modes are the device's own, enforced by the extension it installs (A26) |
| `default_permission_mode` | string \| null | yes | |
| `efforts` | `LabeledId[]` | yes | Empty array when the agent has no effort levels |
| `default_effort` | string \| null | yes | |
| `speeds` | `LabeledId[]` | no | Speed tiers the agent can run a session at beyond its standard speed, for example Codex's `priority` ("Fast"); empty or absent when it has none (amendment A21) |
| `capabilities` | string[] | yes | Subset of `worktree`, `takeover`, `interrupt`, `queue`, `steer`, `attachments`, `effort`, `history`, `commands`. `commands` means the agent's sessions can list and run slash commands from an app through `session.commands` and `session.command` (A27); Codex, Grok Build and pi carry it, Claude does not |
| `attach` | `channel` \| `daemon` \| `extension` \| `leader` \| null | no | How this agent's terminal sessions can be attached. `channel` is the Claude channel shim, `daemon` the Codex shared app-server, `extension` an extension of the device's own that the agent loads into every one of its processes (pi, A26), `leader` Grok Build's leader process — one shared backend per machine that its TUI joins when `[cli] use_leader` is on and that the device joins as another ACP client (A28). Null or absent means terminal sessions can only be taken over or resumed. |
| `attach_ready` | boolean | no | Whether the device is prepared to attach: for Claude the `claude` shim is installed and on `PATH`, for Codex a handshake on the shared daemon socket succeeds, for pi the device's extension is installed in pi's global extension directory at the current build, for Grok Build the person's own `~/.grok/config.toml` turns `[cli] use_leader` on and requests no sandbox profile, so the next `grok` started on that machine joins the leader (A28). Apps use it only to word the hint on a `terminal` session. |
| `shared_interrupt` | boolean | no | Whether the attachment can interrupt a running turn. `session.stop` on a `shared` session needs this **and** capability `interrupt`. False when absent. |
| `shared_settings` | boolean | no | Whether `session.set` for `model`, `permission_mode`, `effort` and `speed` works on a `shared` session. False when absent. |
| `shared_settings_keys` | string[] | no | With `shared_settings` true, the subset of `model`, `permission_mode`, `effort` and `speed` that `session.set` changes on a `shared` session; the others stay what the terminal set and an app draws them as values (A17). Absent means all four. Never present when `shared_settings` is false or absent (A40). |
| `shared_attachments` | boolean | no | Whether `session.send` attachments are delivered on a `shared` session. False when absent. |
| `shared_steer` | boolean | no | Whether `session.send` with `mode: "auto"` steers a running turn on a `shared` session: the device answers `accepted: "steered"` and the message reaches the turn at its next step — for Claude its next batch of tool calls, or right after the turn when it has none left. Sent only when true; false when absent (A51). |
| `accounts` | `AgentAccount[]` | no | How the agent is signed in on this device: one entry per vendor credential it holds, with the vendor's plan and, in a `device.agents` reply, the rate-limit windows the device could read (A33). Absent when the device did not look; empty when the agent is installed but signed in nowhere. |

**Accounts and quota (A33).** An agent runs on somebody's credentials: the vendor's own account —
Anthropic's Pro, Max, Team or Enterprise, OpenAI's Plus, Pro or Business, an xAI account — or an
API key, possibly pointed at a third-party endpoint. `accounts` says which, one `AgentAccount` per
credential the agent holds on the device, read from the agent's own files and never from a running
session. pi holds one per provider it is signed in to; the other three hold at most one.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `provider` | string | yes | The vendor the credential belongs to, as the agent names it: `anthropic`, `openai`, `xai`, or another id. Apps show a known name and otherwise the id |
| `method` | `account` \| `api_key` | yes | `account` is the vendor's own subscription account, signed in with OAuth; `api_key` is a key |
| `plan` | string \| null | no | The plan word the vendor records, lowercase as reported: `pro`, `max`, `team`, `enterprise`, `plus`, `business`, `free`… Null or absent when the agent records none |
| `tier` | string | no | A finer tier when the vendor exposes one, in words the device vouches for: `Max 5x` from Claude's rate-limit tier id `default_claude_max_5x`. Omitted when it only repeats `plan` |
| `email` | string | no | The account's email when the agent records it locally |
| `endpoint` | string | no | For `api_key`: the host the key is sent to when it is not the vendor's own (`ANTHROPIC_BASE_URL`, a Codex `model_providers` entry). Host only, never a path or a secret |
| `limits` | `AgentLimit[]` | no | The rate-limit windows the device read for an `account`. Present only in a `device.agents` reply; empty when the vendor reported none. Absent, with no `limits_error`, when the device has no way to read them: a pi sign-in other than Anthropic's, or Grok Build with leader mode off (A53) |
| `limits_error` | string | no | Why `limits` is missing after the device tried — the token expired, the shared daemon is down, the network failed — in the device's words, one line |
| `limits_checked_at` | timestamp | no | When the device read `limits` |

An `AgentLimit` is one window:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `window_minutes` | integer ≥ 1 | yes | The window's length: 300 for a five-hour window, 10080 for a week |
| `scope` | string | no | What the window is confined to when it is not everything: the model a weekly limit applies to, in the vendor's words |
| `used_percent` | number 0–100 | yes | How much of the window is used |
| `resets_at` | timestamp \| null | no | When the window resets |

`hello` and `agents.updated` carry `accounts` without `limits`, `limits_error` or
`limits_checked_at`: they come from local files and change rarely, so a device that re-detects its
agents every quarter hour republishes nothing new. Limits are read on request: the reply to
`device.agents` (6) carries them fresh — one network or daemon call per account, under a short
timeout — and the device keeps that reply out of what it republishes. An app that opens a device
shows the stored accounts at once and asks `device.agents` for the meters; a device that is offline
has accounts and no meters. `fixtures/objects/agent.claude-attach.json` (an Anthropic account with a
tier and two windows, one confined to a model), `agent.codex-daemon.json` (an OpenAI account on plan
`pro` with the shared daemon's two windows), `agent.grok.json` (an xAI account with no plan, on a
device with Grok's leader mode off and so with no windows) and `agent.pi.json` (an Anthropic
account beside an OpenAI key) are the worked examples, each as a `device.agents` reply would carry
it.

Capabilities gate the UI. `steer` decides whether a message sent during a running turn is steered or
queued; `takeover` decides whether a terminal-controlled session offers "Take over"; `history`
decides whether the app can page backwards. On a `shared` session `shared_steer` decides steering
as well: an app treats a running session as steering when the agent's `capabilities` has `steer`,
or when the session is `shared` and the agent reports `shared_steer: true`, and as queueing
otherwise (A51).

`capabilities` is unchanged by the attachment fields. `takeover` keeps its meaning and applies to
`control: "terminal"` only; `attach`, `attach_ready`, `shared_interrupt`, `shared_settings` and
`shared_attachments` describe `control: "shared"` instead (4.4).

The attachment fields are the whole story an app needs: nothing in this protocol is specific to
one agent's attachment mechanism. Claude reports `attach: "channel"` with `shared_attachments`
true whenever `attach_ready` is (A49): a channel carries text and no bytes, so the device writes each
file to disk and the words it injects name it, as 4.8 describes. It reports `shared_settings` true
with `shared_settings_keys: ["model", "effort"]` (A40): the device's shim runs the CLI inside a
pseudo-terminal the device owns
and types the change into it as the person would — the terminal shows `/model` or `/effort` and
the answer — so the model and the effort are the device's to change while the permission mode stays
the terminal's; the same typing runs `/compact`, so Claude carries the `commands` capability (A27);
and since A42 `shared_interrupt` is true for the same reason: `session.stop` types Escape into that
terminal, which is how the person stops a turn there, only while a turn is running and no dialog is
open — an approval or a question on screen is answered, not escaped, and the request answers
`conflict` until it is. Since A51 it reports `shared_steer` true whenever `attach_ready` is and its
Claude Code runs the `PostToolBatch` hook (2.1.291 and later): the settings file the shim passes
carries the turn hooks that hand a steered message to the model at the turn's next batch of tool
calls. Claude lists no `steer` capability, because a session the SDK drives cannot steer. Codex reports `attach: "daemon"`, `attach_ready` true once a
WebSocket handshake on the shared daemon socket succeeds rather than merely because the socket file
is there, and `shared_interrupt`, `shared_settings` and `shared_attachments` all true, because the
shared app-server accepts interrupts, settings updates and image inputs from every attached client.
pi reports `attach: "extension"` and the same three flags true, because the extension runs inside
the pi process and can abort its turn, change its model and thinking level, and hand it images
(A26). Grok Build reports `attach: "leader"`, `attach_ready` from the person's configuration rather
than from a handshake — the leader is started on demand by whichever client comes first, the TUI or
the device, so there is nothing to handshake with before then — with `shared_interrupt` and
`shared_settings` true, because `session/cancel` and `session/set_config_option` from any client of
the leader act on the session everyone is in (A28), and `shared_attachments` true with capability
`attachments`, because its prompts, which take no images, name files the device wrote (4.8, A49).
`fixtures/objects/agent.claude-attach.json`,
`fixtures/objects/agent.codex-daemon.json`, `fixtures/objects/agent.pi.json` and
`fixtures/objects/agent.grok.json` are the four worked examples.

### 4.3 Permission-mode ids exposed by the device

| Agent | Ids and labels |
| --- | --- |
| Claude | `default` "Ask before edits", `acceptEdits` "Auto-accept edits", `plan` "Plan mode", `bypassPermissions` "Bypass permissions" |
| Codex | `untrusted` "Ask for everything", `on-request` "Ask when needed", `never` "Never ask" |
| Grok Build | `default` "Ask when needed", `acceptEdits` "Auto-accept edits", `auto` "Auto mode", `dontAsk` "Deny unless allowed", `plan` "Plan mode", `bypassPermissions` "Bypass permissions" (A25) |
| pi | `untrusted` "Ask for everything", `on-request` "Ask when needed", `never` "Never ask". pi itself has no permission system; these are the device's, enforced by the extension it loads into every pi session it drives or is attached to. `on-request` asks before `bash`, `edit`, `write` and every tool that is not one of pi's built-in readers (`read`, `grep`, `find`, `ls`); `untrusted` asks before every tool; `never` is pi's own behaviour (A26) |

Model ids are the agents' native ids. Effort ids are too: Claude's and Codex's reasoning
efforts, Grok's `reasoning_effort` levels as its model catalogue lists them per model, and pi's
thinking levels (`off` … `max`). `fixtures/objects/agent.grok.json` and `agent.pi.json` are the two
worked examples of A25 and A26.

### 4.4 Session

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `session_id` | string | yes | Device-local. Global identity is (`device_id`, `session_id`). |
| `device_id` | uuid | yes | |
| `agent` | string | yes | |
| `title` | string | yes | Device-supplied summary of the conversation |
| `cwd` | string | yes | Working directory on the device |
| `git` | `Git` \| null | yes | Null when `cwd` is not a repository |
| `state` | see 4.5 | yes | |
| `state_detail` | string \| null | yes | One line of context for the current state |
| `origin` | `remote` \| `terminal` | yes | Who created the session |
| `control` | `remote` \| `terminal` \| `shared` \| `none` | yes | Who owns the input right now. See the table below. |
| `model` | string \| null | yes | |
| `permission_mode` | string \| null | yes | |
| `effort` | string \| null | yes | |
| `speed` | string \| null | no | The tier from `AgentInfo.speeds` the session runs at; null or absent is the standard speed (A21) |
| `created_at` | timestamp | yes | |
| `updated_at` | timestamp | yes | |
| `last_seq` | integer | yes | Highest `seq` the device has produced |
| `archived` | boolean | yes | |
| `turn` | `{turn_id, started_at}` \| null | yes | Non-null while a turn is in progress |
| `todos` | `{total, done}` \| null | yes | Counts for the header chip |
| `usage` | `Usage` \| null | yes | |
| `queued` | integer | yes | Number of queued remote messages |
| `resume` | `SessionResume` \| null | no | The resume the device has scheduled for this session after a usage limit (7.2), or null or absent when there is none (A35) |
| `unseen` | boolean | no | True while the session waits for the person and nobody on the account has opened it since it stopped working (A47, below); the gateway's alone — a device never sends it — and absent is false |

#### `unseen` (A47)

A session **works** while its `state` is `running` — a turn is under way — and **waits for the
person** while its `state` is `needs_approval` or `needs_input`, or `idle` or `readonly` with a
`control` other than `none`: the status dot's amber. (`starting` is green too, but a session that
only started has done nothing to look at, so it never brings the mark.) The gateway sets `unseen`
on the account's session when the device moves it from working to waiting — a turn ended, or it
asks for an approval or an answer — and clears it when an app of the account sends `session.seen` (6.2), when the session works
again (someone carried on, from a terminal or another app), or when it is archived or removed. The
mark survives a gateway restart; every change reaches the account's apps as `session.updated`. The
transition is read from the states alone: a turn that ended while the device was offline still
marks the session when the device reports it.

#### `control` values

| Value | Meaning |
| --- | --- |
| `remote` | The device daemon runs the agent process for this session: one created with origin `remote`, or a terminal session that was resumed or taken over. |
| `terminal` | A live CLI process owns the session and the device has **no** way in. The composer is disabled; "Take over" is offered only when the agent has capability `takeover`. |
| `shared` | A live CLI process owns the session **and the device is attached to it**: for Claude through a channel the CLI loads, for Codex through the shared app-server. Apps enable the composer and approvals exactly as for `remote`, hide "Take over", and hide Stop unless the agent lists capability `interrupt` **and** reports `shared_interrupt: true` (4.2). Codex can through the daemon; Claude can since A42 through the pseudo-terminal its shim owns (Escape). |
| `none` | No process owns the session; the next `session.send` resumes it under device control. |

Control moves `terminal → shared` when the attachment registers, `shared → terminal` when the
attachment drops while the CLI process is still alive, and `shared → none` when the CLI exits. Each
transition travels the way every other `control` change does: a `meta` event carrying `control`
(5.11), a `status` event when the state changes with it (5.10), and a republished session summary.

An attachment follows the CLI process, not the id the process was started with (amendment A16). A
Claude CLI that resumes another session inside the TUI, or clears to a fresh one, keeps its channel;
the device learns the new id from the CLI's own `SessionStart` hook and moves the attachment there:
the session the terminal is now in becomes `shared`, and the one it left becomes `none` by the
transition above. A session that never held a message and has no transcript on disk — the id a CLI
was started with and then left by `/resume` or by quitting — is not kept: the device removes it with
`session.removed` (7) as soon as the terminal leaves it, and on its next scan for any it missed.
`session.removed` is therefore not only the answer to a `session.delete`; an app drops the row
whenever the frame arrives.

#### What a terminal-held session reports about itself

For a Claude session it mirrors from a transcript, the device fills `model`, `permission_mode` and
`effort` from the transcript's own records — the model row Claude Code writes at start and on every
change, the permission-mode row it writes each turn, and the effort carried on each assistant
message — and publishes every change as `meta` (5.11), so an app sees a `/model` typed in the
terminal within a scan (amendment A17); a Codex thread reports its `speed` from the daemon's own
settings as soon as the device attaches to it (A21). The
values are the agent's own ids and need not appear in
`AgentInfo`; an app shows an unknown one by its id. Changing them is still `session.set`, which a
`shared` session refuses with `unsupported` unless `shared_settings` is true (4.2) and a
`terminal` session refuses outright.

#### `origin` and `control` for Codex threads on the shared daemon

Codex runs every bare `codex` TUI inside one local app-server daemon, and the device attaches to
that daemon as a second client. The device derives `origin` and `control` for a Codex thread like
this.

| Situation | `origin` | `control` |
| --- | --- | --- |
| The device created the thread and no message typed in a terminal has been seen on it | `remote` | `remote` |
| The thread was already loaded in the daemon when the device found it, or any message on it was typed in a terminal | `terminal` when the device did not create the thread, otherwise unchanged | `shared` |
| The thread is known from the daemon's history but is not loaded | unchanged | `none`, and the next `session.send` resumes it |
| The rollout is held by a Codex process that is not the daemon, which is what a TUI started with configuration overrides does | `terminal` | `terminal`, because there is nothing to attach to |

The daemon reports nothing at all when a TUI exits and offers no way to ask who is attached, so the
device takes the TUI process itself as the signal: a Codex thread stays `shared` while a live bare
`codex` process is running in that thread's `cwd`, which the device checks on its ten second scan,
and a message typed in a terminal since the last scan always counts as a terminal being there. Once
the terminal is gone the thread keeps its `origin` and becomes `remote` while a turn the device
started is still running, `none` otherwise; `none` is resumable, so the next `session.send` drives
the same loaded thread. A later message typed in a terminal makes it `shared` again, and a process
scan that cannot be completed changes nothing. Apps need no Codex-specific logic here; they read
`control` and the agent's attachment fields (4.2) and nothing else.

Not every thread the daemon's history lists is a session. Codex keeps one history for the whole
machine, and the desktop app's chats and scheduled automations, an IDE extension's threads and the
subagents a thread spawned all land in it beside the terminal's. The device publishes a Codex
thread only when it is the device's to show (amendments A18 and A48): its `source` is a plain string — a
subagent's `source` is an object naming its parent, and a subagent is never a session of its own —
and either the thread's `originator` is the name the device itself connects to Codex under, its
`source` is `cli` or `exec`, or its `originator` is `codex-tui` and its `source` is `vscode`.
The last combination is a terminal TUI using an app-server, as observed in Codex 0.160.0;
`vscode` alone does not identify a terminal, and any other application's `vscode` threads stay
foreign. Any other thread belongs to the application, or the parent thread, that started it:
the device never publishes it, never mirrors its rollout, and sends `session.removed` for any
it published before this rule, repeating the frame on the next link as A16 does. The apps need
nothing for this; a session they never receive is a row they never draw.

#### `origin` and `control` for Grok Build sessions on the leader

Grok Build can run every `grok` on a machine inside one **leader** process, which the TUI joins
when the person's `~/.grok/config.toml` has `[cli] use_leader = true`, and the device joins the same
leader as another client of Grok's Agent Client Protocol (amendment A28). A `session/load` on a
session a TUI has open joins that session rather than opening a second copy; every client then sees
every update, a prompt from any client runs in the one conversation and is drawn by the TUI, and
`session/cancel` and `session/set_config_option` from any client act on it for all. The device
derives `origin` and `control` for a Grok session like this.

| Situation | `origin` | `control` |
| --- | --- | --- |
| The device created the session and no terminal has registered it | `remote` | `remote` |
| Grok's own session registry (`~/.grok/active_sessions.json`, written by every TUI while it runs) names the session with a live process **and** the leader reports it loaded, which is what a TUI inside the leader looks like | `terminal` when the device did not create the session, otherwise unchanged | `shared` |
| The registry names it with a live process but the leader does not have it loaded: a `grok` started with `use_leader` off, or under a sandbox profile, which runs its own agent | `terminal` | `terminal`, mirrored from the update log as before; `attach_ready` words the hint |
| No terminal has registered it and the device holds it in the leader | unchanged | `remote` while a turn the device started is running, `none` otherwise |
| Known only from disk | unchanged | `none`; the next `session.send` loads it through the leader |

The leader says nothing when a TUI exits and offers no way to ask who is attached, so the registry,
read on the device's ten second scan, is the whole signal, and a session leaves `shared` the way a
Codex thread does: to `remote` while a device turn runs, to `none` otherwise, resumable in place
because the leader still holds it. `session/close` unloads a session for **every** client of the
leader, the terminal included, so the device never sends it for a session a terminal has registered
and never for one it did not create. `session/load` replays the whole conversation with
`_meta.isReplay`; the device publishes only the rows whose `eventId` counter is above the one it
already applied — from the mirror or an earlier attachment — so history is neither doubled nor
lost. Permission prompts fan out to every client; the device offers the leader's own options except
the one that switches the session into always-approve mode, which is a permission policy and so a
`session.set` matter, and an approval another client answered ends as
`decision: {option_id: "elsewhere", by: "terminal"}` exactly as a Codex one does. Grok's questions
fan out the same way and are raised as `question` blocks (A52): an app answers one through
`session.answer`, one another client answered first resolves `by: "terminal"`, and one nobody
answered expires. The first reply of any client settles a prompt for all of them, an error included,
so a device never settles one another client may answer by itself: only an app's answer, or Stop,
does. It sends nothing for a session it does not route and nothing when it lets go of a session a
terminal shares. Apps need no Grok logic: `control` and the five attachment fields of 4.2 say
everything.

`fixtures/app/session.updated.json`

```json
{
  "type": "session.updated",
  "session": {
    "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
    "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
    "agent": "claude",
    "title": "Fix flaky auth refresh test",
    "cwd": "/Users/me/dev/gateway",
    "git": {
      "branch": "main",
      "dirty": false,
      "ahead": 0,
      "behind": 0,
      "worktree": false
    },
    "state": "idle",
    "state_detail": null,
    "origin": "remote",
    "control": "remote",
    "model": "claude-sonnet-4-5",
    "permission_mode": "default",
    "effort": "medium",
    "created_at": 1788942600000,
    "updated_at": 1788944461100,
    "last_seq": 41,
    "archived": false,
    "turn": null,
    "todos": {
      "total": 4,
      "done": 4
    },
    "usage": {
      "input_tokens": 48120,
      "output_tokens": 6210,
      "total_tokens": 54330,
      "context_used": 61000,
      "context_window": 200000,
      "cost_usd": 0.42
    },
    "queued": 0
  }
}
```

A `SessionResume` (amendment A35) is the one resume a session can have pending:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `at` | timestamp | yes | When the device will send the resume prompt |
| `estimated` | boolean | yes | True when the vendor named no reset time and `at` was computed from the window's length |
| `attempts` | integer ≥ 0 | yes | How many resumes have already run into the limit again; the device drops the resume after the third |
| `window_minutes` | integer ≥ 1 | no | The window that was hit, as `AgentLimit.window_minutes` (4.2) |

`fixtures/objects/session.resume-pending.json` is an attached terminal session, idle, with a resume
pending a minute after its five-hour window resets.

### 4.5 Session state

| State | Meaning |
| --- | --- |
| `starting` | The device is creating or resuming the session |
| `idle` | No turn in progress; the composer is live |
| `running` | A turn is in progress |
| `needs_approval` | Sub-state of running: blocked on an `approval` |
| `needs_input` | Sub-state of running: blocked on a `question` |
| `error` | The session failed; `state_detail` says how |
| `stopped` | The agent process ended |
| `readonly` | A mirrored terminal session that is not currently working: `control == "terminal"` **and** no turn in progress. While the terminal-driven turn runs, the device reports `running`, `needs_approval` or `needs_input` as usual (amendment A7). |

A `shared` session reports `idle` when no turn is in progress; `readonly` stays reserved for
`control == "terminal"`. While a turn runs, whether the terminal started it or the device injected
it, the device reports `running`, `needs_approval` or `needs_input` as usual.

### 4.6 Git

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `branch` | string | yes | |
| `dirty` | boolean | yes | Uncommitted changes present |
| `ahead` | integer | yes | Commits ahead of upstream |
| `behind` | integer | yes | Commits behind upstream |
| `worktree` | boolean | yes | True when the session runs in an isolated worktree |

### 4.7 Usage

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `input_tokens` | integer | yes | |
| `output_tokens` | integer | yes | |
| `total_tokens` | integer | yes | |
| `context_used` | integer \| null | no | Tokens currently occupying the context window |
| `context_window` | integer \| null | no | Size of the model's context window |
| `cost_usd` | number \| null | no | Omitted or null when the agent reports no cost |

Only the token totals are guaranteed (amendment A2). An agent that reports no context or cost figures
omits those fields; a UI must render a session whose `usage` carries three fields.
`fixtures/events/turn_completed.no_cost.json` and `fixtures/timelines/codex.json` are the worked
examples.

### 4.8 Attachment

Two shapes carry the same idea in opposite directions.

| Direction | Object | Fields |
| --- | --- | --- |
| App to device (`session.send`) | `AttachmentUpload` | `name`, `mime`, `data_base64` |
| Device to app (`user_message`) | `Attachment` | `name`, `mime`, `size` |

Bytes travel up only. A `user_message` event reports metadata, never the payload.

How the bytes reach the agent is the device's business (A49). Codex takes images as image inputs and
pi as image content. Claude Code and Grok Build take no image in a prompt — a Claude channel carries
text, and Grok answers `promptCapabilities.image: false` — so for them the device writes each file
under its own state directory, one folder per message, and the prompt the agent receives ends with
`Attached files:` and one `- name: path` line per file, which the agent opens with its own file tool;
this holds on a `remote` and a `shared` session alike. No path reaches an app: the `user_message`
carries the words the person typed and the `Attachment` metadata, and a user row the device reads
back from the agent's own record — a Claude transcript, Grok's update log, the leader's replay or
echo — whose prompt ends that way is published as the words before that list and an `Attachment`
per file, `mime` taken from the name and `size` 0 once the file is gone; a title or an `error` the
agent words itself shows a file's name where it quoted a path. The device's echo of its own prompt
on Grok's leader is recognised as its own and is never a second block. The agent opening one of
those files with its own read tool asks nobody: the device allows that read itself, for the
session's own files only, and raises no `approval` block. The files are the device's to delete: a
message that will not reach the agent takes its folder with it, a deleted session all of its
folders, and none is kept longer than a week.

### 4.9 LabeledId

`{"id": "high", "label": "High"}`. Used for models, permission modes and effort levels. Ids are
opaque to the UI, which renders the label and sends back the id unchanged. Approval options extend
this with `style`; question options extend it with an optional `description`.

---

### 4.10 User

The account an app signed in as (A24). It is `user` in `LoginResponse`, `AuthSessionResponse` and
the app `hello`; `UserRecord` is the same account as the admin lists it, with `state`, the
timestamps and the number of devices it has enrolled.

| Field | Type | Notes |
| --- | --- | --- |
| `username` | string | Lower case, 3 to 32 characters, see 3.1. `admin` is the operator. |
| `role` | `admin` \| `member` | What 3.9 is gated on. A `member` sees nothing of any other account. |

---

### 4.11 Command

A slash command a session offers (A27). `fixtures/app/reply.session.commands.json` is the worked
list.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | yes | What the user types after the slash: `compact`, `review`, `hooks-list`, `skill:pdf-tables`. Lower case, `[a-z0-9_:.-]`, never with the slash |
| `description` | string | yes | One line, what it does, in the agent's own words where it has them |
| `argument` | string | no | A placeholder for what may follow the name — `instructions`, `path`. Absent when the command takes nothing. An app shows it as the hint once the name is complete, and sends whatever was typed after the name as `argument` |
| `group` | string | no | Where the command comes from: `Built-in`, `Skills`, `Prompts`, `Extensions`. An app sections the list by it only when more than one group is present |

Apps render `/name` beside `description`, filter by prefix of `name` as the user types, and send
`name` back unchanged.

---

## 5. Session events

Schema: `schema/events.json`. Events travel device → gateway → app inside a `session.event` frame and
are also the record format returned by `session.history`.

```json
{"type": "session.event", "session_id": "…", "event": {"seq": 17, "ts": 1788944409800, "kind": "…"}}
```

Every event carries `seq`, `ts` and `kind`. `ts` is the device clock; **`seq` ordering wins over `ts`
for display order**.

### 5.1 The block model

Six kinds own a block and carry `block_id`, plus the optional `first_seq`: `user_message`,
`assistant_text`, `thinking`, `tool_call`, `approval`, `question`. They may also carry
`parent_block_id`, set when the event was produced inside a sub-agent; the parent is a `tool_call`
with `tool_kind: "subagent"`.

Three rules govern how a timeline is built:

1. **Replacement.** A later event with the same `block_id` fully replaces the earlier one.
2. **Streaming.** The exception is a `delta`: `assistant_text` and `thinking` events that carry
   `delta` append to the block's text. The final event of a stream has `done: true` and the complete
   `text`, and carries no `delta`. Devices flush deltas at most every 80 ms per block.
3. **Ordering.** Display order is `first_seq ?? seq`, where `first_seq` is the `seq` at which the
   block first appeared (amendment A8). Devices set it on replacement events and on
   `session.history` and `session.block` results; on a block's first event it equals `seq` and is
   usually omitted. Without it, a tool call that starts early and finishes late would jump to the
   bottom of the timeline every time it was replaced.

The remaining kinds are not blocks. `todos` and `queue` are snapshots that replace the previous
snapshot of the same kind; `status`, `meta`, `turn_started`, `turn_completed`, `notice` and `error`
are timeline entries or state updates.

### 5.2 `user_message`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `block_id` | uuid | yes | |
| `first_seq` | integer | no | Where the block started; order by `first_seq ?? seq` |
| `text` | string | yes | |
| `attachments` | `Attachment[]` | no | Metadata only |
| `source` | `remote` \| `terminal` \| `queue` \| `agent` \| `resume` | yes | Where the message came from. `agent` is a message the CLI put into the conversation on behalf of another agent — a teammate's message, a background task's notification — that nobody typed (A30). `resume` is the prompt the device sent for the person once a usage limit reset (A35, 7.2) |
| `delivery` | `delivered` \| `absorbed` | no | Set only on `shared` sessions; see below |

The `user_message` a device emits for an app's `session.send` carries the request's `id` as its
`block_id` (amendment A12). An app therefore renders the message the moment it is sent, under that
id and with `source: "remote"`, and the device's event later replaces it under rule 5.1.1; nothing
in the timeline moves, and a retry with the same `id` (2.4) lands on the same block. Messages the
device originates itself (typed in a terminal, replayed from a queue whose item came without an id)
keep device-minted ids.

A steered message is the exception to "at once" on the device side (amendment A14). When a
`session.send` is answered with `accepted: "steered"`, the agent does not read the message where it
was sent but at its next step, after whatever it was already saying; a terminal on the same session
shows the prompt there. The device therefore emits the `user_message` only when the agent reports
having taken the message (for Codex, the daemon's `userMessage` item for it), so `first_seq` places
it after the output that preceded it. The app keeps its optimistic row at the bottom until then —
that is where the message will land. If the turn ends without the agent ever taking the message,
the device emits the block at the turn's end, with a `notice` of level `warning` when the turn was
interrupted, so the message is shown exactly once either way. On a `shared` Claude session (A51)
the agent takes a steered message at the turn's next batch of tool calls, and the block is emitted
once the transcript shows it there, beside what the CLI folded into the turn at the same boundary —
the person's prompt queued in the terminal, a teammate's message, a task's notification — which is
published there too, as it would be had it started a turn (A30); the blocks come in the order the
model reads them, which is the CLI's: the folded messages first from Claude Code 2.1.294 on, the
steered one first in 2.1.291. A turn with no batch left takes it as it ends, as the next turn, and
the block is emitted when that turn starts, after the turn it follows has ended.

`delivery` reports what happened to a message injected into an attached CLI and is absent for an
ordinary prompt. `delivered` means it was injected into the CLI. `absorbed` means the CLI treated
it as mid-turn data rather than a prompt, so the device injects it again at the next turn's end, at
most three times; absorbed once more, the device publishes an `error` naming the message under the
block's own `block_id` (5.14), which apps draw in the bubble's place, because nothing delivered it —
never `delivered` (A51).
A message the device accepted while the terminal's turn was running is not a block at all until it
is injected (amendment A19): it is a `queue` entry (5.12), the app keeps it in its queue as it does
for any queued message, and the `user_message` appears only when the CLI takes it, so `first_seq`
places it after the output of the turn it waited for — the order the terminal shows. A message
steered into the turn instead (A51) is no `queue` entry either; its block appears where the model
reads it, as A14 says, with `delivery: "delivered"`. The block
keeps its `block_id` throughout, and each re-injection is a replacement event carrying
`delivery: "delivered"`. `source` stays `remote` for messages apps send into a shared session and
`terminal` for messages typed in the CLI.

`source: "agent"` (amendment A30) is for the words Claude Code files as user turns that no person
typed: the message one Claude session sends another ("Another Claude session sent a message:" with a
`<teammate-message>` envelope) and the notification a background task or subagent leaves
(`<task-notification>`). The device publishes them as `user_message` with `source: "agent"` and
`text` reduced to what a reader wants — who reported and what they said, a task's summary and
result — never the envelope, and never the `<system-reminder>` blocks the CLI attaches to a turn,
which are dropped wherever they appear; a row that held nothing else produces no block. A subagent's
result that arrives as a tool result is a `tool_call` already and is unchanged. Apps draw an `agent`
message on the agent's side of the conversation — never in the person's bubble — as a muted block
captioned "from another agent", and their Simple detail level hides it as it hides the agent's
other workings (amendment A34); a turn such a message starts carries `trigger: "agent"` (5.9),
which the status line treats as it treats `terminal`. Rows the CLI marks `isMeta`, including the echo of a message this device injected through
its channel, stay out of the timeline as before.

Three more rows Claude Code files as user turns are not prompts either (amendment A32). The summary
the CLI writes after compacting the context (`isCompactSummary: true`, which its own view hides) is
not a block at all: the `compact_boundary` row that precedes it becomes the `notice` of 5.13, as a
Codex compaction already does. The marker the CLI leaves when the person interrupts a turn
(`[Request interrupted by user]`, `[Request interrupted by user for tool use]`) is not a block and
starts no turn: it ends the running one with `stop_reason: "interrupted"`, and a tool result carried
by the same row is published as usual. A slash command typed at the terminal, which the CLI records
as `<command-name>` with `<command-args>`, is the person's words: `user_message {source:
"terminal"}` whose text is the command and its argument as typed (`/model haiku`), published once
even where the CLI records the same keystroke twice, as it does for `/compact`. A typed command
starts no turn; what the CLI printed in reply (`<local-command-stdout>`) is not a block, and ends a
turn that was running as `completed`, since the CLI's reply is the end of what the command did.

`fixtures/events/user_message.agent.json`

```json
{
  "seq": 9,
  "ts": 1788944409000,
  "kind": "user_message",
  "block_id": "3a1f9d2c-6b7e-4c5d-9e8f-0a1b2c3d4e5f",
  "text": "recon-ios: Recon complete. Fact sheet written to the scratchpad; three findings need a decision.",
  "source": "agent"
}
```

`fixtures/events/user_message.json`

```json
{
  "seq": 2,
  "ts": 1788944401000,
  "kind": "user_message",
  "block_id": "78c122e5-9703-4501-bff4-e6e6d8a27efe",
  "text": "Have a look at the screenshot and tell me what broke the layout.",
  "attachments": [
    {
      "name": "settings-drawer.png",
      "mime": "image/png",
      "size": 284913
    }
  ],
  "source": "remote"
}
```

`source: "resume"` (amendment A35) is the one message the device writes for the person: the fixed
sentence of 7.2, sent when the usage limit that stopped the session has reset. Apps draw it in the
person's bubble, captioned as sent for them after the limit reset, so the transcript says who
continued the work and why; the turn it starts carries `trigger: "resume"` (5.9).

`fixtures/events/user_message.resume.json`

```json
{
  "seq": 42,
  "ts": 1788966060000,
  "kind": "user_message",
  "block_id": "4c9e7a12-5b3d-4f80-a6e1-8d2c0b7f3e54",
  "text": "The usage limit has reset. Continue where you left off, and let any subagents you started continue their work.",
  "source": "resume"
}
```

### 5.3 `assistant_text`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `block_id` | uuid | yes | |
| `first_seq` | integer | no | Where the block started; order by `first_seq ?? seq` |
| `delta` | string | when `done` is false | Appended to the block |
| `text` | string | when `done` is true | The complete text |
| `done` | boolean | yes | |

History contains final events only.

`fixtures/events/assistant_text.delta.json`

```json
{
  "seq": 8,
  "ts": 1788944403300,
  "kind": "assistant_text",
  "block_id": "5ccffa9e-4949-4c98-a8a5-066f320bd531",
  "delta": "Let me read the test and the token helper.",
  "done": false
}
```

`fixtures/events/assistant_text.done.json`

```json
{
  "seq": 9,
  "ts": 1788944403500,
  "kind": "assistant_text",
  "first_seq": 8,
  "block_id": "5ccffa9e-4949-4c98-a8a5-066f320bd531",
  "text": "Let me read the test and the token helper.",
  "done": true
}
```

### 5.4 `thinking`

Same fields and streaming rule as `assistant_text`, plus `duration_ms` (integer, optional) on the
final event. The content may be a summary rather than the full reasoning.

`fixtures/events/thinking.done.json`

```json
{
  "seq": 7,
  "ts": 1788944402900,
  "kind": "thinking",
  "first_seq": 5,
  "block_id": "e3c776b7-73e5-449f-a25e-25cec51cf406",
  "text": "The failure is timing dependent, so the refresh window is probably compared against a wall clock that can drift.",
  "done": true,
  "duration_ms": 4200
}
```

### 5.5 `tool_call`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `block_id` | uuid | yes | |
| `first_seq` | integer | no | Where the block started; order by `first_seq ?? seq` |
| `tool` | string | yes | The agent's own tool name, for example `Bash` |
| `tool_kind` | `shell` \| `read` \| `edit` \| `write` \| `search` \| `web` \| `mcp` \| `subagent` \| `todo` \| `other` | yes | Drives the icon and the collapsed row |
| `title` | string | yes | One-line human summary produced by the device: command line, file path, query |
| `status` | `running` \| `succeeded` \| `failed` \| `cancelled` | yes | |
| `input` | object | no | At most 8 KiB of JSON |
| `input_truncated` | boolean | no | |
| `output` | string | no | At most 16 KiB |
| `output_truncated` | boolean | no | |
| `summary` | string | no | Short result badge, for example `2 failed` |
| `diff` | `{path, additions, deletions, patch?, patch_truncated?}` | no | `patch` is at most 32 KiB |
| `started_at` | timestamp | yes | |
| `ended_at` | timestamp | no | Present once the call finishes |
| `duration_ms` | integer | no | |

A device emits the same `block_id` twice: once with `status: "running"` (optionally with partial
`output` for a live output box) and once with the terminal status. Use `session.block` to fetch the
untruncated version.

`fixtures/events/tool_call.shell.json`

```json
{
  "seq": 26,
  "ts": 1788944430700,
  "kind": "tool_call",
  "first_seq": 25,
  "block_id": "d026b2fb-c6fd-4a29-bbdc-34b98ee362bb",
  "tool": "Bash",
  "tool_kind": "shell",
  "title": "pytest -k refresh --count 20",
  "status": "failed",
  "input": {
    "command": "pytest -k refresh --count 20",
    "timeout": 120000
  },
  "output": "============================= test session starts ==============================\nplatform darwin -- Python 3.12.12, pytest-8.3.4, pluggy-1.5.0\nrootdir: /Users/me/dev/gateway\nplugins: anyio-4.7.0, asyncio-0.25.0, repeat-0.9.3\ncollected 20 items\n\ntests/test_auth.py ..F.................F                                  [100%]\n\n=================================== FAILURES ===================================\n__________________ test_refresh_window_is_stable[iteration-3] __________________\n\n    def test_refresh_window_is_stable() -> None:\n        token = issue(ttl=30)\n        freeze(token.expires_at - 1)\n>       assert should_refresh(token) is True\nE       assert False is True\n\ntests/test_auth.py:118: AssertionError\n",
  "output_truncated": true,
  "summary": "2 failed",
  "started_at": 1788944424300,
  "ended_at": 1788944430700,
  "duration_ms": 6400
}
```

`fixtures/events/tool_call.edit.json`

```json
{
  "seq": 23,
  "ts": 1788944424000,
  "kind": "tool_call",
  "block_id": "9d231cee-9a1c-48da-8f14-77c0746ced34",
  "tool": "Edit",
  "tool_kind": "edit",
  "title": "rc_gateway/auth/tokens.py",
  "status": "succeeded",
  "input": {
    "file_path": "/Users/me/dev/gateway/rc_gateway/auth/tokens.py"
  },
  "diff": {
    "path": "rc_gateway/auth/tokens.py",
    "additions": 4,
    "deletions": 2,
    "patch": "--- a/rc_gateway/auth/tokens.py\n+++ b/rc_gateway/auth/tokens.py\n@@ -41,9 +41,11 @@ def should_refresh(token: Token) -> bool:\n-    remaining = token.expires_at - time.time()\n-    return remaining < REFRESH_WINDOW\n+    remaining = token.expires_at - time.monotonic_offset()\n+    skew = min(MAX_CLOCK_SKEW, REFRESH_WINDOW / 2)\n+    return remaining < REFRESH_WINDOW + skew\n"
  },
  "summary": "+4 -2",
  "started_at": 1788944424000,
  "ended_at": 1788944424100,
  "duration_ms": 90
}
```

`fixtures/events/tool_call.subagent.json`

```json
{
  "seq": 18,
  "ts": 1788944409200,
  "kind": "tool_call",
  "first_seq": 15,
  "block_id": "5ca335d3-3c57-4026-9358-49567115efde",
  "tool": "Task",
  "tool_kind": "subagent",
  "title": "Audit clock handling in the auth package",
  "status": "succeeded",
  "input": {
    "description": "Audit clock handling",
    "subagent_type": "Explore"
  },
  "summary": "time.time() is compared with a monotonic deadline; up to 2 s of skew",
  "started_at": 1788944405300,
  "ended_at": 1788944409200,
  "duration_ms": 4300
}
```

### 5.6 `todos`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `items` | `{id, text, status}[]` | yes | `status` is `pending`, `in_progress` or `completed` |

A snapshot that replaces the previous list. It carries no `block_id`.

`fixtures/events/todos.json`

```json
{
  "seq": 12,
  "ts": 1788944404400,
  "kind": "todos",
  "items": [
    {
      "id": "t1",
      "text": "Reproduce the flake",
      "status": "completed"
    },
    {
      "id": "t2",
      "text": "Find the timing dependency",
      "status": "completed"
    },
    {
      "id": "t3",
      "text": "Fix the refresh window",
      "status": "completed"
    },
    {
      "id": "t4",
      "text": "Run the test 20 times",
      "status": "completed"
    }
  ]
}
```

### 5.7 `approval`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `block_id` | uuid | yes | |
| `first_seq` | integer | no | Where the block started; order by `first_seq ?? seq` |
| `request_id` | uuid | yes | Quoted back in `session.approve` |
| `tool` | string | yes | |
| `tool_kind` | see 5.5 | yes | |
| `title` | string | yes | |
| `input` | object | no | |
| `diff` | see 5.5 | no | Present for edit and write approvals |
| `options` | `{id, label, style}[]` | yes | `style` is `primary`, `secondary` or `danger` |
| `status` | `pending` \| `resolved` \| `expired` | yes | |
| `decision` | `{option_id, by}` | no | `by` is `remote`, `terminal` or `policy` |

Option ids are chosen by the device adapter and are **opaque to the UI**, which renders exactly the
options it is given and never assumes a specific id. Claude typically offers `allow`,
`allow_session`, `deny`; Codex offers `accept`, `acceptForSession`, `decline`, `cancel`. Every
`approval` includes at least one option with `style: "primary"` (accept) and one with
`style: "danger"` (reject), so a UI can place them consistently.

On a `shared` session the device emits an `approval` block for every permission request the
attachment relays. Through a Claude channel `options` are exactly
`[{id: "allow", label: "Allow", style: "primary"}, {id: "deny", label: "Deny", style: "danger"}]`,
because session-scoped grants are not available through that relay. `input` carries
`{tool_name, description, input_preview}` as the relay supplies it, and `diff` is absent since the
relay provides none. The dialog in the terminal stays open alongside the relayed request and
whichever side answers first wins; when the terminal answers first the device resolves the block
with `decision.by: "terminal"`. A pending approval becomes `expired` when the CLI exits or the
attachment drops.

Through the Codex shared daemon the options mirror what the daemon offers for that particular
prompt, so a `shared` Codex approval carries up to four of them:

| Option | Label | `style` |
| --- | --- | --- |
| `allow` | Allow | `primary` |
| `allow_session` | Allow for this session | `secondary` |
| `allow_always` | Always allow commands like this | `secondary` |
| `deny` | Deny | `danger` |

The device sends only the options the daemon lists for that request, and every set still contains one
`primary` and one `danger` option. `input` carries `{command, cwd, command_actions}` for a command
approval and the summary of the proposed change for a file approval; `diff` is present when the
daemon supplies one. `request_id` is minted by the device, as it is for every relayed approval.

The terminal and the device see the same Codex request and either can answer it, but the daemon does
not say who answered or what they chose. When the device learns that a request it did not answer has
been resolved, it resolves the block with `decision: {option_id: "elsewhere", by: "terminal"}`.
`elsewhere` is an ordinary option id that never appears in `options`; an app renders it as "answered
in the terminal" and never sends it back. `fixtures/events/approval.codex-shared-pending.json` and
`fixtures/events/approval.codex-elsewhere.json` are the worked pair.

`fixtures/events/approval.pending.json`

```json
{
  "seq": 19,
  "ts": 1788944409800,
  "kind": "approval",
  "block_id": "3db744e1-a6c3-4f21-bc51-a8ca32c6cd9a",
  "request_id": "0f99737c-e8c1-4242-ac23-42f50fe1654e",
  "tool": "Edit",
  "tool_kind": "edit",
  "title": "rc_gateway/auth/tokens.py",
  "input": {
    "file_path": "/Users/me/dev/gateway/rc_gateway/auth/tokens.py"
  },
  "diff": {
    "path": "rc_gateway/auth/tokens.py",
    "additions": 4,
    "deletions": 2,
    "patch": "--- a/rc_gateway/auth/tokens.py\n+++ b/rc_gateway/auth/tokens.py\n@@ -41,9 +41,11 @@ def should_refresh(token: Token) -> bool:\n-    remaining = token.expires_at - time.time()\n-    return remaining < REFRESH_WINDOW\n+    remaining = token.expires_at - time.monotonic_offset()\n+    skew = min(MAX_CLOCK_SKEW, REFRESH_WINDOW / 2)\n+    return remaining < REFRESH_WINDOW + skew\n"
  },
  "options": [
    {
      "id": "allow",
      "label": "Allow",
      "style": "primary"
    },
    {
      "id": "allow_session",
      "label": "Allow for this session",
      "style": "secondary"
    },
    {
      "id": "deny",
      "label": "Deny",
      "style": "danger"
    }
  ],
  "status": "pending"
}
```

`fixtures/events/approval.resolved.json`

```json
{
  "seq": 21,
  "ts": 1788944423800,
  "kind": "approval",
  "first_seq": 19,
  "block_id": "3db744e1-a6c3-4f21-bc51-a8ca32c6cd9a",
  "request_id": "0f99737c-e8c1-4242-ac23-42f50fe1654e",
  "tool": "Edit",
  "tool_kind": "edit",
  "title": "rc_gateway/auth/tokens.py",
  "input": {
    "file_path": "/Users/me/dev/gateway/rc_gateway/auth/tokens.py"
  },
  "diff": {
    "path": "rc_gateway/auth/tokens.py",
    "additions": 4,
    "deletions": 2,
    "patch": "--- a/rc_gateway/auth/tokens.py\n+++ b/rc_gateway/auth/tokens.py\n@@ -41,9 +41,11 @@ def should_refresh(token: Token) -> bool:\n-    remaining = token.expires_at - time.time()\n-    return remaining < REFRESH_WINDOW\n+    remaining = token.expires_at - time.monotonic_offset()\n+    skew = min(MAX_CLOCK_SKEW, REFRESH_WINDOW / 2)\n+    return remaining < REFRESH_WINDOW + skew\n"
  },
  "options": [
    {
      "id": "allow",
      "label": "Allow",
      "style": "primary"
    },
    {
      "id": "allow_session",
      "label": "Allow for this session",
      "style": "secondary"
    },
    {
      "id": "deny",
      "label": "Deny",
      "style": "danger"
    }
  ],
  "status": "resolved",
  "decision": {
    "option_id": "allow",
    "by": "remote"
  }
}
```

### 5.8 `question`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `block_id` | uuid | yes | |
| `first_seq` | integer | no | Where the block started; order by `first_seq ?? seq` |
| `request_id` | uuid | yes | Quoted back in `session.answer` |
| `questions` | `QuestionItem[]` | yes | |
| `status` | `pending` \| `resolved` \| `expired` | yes | |
| `answers` | map | no | Question id to a list of option ids, or to free text |
| `by` | `remote` \| `terminal` | no | On a resolved question: who answered it (amendment A20) |

`QuestionItem` is `{id, prompt, options: [{id, label, description?}], multi, allow_text, secret?}`.
`multi` allows several options; `allow_text` allows a free-text answer; `secret` asks the UI to mask
the input.

On an attached Claude Code session (`control: "shared"`) the CLI's own question dialog and this block
exist at the same time (amendment A20). The device raises the block from the `PermissionRequest`
hook Claude Code runs beside the dialog, reports `needs_input`, and answers the hook with the
first reply that arrives: a `session.answer` from an app, or the dialog in the terminal. The other
side then sees the block `resolved` with `by` saying who answered, and the CLI's dialog is
withdrawn when the app was first. A question the device could not raise this way — an older shim
without the hook — stays what it was before: a running `tool_call` the terminal answers alone.

Grok Build asks with a request of its own, `_x.ai/ask_user_question`, and the device raises the
block from it on a session it drives and on one a TUI shares through the leader alike (A52). A
question past the fourth is not shown and goes unanswered. When another client answered first,
`answers` is present only where Grok's own summary names the question and option labels exactly; a
resolved block without `answers` means the answer named nothing the device could match, whether free
text or a question or label worded otherwise than the device read it. A Grok option's `preview` has
no field here and is left out.

`fixtures/events/question.resolved.terminal.json` is the block after the person at the terminal
answered.

`fixtures/events/question.pending.json`

```json
{
  "seq": 28,
  "ts": 1788944431500,
  "kind": "question",
  "block_id": "c918d535-0a55-4521-8254-66c9bec5be9d",
  "request_id": "dbe1ce62-ee02-41b3-a229-50b5b4689610",
  "questions": [
    {
      "id": "q1",
      "prompt": "Two runs still fail on a machine whose clock jumps. How should the refresh window treat skew?",
      "options": [
        {
          "id": "clamp",
          "label": "Clamp skew to half the window",
          "description": "Safest; never refreshes early by more than 2.5 s."
        },
        {
          "id": "monotonic",
          "label": "Switch to a monotonic deadline",
          "description": "Bigger change, touches the issue path."
        },
        {
          "id": "widen",
          "label": "Widen the window to 10 s"
        }
      ],
      "multi": false,
      "allow_text": true
    }
  ],
  "status": "pending"
}
```

### 5.9 `turn_started` and `turn_completed`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `turn_id` | uuid | yes | Matches `Session.turn.turn_id` |
| `trigger` | `remote` \| `terminal` \| `queue` \| `agent` \| `resume` | yes | `turn_started` only. `agent` is a turn started by a message another agent put into the conversation (A30); `resume` is a turn the device started once a usage limit reset (A35) |
| `stop_reason` | `completed` \| `interrupted` \| `error` | yes | `turn_completed` only |
| `duration_ms` | integer | yes | `turn_completed` only |
| `usage` | `Usage` | no | `turn_completed` only |
| `limit` | `LimitStop` | no | `turn_completed` only. Present when the turn ended because the vendor's usage limit was reached; `stop_reason` is `error` (A35) |

`fixtures/events/turn_started.json`

```json
{
  "seq": 3,
  "ts": 1788944401100,
  "kind": "turn_started",
  "turn_id": "ad5ec709-3ce6-44c6-80a5-af2cb46308bc",
  "trigger": "remote"
}
```

`fixtures/events/turn_completed.json`

```json
{
  "seq": 40,
  "ts": 1788944461100,
  "kind": "turn_completed",
  "turn_id": "ad5ec709-3ce6-44c6-80a5-af2cb46308bc",
  "stop_reason": "completed",
  "duration_ms": 71400,
  "usage": {
    "input_tokens": 48120,
    "output_tokens": 6210,
    "total_tokens": 54330,
    "context_used": 61000,
    "context_window": 200000,
    "cost_usd": 0.42
  }
}
```

A Codex turn reports no cost, so `usage` carries the token totals only:

`fixtures/events/turn_completed.no_cost.json`

```json
{
  "seq": 36,
  "ts": 1788945049900,
  "kind": "turn_completed",
  "turn_id": "e411cdfb-4811-467a-9057-20393b6abb21",
  "stop_reason": "completed",
  "duration_ms": 19600,
  "usage": {
    "input_tokens": 26800,
    "output_tokens": 3640,
    "total_tokens": 30440,
    "context_used": 36900,
    "context_window": 272000
  }
}
```

A turn the vendor's usage limit ended (amendment A35) is an `error` stop that says so, and says when
the limit resets when the vendor did. A `LimitStop` is `{window_minutes?, resets_at}`: the window
that was hit as `AgentLimit.window_minutes` (300 for five hours, 10080 for a week) when it is known,
and `resets_at` null when the vendor named no time. The device reads the limit from the agent, never
from its words: Claude Code's result with HTTP status 429 — `api_error_status` in the SDK's result
message, `apiErrorStatus` with `quotaLimits.resetsAt` in the transcript row — and Codex's
`turn/completed` whose error is `usageLimitExceeded`, with the window from `account/rateLimits/read`.
The vendor's sentence goes out as the `error` of 5.14, not as the agent's text. Grok Build and pi
report no limit the device can read, so their turns never carry it.

`fixtures/events/turn_completed.limit.json`

```json
{
  "seq": 40,
  "ts": 1788947998000,
  "kind": "turn_completed",
  "turn_id": "b7e2a5c4-3f1d-4a8e-9c6b-0d2f4e8a1b37",
  "stop_reason": "error",
  "duration_ms": 1200,
  "limit": {
    "window_minutes": 300,
    "resets_at": 1788966000000
  }
}
```

A turn a resume started (7.2) carries `trigger: "resume"`; the status line reads it as a remote turn.

`fixtures/events/turn_started.resume.json`

```json
{
  "seq": 43,
  "ts": 1788966060100,
  "kind": "turn_started",
  "turn_id": "9f4b2d1e-7c3a-4e58-b1d6-2a8f0c5e7d19",
  "trigger": "resume"
}
```

### 5.10 `status`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `state` | see 4.5 | yes | |
| `detail` | string | no | One line of context |

Emitted on every state change. Along with `turn_started` and `turn_completed` these are the only
events that change a session's state.

`fixtures/events/status.json`

```json
{
  "seq": 20,
  "ts": 1788944409800,
  "kind": "status",
  "state": "needs_approval",
  "detail": "Edit rc_gateway/auth/tokens.py"
}
```

### 5.11 `meta`

A partial update of `Session` fields: `title`, `model`, `permission_mode`, `effort`, `speed`, `cwd`,
`git`, `control`, `agent_version`. Every field is optional; apply only what is present.

`fixtures/events/meta.json`

```json
{
  "seq": 88,
  "ts": 1788945300000,
  "kind": "meta",
  "title": "Fix flaky auth refresh test",
  "model": "claude-opus-4-6",
  "permission_mode": "acceptEdits",
  "effort": "high",
  "cwd": "/Users/me/dev/gateway",
  "git": {
    "branch": "fix/refresh-window",
    "dirty": true,
    "ahead": 1,
    "behind": 0,
    "worktree": true
  },
  "control": "remote",
  "agent_version": "2.1.266"
}
```

### 5.12 `queue`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `pending` | `{id, text, ts, attachments?}[]` | yes | `id` is the id of the original `session.send` request. `attachments` is how many files the held message carries, absent when it carries none (A43) |

A snapshot of queued remote messages, in the order they will be delivered. Remove one with
`session.queue_remove`. An app edits one by taking it out with `session.queue_remove` and sending
the edited words back with `mode: "queue"` and that entry's `ts` as `queue_ts` (6.3): the device
holds them in the place the entry left (A43). The files of a held message stay on the device and
no snapshot carries them, so an entry with `attachments` can be removed but not edited.

`fixtures/events/queue.json`

```json
{
  "seq": 22,
  "ts": 1788945028800,
  "kind": "queue",
  "pending": [
    {
      "id": "573d2674-debc-4db0-973c-16e8a7c2e9b1",
      "text": "Then run the full test suite.",
      "ts": 1788945028800
    }
  ]
}
```

A queue holding two messages, the second with two files, which can be removed but not edited:
`fixtures/events/queue.attachments.json`

```json
{
  "seq": 24,
  "ts": 1788945031200,
  "kind": "queue",
  "pending": [
    {
      "id": "573d2674-debc-4db0-973c-16e8a7c2e9b1",
      "text": "Then run the full test suite.",
      "ts": 1788945028800
    },
    {
      "id": "807da3ea-84ba-4e5d-b26d-77a153262986",
      "text": "These two screenshots show the drawer before and after.",
      "ts": 1788945031200,
      "attachments": 2
    }
  ]
}
```

### 5.13 `notice`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `level` | `info` \| `warn` \| `error` | yes | |
| `text` | string | yes | |

System lines: context compaction, takeover, reconnect. A Claude compaction reaches the apps this
way too: the device emits this line for the `compact_boundary` row a transcript records, or for
the `compact_boundary` system message the SDK streams, and the summary itself is never a block
(A32).

`fixtures/events/notice.json`

```json
{
  "seq": 27,
  "ts": 1788944431000,
  "kind": "notice",
  "level": "warn",
  "text": "Context was compacted; earlier turns are summarised."
}
```

### 5.14 `error`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `message` | string | yes | |
| `code` | error code | no | |
| `block_id` | uuid | no | Set when the failure belongs to a block |

`fixtures/events/error.json`

```json
{
  "seq": 91,
  "ts": 1788945330000,
  "kind": "error",
  "message": "The agent process exited before the turn completed.",
  "code": "internal",
  "block_id": "cbe34579-6f2c-4db5-9d2d-b95a7a2f4468"
}
```

### 5.15 `resume`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `status` | `scheduled` \| `rescheduled` \| `fired` \| `cancelled` \| `dropped` | yes | |
| `at` | timestamp | no | For `scheduled` and `rescheduled`: when the prompt will be sent |
| `estimated` | boolean | no | For `scheduled` and `rescheduled`: `at` was computed from the window's length, not given by the vendor |
| `attempts` | integer ≥ 0 | no | For `rescheduled`: how many resumes have run into the limit again |
| `reason` | string | no | For `cancelled` and `dropped`: why, in the device's words, one line |

What the device did about a session the usage limit stopped (amendment A35, 7.2), as a row of the
timeline: `scheduled` when it set the resume after a limit stop or a person asked for one,
`rescheduled` when the time moved — the person changed it, or the resumed turn ran into the limit
again — `fired` the moment it sent the prompt, `cancelled` when a person or the switch ended it, and
`dropped` when it could not resume: the terminal that owned the session is gone, or the third try
hit the limit too. The pending resume also travels as `Session.resume` (4.4), which is what an app
draws above the transcript; the event is the record in the timeline, and the gateway's cue for a
push (3.7). Like `notice`, it changes no state.

`fixtures/events/resume.json`

```json
{
  "seq": 41,
  "ts": 1788948000000,
  "kind": "resume",
  "status": "scheduled",
  "at": 1788966060000,
  "estimated": false
}
```

`fixtures/events/resume.dropped.json`

```json
{
  "seq": 42,
  "ts": 1788966060000,
  "kind": "resume",
  "status": "dropped",
  "reason": "The terminal that owned this session was closed."
}
```

---

## 6. App ↔ gateway frames

Schema: `schema/app_frames.json`. Endpoint `WS /ws/app`.

### 6.1 Gateway → app

On connect the gateway sends `hello`, then pushes updates for as long as the socket lives. `user`
is the account the socket signed in as (`User`, 4.10). Every frame after `hello` concerns that
account's devices and the sessions on them and nothing else (A24): `device.updated`,
`session.updated`, `session.removed`, `session.event` and `pairing.progress` reach only the owner's
sockets, and a `session.subscribe` or a forwarded request that names another account's session or
device is answered `not_found`, exactly as one naming nothing would be.

| Type | Payload | When |
| --- | --- | --- |
| `hello` | `protocol`, `gateway_version`, `user`, `devices`, `sessions`, `stt`, `polish`, `apps`, `preferences`, `server_time` | First frame. `polish` (A29) may be absent on a gateway older than it, which means disabled; `apps` (A31) likewise, which means no minimum; `preferences` (A35) likewise, which means the switches it holds are not offered |
| `device.updated` | `device` | A device connects, disconnects, is renamed or re-detects agents |
| `device.removed` | `device_id` | A device is deleted |
| `session.updated` | `session` | Any change to a session summary |
| `session.removed` | `session_id`, `device_id` | A session is deleted |
| `session.event` | `session_id`, `device_id`, `event` | Only for sessions this connection subscribed to |
| `terminal.output` | `terminal_id`, `device_id`, `seq`, `data` | Only to the one connection holding the terminal (7.3, A38); the device's `to` is stripped and `device_id` added |
| `terminal.exited` | `terminal_id`, `device_id`, `code` | The shell ended; to the holder alone (A38) |
| `pairing.progress` | `code`, `step`, `device?` | While a pairing code is outstanding. `step` is `waiting`, `enrolled`, `online` or `agents`. |
| `preferences.updated` | `preferences` | The account's preferences changed, from this app or another (A35) |
| `ping` | – | Every 25 s; the app replies `pong` |
| `reply` | `id`, `ok`, `result` or `error` | Answer to a request |

The gateway stamps `device_id` on every `session.event` and `session.removed` frame it sends to
an app, because it knows the device from the socket (amendment A5). Devices never send it.
`session_id` remains globally unique and is the primary key an app indexes by; `device_id` is
informational and for routing.

`fixtures/app/hello.json` (abridged)

```json
{
  "type": "hello",
  "protocol": 1,
  "gateway_version": "0.1.0",
  "user": {
    "username": "admin"
  },
  "devices": [
    {
      "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
      "name": "mac-studio-office",
      "platform": "macos",
      "hostname": "mac-studio.local",
      "arch": "arm64",
      "client_version": "0.1.0",
      "online": true,
      "last_seen": 1788944400000,
      "created_at": 1788426000000,
      "latency_ms": 18,
      "agents": [
        {
          "agent": "claude",
          "available": true,
          "version": "2.1.266",
          "path": "/Users/me/.local/bin/claude",
          "models": [
            {
              "id": "claude-opus-4-6",
              "label": "Opus 4.6"
            }
          ],
          "default_model": "claude-sonnet-4-5",
          "permission_modes": [
            {
              "id": "default",
              "label": "Ask before edits"
            }
          ],
          "default_permission_mode": "default",
          "efforts": [
            {
              "id": "low",
              "label": "Low"
            }
          ],
          "default_effort": "medium",
          "capabilities": [
            "worktree",
            "takeover",
            "interrupt",
            "queue",
            "attachments",
            "effort",
            "history"
          ]
        }
      ]
    }
  ],
  "sessions": [
    {
      "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
      "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
      "agent": "claude",
      "title": "Fix flaky auth refresh test",
      "cwd": "/Users/me/dev/gateway",
      "git": {
        "branch": "main",
        "dirty": false,
        "ahead": 0,
        "behind": 0,
        "worktree": false
      },
      "state": "idle",
      "state_detail": null,
      "origin": "remote",
      "control": "remote",
      "model": "claude-sonnet-4-5",
      "permission_mode": "default",
      "effort": "medium",
      "created_at": 1788942600000,
      "updated_at": 1788944461100,
      "last_seq": 41,
      "archived": false,
      "turn": null,
      "todos": {
        "total": 4,
        "done": 4
      },
      "usage": {
        "input_tokens": 48120,
        "output_tokens": 6210,
        "total_tokens": 54330,
        "context_used": 61000,
        "context_window": 200000,
        "cost_usd": 0.42
      },
      "queued": 0
    }
  ],
  "stt": {
    "enabled": true,
    "languages": [
      "auto"
    ]
  },
  "polish": {
    "enabled": true
  },
  "apps": {
    "ios": {
      "minimum_version": "0.1.0",
      "update_url": "https://testflight.apple.com/join/EXAMPLE"
    },
    "macos": {
      "minimum_version": "1.11.0"
    },
    "android": {
      "minimum_version": "1.12.0"
    },
    "windows": {
      "minimum_version": "1.12.0"
    }
  },
  "server_time": 1788944400000
}
```

The device, session and option arrays are shortened here; the fixture holds the full frame.

`fixtures/app/device.removed.json`

```json
{
  "type": "device.removed",
  "device_id": "1fcda02a-8af6-4019-bef9-2a9dfacae4a3"
}
```

`fixtures/app/session.removed.json`

```json
{
  "type": "session.removed",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712"
}
```

`fixtures/app/session.event.json`

```json
{
  "type": "session.event",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "event": {
    "seq": 11,
    "ts": 1788944404200,
    "kind": "tool_call",
    "first_seq": 10,
    "block_id": "3216f1bc-6f4f-4cc4-8666-3370bb8e3b97",
    "tool": "Read",
    "tool_kind": "read",
    "title": "tests/test_auth.py",
    "status": "succeeded",
    "input": {
      "file_path": "/Users/me/dev/gateway/tests/test_auth.py"
    },
    "output": "  1\timport time\n  2\t\n  3\tfrom rc_gateway.auth.tokens import issue, should_refresh\n",
    "output_truncated": true,
    "summary": "84 lines",
    "started_at": 1788944403700,
    "ended_at": 1788944404200,
    "duration_ms": 480
  },
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712"
}
```

`fixtures/app/pairing.progress.json` (abridged)

```json
{
  "type": "pairing.progress",
  "code": "RC-7K42-QX9M",
  "step": "agents"
}
```

`fixtures/app/ping.json`

```json
{
  "type": "ping"
}
```

`fixtures/app/reply.error.json`

```json
{
  "type": "reply",
  "id": "d7c20931-eada-4d1f-9df5-e8426a3d8dcd",
  "ok": false,
  "error": {
    "code": "conflict",
    "message": "controlled by terminal; take over first"
  }
}
```

### 6.2 App → gateway, handled by the gateway

| Type | Fields | Reply |
| --- | --- | --- |
| `session.subscribe` | `id`, `session_id`, `since_seq?` | `{session, events, resync, queue?}` |
| `session.unsubscribe` | `session_id` | none |
| `session.seen` | `id`, `session_id` | `{}` |
| `pong` | – | none |

`events` are the buffered events with `seq > since_seq`. Omitting `since_seq` returns an empty array,
which tells the app to call `session.history`. `resync: true` means the replay buffer no longer
covers `since_seq`, so the app must reload from history. A connection may subscribe to several
sessions; subscribing again replaces the cursor.

`queue` is the latest `queue` event payload the gateway saw for this session (amendment A6). It
is absent when the gateway has seen none, which is the normal case for a session nobody has
queued to. An app applies it exactly as it would apply a live `queue` event.

`fixtures/app/session.subscribe.json`

```json
{
  "type": "session.subscribe",
  "id": "21a35285-bdfa-41c4-baed-7d6f847f6a22",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "since_seq": 30
}
```

`session.seen` (A47) says the person has the conversation in front of them: an app sends it when it
opens a conversation in a window or screen the person is using, when such a window comes to the
front, and when `session.updated` marks the session that is already on screen. The gateway clears
`unseen` (4.4), answers `{}`, and, if the mark was set, sends `session.updated` to every socket of
the account. It is idempotent; another account's session is `not_found`.

`fixtures/app/session.seen.json`

```json
{
  "type": "session.seen",
  "id": "5b0e7d63-0a4f-4c1e-9d1b-7f3e8a2c6b44",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0"
}
```

`fixtures/app/session.unsubscribe.json`

```json
{
  "type": "session.unsubscribe",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0"
}
```

`fixtures/app/pong.json`

```json
{
  "type": "pong"
}
```

A reply that carries a queue snapshot, taken from the Codex session while one message was
waiting:

`fixtures/replay/subscribe.codex.reply.json`

```json
{
  "type": "reply",
  "id": "393ab352-34aa-4086-a1c4-23f41dd7b103",
  "ok": true,
  "result": {
    "session": {
      "session_id": "9cabb33b-a6e2-4faf-a905-865f89dcfa8e",
      "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
      "agent": "codex",
      "title": "Typecheck the web app",
      "cwd": "/Users/me/dev/web",
      "git": {
        "branch": "feat/settings-drawer",
        "dirty": true,
        "ahead": 2,
        "behind": 0,
        "worktree": false
      },
      "state": "running",
      "state_detail": null,
      "origin": "remote",
      "control": "remote",
      "model": "gpt-5.4-codex",
      "permission_mode": "on-request",
      "effort": "medium",
      "created_at": 1788944940000,
      "updated_at": 1788945028800,
      "last_seq": 22,
      "archived": false,
      "turn": {
        "turn_id": "192a2a3b-0e93-4d42-bf6c-9d395dda731e",
        "started_at": 1788945001100
      },
      "todos": {
        "total": 3,
        "done": 2
      },
      "usage": null,
      "queued": 1
    },
    "events": [
      {
        "seq": 14,
        "ts": 1788945012800,
        "kind": "status",
        "state": "running"
      },
      {
        "seq": 15,
        "ts": 1788945013000,
        "kind": "tool_call",
        "block_id": "5e808f56-b645-41fa-bba5-9a49992f3d57",
        "tool": "shell",
        "tool_kind": "shell",
        "title": "npm run typecheck",
        "status": "running",
        "input": {
          "command": [
            "npm",
            "run",
            "typecheck"
          ],
          "cwd": "/Users/me/dev/web"
        },
        "started_at": 1788945013000
      },
      {
        "seq": 16,
        "ts": 1788945020800,
        "kind": "tool_call",
        "first_seq": 15,
        "block_id": "5e808f56-b645-41fa-bba5-9a49992f3d57",
        "tool": "shell",
        "tool_kind": "shell",
        "title": "npm run typecheck",
        "status": "failed",
        "input": {
          "command": [
            "npm",
            "run",
            "typecheck"
          ],
          "cwd": "/Users/me/dev/web"
        },
        "output": "src/state/sessions.ts(74,11): error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.\n",
        "summary": "1 error",
        "started_at": 1788945013000,
        "ended_at": 1788945020800,
        "duration_ms": 7800
      },
      {
        "seq": 17,
        "ts": 1788945023800,
        "kind": "user_message",
        "block_id": "faf38b44-3516-44e8-bf66-ddc93f3037f1",
        "text": "While you are in there, also check the settings drawer.",
        "source": "remote"
      },
      {
        "seq": 18,
        "ts": 1788945023900,
        "kind": "notice",
        "level": "info",
        "text": "Message steered into the running turn."
      },
      {
        "seq": 19,
        "ts": 1788945024200,
        "kind": "tool_call",
        "block_id": "643de5d9-01ef-47ef-816e-432020d413a0",
        "tool": "fuzzyFileSearch",
        "tool_kind": "search",
        "title": "settings drawer",
        "status": "succeeded",
        "input": {
          "query": "settings drawer"
        },
        "output": "src/ui/SettingsDrawer.tsx\nsrc/ui/SettingsDrawer.test.tsx\n",
        "summary": "2 files",
        "started_at": 1788945024200,
        "ended_at": 1788945024400,
        "duration_ms": 180
      },
      {
        "seq": 20,
        "ts": 1788945024600,
        "kind": "tool_call",
        "block_id": "ab5cb1a9-2833-4cfa-bec9-592efca4f6c3",
        "tool": "apply_patch",
        "tool_kind": "edit",
        "title": "src/state/sessions.ts",
        "status": "succeeded",
        "input": {
          "path": "src/state/sessions.ts"
        },
        "diff": {
          "path": "src/state/sessions.ts",
          "additions": 3,
          "deletions": 1,
          "patch": "--- a/src/state/sessions.ts\n+++ b/src/state/sessions.ts\n@@ -71,7 +71,9 @@\n-  return byDevice(session.device_id)\n+  if (!session.device_id) return undefined\n+  return byDevice(session.device_id)\n"
        },
        "summary": "+3 -1",
        "started_at": 1788945024600,
        "ended_at": 1788945024800,
        "duration_ms": 120
      },
      {
        "seq": 21,
        "ts": 1788945024800,
        "kind": "todos",
        "items": [
          {
            "id": "p1",
            "text": "Run npm run typecheck",
            "status": "completed"
          },
          {
            "id": "p2",
            "text": "Fix reported type errors",
            "status": "completed"
          },
          {
            "id": "p3",
            "text": "Re-run typecheck",
            "status": "in_progress"
          }
        ]
      },
      {
        "seq": 22,
        "ts": 1788945028800,
        "kind": "queue",
        "pending": [
          {
            "id": "573d2674-debc-4db0-973c-16e8a7c2e9b1",
            "text": "Then run the full test suite.",
            "ts": 1788945028800
          }
        ]
      }
    ],
    "resync": false,
    "queue": {
      "pending": [
        {
          "id": "573d2674-debc-4db0-973c-16e8a7c2e9b1",
          "text": "Then run the full test suite.",
          "ts": 1788945028800
        }
      ]
    }
  }
}
```

### 6.3 App → gateway → device

The gateway forwards these to the owning device and returns the device's reply.

| Type | Fields | Result |
| --- | --- | --- |
| `session.create` | `device_id`, `agent`, `cwd`, `model?`, `permission_mode?`, `effort?`, `speed?`, `worktree?`, `first_message?`, `title?` | `{session}` |
| `session.send` | `session_id`, `text`, `attachments?`, `mode`, `queue_ts?` | `{accepted, queued_id?}` |
| `session.stop` | `session_id` | `{}` |
| `session.approve` | `session_id`, `request_id`, `option_id`, `message?` | `{}` |
| `session.answer` | `session_id`, `request_id`, `answers` | `{}` |
| `session.set` | `session_id`, `model?`, `permission_mode?`, `effort?`, `speed?`, `title?` | `{session}` |
| `session.history` | `session_id`, `before_seq?`, `after_seq?`, `limit?` | `{events, has_more}` |
| `session.block` | `session_id`, `block_id` | `{event}` |
| `session.commands` | `session_id` | `{commands}` — the slash commands the session offers now (A27) |
| `session.command` | `session_id`, `name`, `argument?` | `{}` — runs one; the echo and the outcome arrive as events (A27) |
| `session.queue_remove` | `session_id`, `queued_id` | `{}` |
| `session.resume_set` | `session_id`, `at` | `{session}` — schedules the resume of 7.2 for `at`, or moves the pending one there; `bad_request` unless `at` is at least a minute ahead and within eight days, `conflict` while a turn runs or while the terminal controls the session (A35) |
| `session.resume_cancel` | `session_id` | `{session}` — removes the pending resume; idempotent (A35) |
| `session.takeover` | `session_id` | `{session}` |
| `session.archive` | `session_id`, `archived` | `{session}` |
| `session.delete` | `session_id` | `{}` |
| `device.dirs` | `device_id`, `path?` | `{path, parent, entries, recent}` |
| `device.git` | `device_id`, `path` | `{is_repo, branch?, dirty?, ahead?, behind?}` |
| `device.mkdir` | `device_id`, `path`, `name` | `{path, parent, entries, recent}` — makes the directory `name` inside `path` and replies with the new directory's listing, as `device.dirs` would (A37) |
| `terminal.open` | `device_id`, `cols`, `rows` | `{terminal_id}` — starts the person's login shell in a pseudo-terminal of that size and streams it to the requesting app connection (7.3, A38). `unsupported` when the device offers no terminal; `conflict` when it already runs four |
| `terminal.input` | `device_id`, `terminal_id`, `data` | `{}` — `data` is base64, at most 64 KiB decoded, written to the terminal as typed (A38) |
| `terminal.resize` | `device_id`, `terminal_id`, `cols`, `rows` | `{}` — 1–500 columns, 1–200 rows (A38) |
| `terminal.attach` | `device_id`, `terminal_id` | `{terminal_id, cols, rows, scrollback}` — output goes to this app connection from now on; `scrollback` is base64 of the last 64 KiB the terminal produced (A38). `not_found` once the terminal is gone |
| `terminal.close` | `device_id`, `terminal_id` | `{}` — ends the shell; idempotent (A38) |
| `device.agents` | `device_id` | `{agents}` — the agents as `hello` reports them, with `accounts[].limits` read fresh for this reply (A33) |
| `device.update` | `device_id`, `build` | `{accepted: true, from}` — the device fetches the gateway's wheel, refuses it unless its SHA-256 is `build`, installs it, restarts its service and reconnects with the new `client_build` (A22). `conflict` while a session it drives is running or when it already runs `build`; `unsupported` when the client cannot update itself (installed from source). The gateway sends it on its own account, with `from: "gateway"`, whenever a device's build is not the served one (A36); an app sends it to retry a failed update. |

Every request carries `id`. `session.stop` is idempotent. `session.delete` removes the session from
the device registry and does **not** delete the agent's own transcripts. `device.dirs` returns
directories only, excludes hidden entries, and defaults to the home directory when `path` is
omitted. `device.mkdir` makes exactly one directory, as `mkdir` would: `path` is a directory the
device listed, `name` is one path component — no `/`, no leading `.`, at most 255 bytes — and the
reply is the listing of the directory just made (empty, with the same `recent`), so a picker can
show it as the place it now stands in. `bad_request` for a name or path that breaks the rule,
`not_found` when `path` is not a directory, `conflict` when something already exists under that
name, `forbidden` when the device may not write there (A37). `session.takeover` needs capability
`takeover` and applies to `control: "terminal"`
sessions; on a `shared` session it fails with `conflict`.

#### `session.send` modes

| Mode | Behaviour |
| --- | --- |
| `auto` | Send now if the session is idle. If it is running, steer when the agent has capability `steer`, otherwise queue. |
| `queue` | Always queue. |
| `interrupt` | Stop the current turn, then send. |

The result's `accepted` field reports what actually happened: `sent`, `queued` or `steered`.
`queued_id` is present when the message was queued and is the handle for `session.queue_remove`.

#### Editing a queued message (A43)

A queued message is edited by taking it out and putting it back. The app removes the entry with
`session.queue_remove` — so the device cannot deliver words that are still being changed — and
edits its text in the composer; a `not_found` reply means the device took the message first, and
there is nothing left to edit. The edited words go back as an ordinary `session.send` with
`mode: "queue"` and `queue_ts`, the `ts` the entry had in the snapshot:

- When the message is queued, the device holds it under that `ts`, in front of every entry with a
  later one, so it waits in the place it left however the queue moved meanwhile. Without
  `queue_ts` a queued message joins the end under the current time, or one millisecond past the
  last entry's `ts` when that is later, so no two entries share a `ts` and an edit has exactly one
  place to go back to. A message the device puts back itself — an injection the CLI absorbed, one
  it could not inject — goes back under its own `ts` the same way. The queue is therefore always
  in `ts` order, which is the order it is delivered in.
- `queue_ts` never decides whether a message queues; `mode` does, as above. An idle session
  delivers a `mode: "queue"` message at once, which is right for an edit that outlived the turn
  it was waiting for.
- A `queue_ts` that is not a non-negative integer is `bad_request`.

An entry whose snapshot carries `attachments` is not edited: its files are on the device, and no
frame brings them back to an app. Apps offer Remove for it and nothing else.

#### Rules the device enforces

- `session.send` to a session with `control: "terminal"` is refused with `conflict`
  ("controlled by terminal; take over first").
- `session.send` to a session with `control: "none"` makes the device resume the session first
  (Claude `resume`, Codex `thread/resume`) and then send.
- A `session.send` or `session.command` a person sends to a session with a pending resume (7.2)
  cancels the resume: the person got there first (A35).
- `session.set` with `effort` on Claude may need the SDK connection restarted before the next turn.
  The device replies immediately with the updated `Session` and applies the change lazily. The same
  applies to `permission_mode` when the agent cannot change it live.
- On a `shared` Claude session, `session.set` for a key in `shared_settings_keys` and
  `session.command` are typed into the CLI's terminal (A40). The device types only into an idle
  terminal — no turn running, no dialog open, nobody typing there — and otherwise answers
  `conflict` ("the terminal is busy; try again in a moment") without queueing anything. It replies
  to `session.set` only once the transcript confirms the change, so the `Session` it returns is what
  the terminal now runs; the change is for this session only, and no settings file of the person's
  is written. A key outside `shared_settings_keys` is refused with `unsupported` as before.

#### Slash commands (A27)

A terminal offers its agent's commands the moment `/` is typed; an app offers the same list the same
way, for the agents that can take them. `AgentInfo.capabilities` carries `commands` for an agent
whose sessions can list and run commands remotely — Codex, Grok Build and pi today — and never for
Claude, whose channel carries user text and nothing else. An app draws nothing for an agent without
it: `/` is then ordinary text.

`session.commands {session_id}` returns `{commands: Command[]}` (4.11): what the session's agent
offers **now**. For a session with a live process the device asks the agent — Grok Build advertises
its list over ACP when a session opens, pi answers `get_commands`; Codex has no such call, so the
device ships a fixed table. For a session with no live process (`control: "none"`) the device answers
from what it knows without starting one: Codex's table, pi's prompt templates and skills on disk,
the list Grok last advertised on this device. That may be `[]`, and an app that receives `[]` draws
nothing until it asks again. Apps ask when a conversation opens and again when `/` is typed if the
last answer is older than a minute or was empty. `unsupported` for an agent without the capability.

`session.command {session_id, name, argument?}` runs one. The device echoes it as a `user_message`
under the request's `id` with text `/name argument` and `source: "remote"` — A12 applies, the app
has already drawn the row — and reports the outcome as ordinary events. For Grok Build and pi the
command is handed to the agent as the text of a turn, which is how their agents interpret it: Grok
runs its shell-side commands locally and answers in `assistant_text` at zero tokens, sometimes with
no output at all; pi expands prompt templates, skill commands and extension commands before the
turn. For Codex the device calls the method the command stands for and reports a `notice` for a
state change (`compact`), the turn's own blocks (`review`, `init`), or a `tool_call` block for
information a terminal would have printed: `tool_kind: "other"`, `tool` and `title` `/name`,
`status: "succeeded"`, `output` the text — `fixtures/events/tool_call.command.json`. The result is `{}`;
the effects are in the stream. Refusals: `not_found` for a name the session does not offer,
`conflict` while a turn is running ("wait for the turn to finish") or on a `control: "terminal"`
session (as `session.send`), `unsupported` for an agent without the capability. A `none` session is
resumed first, exactly as `session.send` does. On a `shared` session the request works exactly where
`session.send` does — Codex on the daemon, pi through its extension.

Commands are not settings and not lifecycle. `/model`, `/permissions`, `/fast`, `/thinking` and
`/name` are `session.set`; `/new`, `/archive`, `/delete` and `/resume` are `session.create`,
`session.archive` and `session.delete`. A device never lists them as commands, so an app never draws
two controls for one thing. Terminal ergonomics — `/vim`, `/theme`, `/keymap`, `/copy`, `/hotkeys` —
are never listed either: they change a terminal the app cannot see.

#### Requests on a `shared` session

| Request | Behaviour |
| --- | --- |
| `session.send` | Accepted whatever `mode` says. When the CLI's own turn is idle — whatever its subagents are doing (A51) — the device injects at once, replies `accepted: "sent"` and emits `user_message {delivery: "delivered"}` — at once when the CLI holds no earlier message, otherwise when the terminal shows the message's own row, so the bubbles keep the order they were sent in (A51). While a turn runs, `mode: "auto"` is steered when the session's CLI runs the device's turn hooks and was started with `PostToolBatch` among them, which the terminal proxy gives a CLI only when its version can load it: the device replies `accepted: "steered"`, hands the message to the model at the turn's next batch of tool calls — or, when the turn has none left, writes it into the channel as the turn ends, so that it starts the next turn — and emits the block when it does (A14, A51). Steering is thus decided per session, not by `shared_steer` (4.2), which the device reports by the version of the `claude` it detects; `accepted` says which way a message went. `mode: "queue"` and `mode: "interrupt"`, and `mode: "auto"` on a session whose CLI started without that hook, are held locally: the device replies `accepted: "queued"` with a `queued_id` and emits a `queue` event listing it — no `user_message` yet (A19) — then injects the oldest held message as the turn ends, one per turn, and emits the block with `delivery: "delivered"`. `session.queue_remove` works on held items. A message the CLI absorbs, removes from its queue without running it, or goes past without its own row (a later turn hook ran, and neither its row nor the CLI's queue showed it; time alone never counts) goes in again — an `auto` one at the running turn's next batch of tool calls when the session steers, any other at the next turn's end — at most three times, and is then reported with an `error` under its bubble's `block_id` (A51). One written at a turn's end and absorbed by that turn going on is sent again outside those three: at the turn's next batch of tool calls whatever its `mode`, uncounted, when the session steers, and when it cannot, at the next turn's end, at most ten times before the same `error`. One the person pulls back into the terminal's input box is theirs and is not sent again. Attachments are delivered as files the words name (4.8, A49), held, steered or not, when the agent reports `shared_attachments: true`, and are refused with `unsupported` ("attachments cannot be delivered to a terminal session") when it does not. |
| `session.approve` | Relays the option the block offered. An `option_id` the block did not offer, `elsewhere` included, is `bad_request`. Replying to a request the terminal already answered is a no-op returning `{}`. |
| `session.answer` | Supported for a `question` block the device raised through its `PermissionRequest` hook (A20): the answer goes to the CLI as the tool's own answers and the block resolves with `by: "remote"`. Answering a question the terminal already answered is a no-op returning `{}`. A question the device did not raise has no block to answer. |
| `session.stop` | `unsupported` unless the agent lists capability `interrupt` **and** reports `shared_interrupt: true`. Message: "stop it in the terminal". Claude reports it since A42: the device types Escape into the CLI's terminal while a turn runs; with an approval or a question on screen it answers `conflict` ("answer the prompt first"), and with no turn running it returns `{}` and types nothing. |
| `session.set` | `unsupported` for `model`, `permission_mode`, `effort` and `speed` ("change it in the terminal"). `title` works. |
| `session.takeover` | `conflict` ("already attached"). |

The table above describes what an attachment can do at its narrowest, which is what a Claude channel
can do. An agent whose attachment can do more says so in its `AgentInfo` (4.2), and the rows below
replace the ones they name for a `shared` Codex session on the daemon.

| Request | Behaviour |
| --- | --- |
| `session.send` | Every `mode` behaves as it does on a `remote` session. Idle: the device starts the turn and replies `accepted: "sent"`. Running under `mode: "auto"`: the device steers the running turn and replies `accepted: "steered"`, because Codex has capability `steer`. Running under `mode: "queue"`: the device holds the message, replies `accepted: "queued"` with a `queued_id`, emits `user_message {delivery: "pending"}` and a `queue` event, and starts the turn once the running one completes. `mode: "interrupt"` interrupts and then sends. Attachments are delivered, because `shared_attachments` is true. |
| `session.stop` | Interrupts the running turn, because `shared_interrupt` is true. |
| `session.set` | `model`, `permission_mode`, `effort` and `speed` reach the daemon and change the thread for everyone attached to it, because `shared_settings` is true; `title` stays device-local as always. |
| `session.answer` | Supported. The daemon relays questions asked of the thread and accepts the answer from whichever client replies. |

`session.takeover` is still `conflict`, and every rule of 5.1 through 5.13 applies to a shared Codex
session exactly as it does to a remote one.

#### How the device attaches to the Codex daemon

Normative for the device and invisible to apps.

- The device opens one connection to the shared daemon at startup and identifies itself by name, so
  the daemon and its other clients can tell our traffic apart.
- It subscribes to a thread lazily, when an app subscribes to the session or the daemon reports the
  thread loaded, and backfills the thread's history from the daemon rather than from the rollout
  file. Threads the daemon marks ephemeral are never surfaced as sessions.
- It reconnects with backoff, and on every reconnect re-subscribes to each thread it was following
  and backfills from the last item it saw, so no event is lost across a daemon restart.
- A Codex thread has no rollout until its first turn starts, and resuming a thread without one
  fails. The device therefore cannot subscribe to a thread the terminal has only just created. It
  still reports the session `shared`, `turn/start` works on it, and the device subscribes the moment
  the first turn creates the rollout. The same gap runs the other way: a thread created from an app
  can be reopened in a terminal with `codex resume <id>` only after its first turn.
- With no daemon socket present the device falls back to running one Codex app-server per session.
  It keeps reporting `attach: "daemon"` with `attach_ready: false`, terminal Codex sessions stay
  `control: "terminal"`, and the apps' existing hint for an agent with `attach` set already says the
  right thing.

`fixtures/app/session.create.json`

```json
{
  "type": "session.create",
  "id": "327b6ada-c1fd-4888-a6ba-97507efa5579",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "agent": "claude",
  "cwd": "/Users/me/dev/gateway",
  "model": "claude-sonnet-4-5",
  "permission_mode": "default",
  "effort": "medium",
  "worktree": false,
  "first_message": "Fix the flaky refresh test in tests/test_auth.py.",
  "title": "Fix flaky auth test"
}
```

`fixtures/app/reply.session.create.json`

```json
{
  "type": "reply",
  "id": "327b6ada-c1fd-4888-a6ba-97507efa5579",
  "ok": true,
  "result": {
    "session": {
      "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
      "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
      "agent": "claude",
      "title": "Fix flaky auth test",
      "cwd": "/Users/me/dev/gateway",
      "git": {
        "branch": "main",
        "dirty": false,
        "ahead": 0,
        "behind": 0,
        "worktree": false
      },
      "state": "starting",
      "state_detail": null,
      "origin": "remote",
      "control": "remote",
      "model": "claude-sonnet-4-5",
      "permission_mode": "default",
      "effort": "medium",
      "created_at": 1788942600000,
      "updated_at": 1788944400000,
      "last_seq": 0,
      "archived": false,
      "turn": null,
      "todos": null,
      "usage": null,
      "queued": 0
    }
  }
}
```

`fixtures/app/session.send.json`

```json
{
  "type": "session.send",
  "id": "d7c20931-eada-4d1f-9df5-e8426a3d8dcd",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "text": "Have a look at the screenshot and tell me what broke the layout.",
  "attachments": [
    {
      "name": "settings-drawer.png",
      "mime": "image/png",
      "data_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    }
  ],
  "mode": "auto"
}
```

The first queued message of `fixtures/events/queue.json`, edited and sent back to its place (A43):
`fixtures/app/session.send.requeue.json`

```json
{
  "type": "session.send",
  "id": "094980b3-a803-4e2b-b6cf-b97e665d8b27",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "text": "Then run the full test suite, and the linters after it.",
  "mode": "queue",
  "queue_ts": 1788945028800
}
```

`fixtures/app/reply.session.send.json`

```json
{
  "type": "reply",
  "id": "d7c20931-eada-4d1f-9df5-e8426a3d8dcd",
  "ok": true,
  "result": {
    "accepted": "queued",
    "queued_id": "573d2674-debc-4db0-973c-16e8a7c2e9b1"
  }
}
```

`fixtures/app/session.stop.json`

```json
{
  "type": "session.stop",
  "id": "5e3657e8-af32-4c9a-837c-5dc77aa0edcb",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0"
}
```

`fixtures/app/reply.empty.json`

```json
{
  "type": "reply",
  "id": "5e3657e8-af32-4c9a-837c-5dc77aa0edcb",
  "ok": true,
  "result": {}
}
```

`fixtures/app/session.approve.json`

```json
{
  "type": "session.approve",
  "id": "b68baab6-7341-4b82-8f7b-d94dd6a7958b",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "request_id": "0f99737c-e8c1-4242-ac23-42f50fe1654e",
  "option_id": "allow"
}
```

`fixtures/app/session.answer.json`

```json
{
  "type": "session.answer",
  "id": "c5d9573b-2521-418d-8551-d39111c7cb34",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "request_id": "dbe1ce62-ee02-41b3-a229-50b5b4689610",
  "answers": {
    "q1": [
      "clamp"
    ]
  }
}
```

`fixtures/app/session.set.json`

```json
{
  "type": "session.set",
  "id": "ed9c12f6-754a-4b65-b843-20f35de3827b",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "model": "claude-opus-4-6",
  "permission_mode": "acceptEdits",
  "effort": "high",
  "title": "Fix flaky auth refresh test"
}
```

`fixtures/app/session.history.json`

```json
{
  "type": "session.history",
  "id": "bc51b5b2-2149-4418-966c-5884477c745d",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "before_seq": 41,
  "limit": 200
}
```

`fixtures/app/session.block.json`

```json
{
  "type": "session.block",
  "id": "db04f345-eeba-4a1c-a904-1e5b24096685",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "block_id": "d026b2fb-c6fd-4a29-bbdc-34b98ee362bb"
}
```

`fixtures/app/session.queue_remove.json`

```json
{
  "type": "session.queue_remove",
  "id": "763f021c-1e47-40f9-8c1b-97231505c649",
  "session_id": "9cabb33b-a6e2-4faf-a905-865f89dcfa8e",
  "queued_id": "573d2674-debc-4db0-973c-16e8a7c2e9b1"
}
```

`fixtures/app/session.takeover.json`

```json
{
  "type": "session.takeover",
  "id": "c111366e-43d9-4a94-b842-474932d7ff9e",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0"
}
```

`session.archive` with `archived: true` **closes** a session the device is driving (amendment A39):
the device interrupts the turn if one is running, ends what it holds for the agent — Claude's CLI
process, pi's process, the Codex thread (archived in Codex as well), the Grok leader session — and
only then records the choice, so the reply and the publish carry `archived: true`, `control:
"none"` and `state: "stopped"` together, and nothing of the session runs on the machine afterwards.
A message the agent still sends while it is being closed does not revive it. Messages held for the
session (5.12) are discarded, never sent: the device publishes an empty `queue` with the close. The
device clears `archived` on its own the moment the session comes back to life — a turn starts in it,
from an app or from a terminal, or a terminal attaches to it again — and publishes the session with
`archived: false` (amendment A15). An archived session therefore never runs, and apps need no rule
of their own for it: the row moves out of the Archive when the device's `session.updated` arrives.

`fixtures/app/session.archive.json`

```json
{
  "type": "session.archive",
  "id": "9b774ca5-6325-4bd0-8545-6e75c45b7ad9",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "archived": true
}
```

`fixtures/app/session.delete.json`

```json
{
  "type": "session.delete",
  "id": "2d74f3da-cd06-4d68-ba36-10c1986269e4",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0"
}
```

`fixtures/app/device.dirs.json`

```json
{
  "type": "device.dirs",
  "id": "9ed320c0-44f6-4a52-a1ac-756b4dbb0a7c",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "path": "/Users/me/dev"
}
```

`fixtures/app/reply.device.dirs.json`

```json
{
  "type": "reply",
  "id": "9ed320c0-44f6-4a52-a1ac-756b4dbb0a7c",
  "ok": true,
  "result": {
    "path": "/Users/me/dev",
    "parent": "/Users/me",
    "entries": [
      {
        "name": "gateway",
        "path": "/Users/me/dev/gateway",
        "is_git": true
      },
      {
        "name": "web",
        "path": "/Users/me/dev/web",
        "is_git": true
      },
      {
        "name": "scratch",
        "path": "/Users/me/dev/scratch",
        "is_git": false
      }
    ],
    "recent": [
      {
        "path": "/Users/me/dev/gateway",
        "last_used": 1788943800000
      },
      {
        "path": "/Users/me/dev/web",
        "last_used": 1788937200000
      }
    ]
  }
}
```

`fixtures/app/device.mkdir.json`

```json
{
  "type": "device.mkdir",
  "id": "3b9c6f1e-7a2d-4e58-9c1b-8f6a2d4e7b31",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "path": "/Users/me/dev",
  "name": "new-project"
}
```

`fixtures/app/reply.device.mkdir.json`

```json
{
  "type": "reply",
  "id": "3b9c6f1e-7a2d-4e58-9c1b-8f6a2d4e7b31",
  "ok": true,
  "result": {
    "path": "/Users/me/dev/new-project",
    "parent": "/Users/me/dev",
    "entries": [],
    "recent": [
      {
        "path": "/Users/me/dev/gateway",
        "last_used": 1788943800000
      }
    ]
  }
}
```

`fixtures/app/terminal.open.json`

```json
{
  "type": "terminal.open",
  "id": "7c1e9a52-3f4b-4d86-b2e7-9a0c5d1f6e83",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "cols": 80,
  "rows": 24
}
```

`fixtures/app/reply.terminal.open.json`

```json
{
  "type": "reply",
  "id": "7c1e9a52-3f4b-4d86-b2e7-9a0c5d1f6e83",
  "ok": true,
  "result": {
    "terminal_id": "2f8d4b6a-1c3e-4a75-9b0d-6e2f8c4a1d57"
  }
}
```

`fixtures/app/terminal.attach.json` and `fixtures/app/reply.terminal.attach.json` show the
reconnect: the reply carries the size the terminal has now and `scrollback`, the last 64 KiB of
its output, base64. `fixtures/app/terminal.input.json`, `terminal.resize.json` and
`terminal.close.json` are the other three requests.

`fixtures/app/device.git.json`

```json
{
  "type": "device.git",
  "id": "74d5f4eb-37e6-41b7-88ab-30cbf8b142a1",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "path": "/Users/me/dev/gateway"
}
```

`fixtures/app/reply.device.git.json`

```json
{
  "type": "reply",
  "id": "74d5f4eb-37e6-41b7-88ab-30cbf8b142a1",
  "ok": true,
  "result": {
    "is_repo": true,
    "branch": "main",
    "dirty": false,
    "ahead": 0,
    "behind": 3
  }
}
```

`fixtures/app/device.agents.json`

```json
{
  "type": "device.agents",
  "id": "d0da1eee-6cd3-41e5-9d4e-166838a07374",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712"
}
```

### 6.4 `session.history` and what it returns

`session.history` returns, in ascending `seq` order:

- the **latest** event for each block: `user_message`, `assistant_text` with `done: true`, `thinking`
  with `done: true`, `tool_call`, `approval`, `question`;
- plus `turn_started`, `turn_completed`, `notice`, `error` and `resume` (A35: what the device did
  about a paused session is part of the record, so a reload and the gateway's backfill keep it);
- plus the latest `todos` snapshot.

It never returns streaming deltas, `status`, `meta` or `queue` events: those describe current state
and are carried by the `Session` object.

The ordering of the reply itself is unchanged: ascending `seq`, one event per block. Each block event
also carries `first_seq`, so an app that pages backwards can place a block at its original position
rather than where its latest event happens to land.

Two cursors select the page, and they are mutually exclusive:

| Cursor | Direction | Returns |
| --- | --- | --- |
| `before_seq` | Backwards from the newest event | Events ending just before `before_seq` |
| `after_seq` | Forwards | Events with `seq` greater than `after_seq` (amendment A9) |
| neither | Backwards from the newest event | The most recent page |

Both forms return at most `limit` events, ascending by `seq`, with `has_more`. Apps page backwards
with `before_seq`; `after_seq` exists for the gateway backfill described in 7.1.

`fixtures/history/page.json` is a worked page built from `fixtures/timelines/claude.json`, and
`scripts/validate_fixtures.py` enforces every rule above on it. Contrast it with
`fixtures/replay/subscribe.reply.json`, which is a replay-buffer answer and therefore does contain
deltas and `status` events.

---

## 7. Device ↔ gateway frames

Schema: `schema/device_frames.json`. Endpoint `WS /ws/device`, header
`Authorization: Bearer <device_token>`.

The device sends `hello` first and the gateway answers `hello_ack`. A second connection for the same
device replaces the first; the old socket is closed with code 4001.

`hello.name` is what the device calls itself, its configured name or its hostname. The gateway
accepts it and does not apply it (A50): a device keeps the `name` it enrolled with until
`PATCH /api/devices/{device_id}` renames it, so neither a reconnect nor a gateway restart undoes a
rename. The rest of what `hello` says about the machine is stored on every connection.

| Direction | Type | Payload |
| --- | --- | --- |
| device → gateway | `hello` | `protocol`, `client_version`, `client_build?`, `name`, `platform`, `hostname`, `arch`, `agents`, `sessions`, `terminal?` (A38) |
| gateway → device | `hello_ack` | `device_id`, `server_time`, `config: {delta_flush_ms, max_event_bytes}` |
| device → gateway | `session.updated` | `session` |
| device → gateway | `session.removed` | `session_id` |
| device → gateway | `session.event` | `session_id`, `event` |
| device → gateway | `agents.updated` | `agents` |
| device → gateway | `update.failed` | `message` — the update that was asked for did not complete; the old client is still running (A22, A36) |
| device → gateway | `terminal.output` | `terminal_id`, `to`, `seq`, `data` — bytes the shell produced, base64, at most 16 KiB decoded, for the one app connection `to` names (7.3, A38) |
| device → gateway | `terminal.exited` | `terminal_id`, `to`, `code` — the shell ended; the terminal is gone (A38) |
| device → gateway | `pong` | – |
| device → gateway | `reply` | `id`, `from`, `ok`, `result` or `error` |
| gateway → device | forwarded request | any type from 6.3, plus `from` and `device_id` |
| gateway → device | `ping` | – every 25 s |
| gateway → device | `preferences` | `preferences` — after `hello_ack`, and whenever the account's preferences change (A35) |
| gateway → device | `terminal.detach` | `terminal_id` — a gateway-originated request (`from: "gateway"`, `device_id`) when the app connection holding a terminal is gone; the device stops streaming and keeps the shell for ten minutes (7.3, A38) |

`fixtures/device/hello.json` (abridged)

```json
{
  "type": "hello",
  "protocol": 1,
  "client_version": "0.1.0",
  "name": "mac-studio-office",
  "platform": "macos",
  "hostname": "mac-studio.local",
  "arch": "arm64",
  "agents": [
    {
      "agent": "claude",
      "available": true,
      "version": "2.1.266",
      "path": "/Users/me/.local/bin/claude",
      "models": [
        {
          "id": "claude-opus-4-6",
          "label": "Opus 4.6"
        }
      ],
      "default_model": "claude-sonnet-4-5",
      "permission_modes": [
        {
          "id": "default",
          "label": "Ask before edits"
        }
      ],
      "default_permission_mode": "default",
      "efforts": [
        {
          "id": "low",
          "label": "Low"
        }
      ],
      "default_effort": "medium",
      "capabilities": [
        "worktree",
        "takeover",
        "interrupt",
        "queue",
        "attachments",
        "effort",
        "history"
      ]
    }
  ],
  "sessions": [
    {
      "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
      "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
      "agent": "claude",
      "title": "Fix flaky auth refresh test",
      "cwd": "/Users/me/dev/gateway",
      "git": {
        "branch": "main",
        "dirty": false,
        "ahead": 0,
        "behind": 0,
        "worktree": false
      },
      "state": "idle",
      "state_detail": null,
      "origin": "remote",
      "control": "remote",
      "model": "claude-sonnet-4-5",
      "permission_mode": "default",
      "effort": "medium",
      "created_at": 1788942600000,
      "updated_at": 1788944461100,
      "last_seq": 41,
      "archived": false,
      "turn": null,
      "todos": {
        "total": 4,
        "done": 4
      },
      "usage": {
        "input_tokens": 48120,
        "output_tokens": 6210,
        "total_tokens": 54330,
        "context_used": 61000,
        "context_window": 200000,
        "cost_usd": 0.42
      },
      "queued": 0
    }
  ]
}
```

The agent, session and option arrays are shortened here; the fixture holds the full frame.

`fixtures/device/hello_ack.json`

```json
{
  "type": "hello_ack",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712",
  "server_time": 1788944400000,
  "config": {
    "delta_flush_ms": 80,
    "max_event_bytes": 65536
  }
}
```

`fixtures/device/session.removed.json`

```json
{
  "type": "session.removed",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0"
}
```

`fixtures/device/agents.updated.json` (abridged)

```json
{
  "type": "agents.updated",
  "agents": [
    {
      "agent": "claude",
      "available": true,
      "version": "2.1.266",
      "path": "/Users/me/.local/bin/claude",
      "models": [
        {
          "id": "claude-opus-4-6",
          "label": "Opus 4.6"
        }
      ],
      "default_model": "claude-sonnet-4-5",
      "permission_modes": [
        {
          "id": "default",
          "label": "Ask before edits"
        }
      ],
      "default_permission_mode": "default",
      "efforts": [
        {
          "id": "low",
          "label": "Low"
        }
      ],
      "default_effort": "medium",
      "capabilities": [
        "worktree",
        "takeover",
        "interrupt",
        "queue",
        "attachments",
        "effort",
        "history"
      ]
    }
  ]
}
```

`fixtures/device/pong.json`

```json
{
  "type": "pong"
}
```

`fixtures/device/reply.json`

```json
{
  "type": "reply",
  "id": "d7c20931-eada-4d1f-9df5-e8426a3d8dcd",
  "from": "app-7f3c2a19",
  "ok": true,
  "result": {
    "accepted": "sent"
  }
}
```

`fixtures/device/reply.error.json`

```json
{
  "type": "reply",
  "id": "c111366e-43d9-4a94-b842-474932d7ff9e",
  "from": "app-7f3c2a19",
  "ok": false,
  "error": {
    "code": "agent_unavailable",
    "message": "claude is not installed on this device"
  }
}
```

`fixtures/device/forwarded/session.send.json` (abridged)

```json
{
  "type": "session.send",
  "id": "d7c20931-eada-4d1f-9df5-e8426a3d8dcd",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "text": "Have a look at the screenshot and tell me what broke the layout.",
  "mode": "auto",
  "from": "app-7f3c2a19",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712"
}
```

Note the two fields the gateway added: `from` identifies the app connection and must be echoed in the
reply, and `device_id` was injected because the request was addressed by `session_id`.
`fixtures/device/forwarded/` holds all seventeen forwarded requests, plus the gateway's backfill
variant of `session.history` described in 7.1.

The gateway keeps a replay buffer of the last 2 000 events or 4 MiB per session, whichever is
smaller, and persists the latest `Session` summary per session in SQLite so lists render while a
device is offline.

### 7.1 Backfill after a device link outage

A device keeps working while its socket is down, so events produced during an outage never reach the
gateway. Amendment A9 closes that hole. When a device reconnects, the gateway compares each
session's `last_seq` in the device `hello` with the tail of its own replay buffer. For every session
whose device `last_seq` is ahead, the gateway asks the device for the missing range:

`fixtures/device/forwarded/session.history.after.json`

```json
{
  "type": "session.history",
  "id": "586c5356-ccb2-438c-aca6-1fff63807f39",
  "session_id": "ad2c9abb-4a1e-470a-835c-228778fc17f0",
  "after_seq": 22,
  "limit": 1000,
  "from": "gateway",
  "device_id": "c5efb1ec-2912-4619-90f7-93b5172fd712"
}
```

The gateway appends the returned events to the replay buffer and fans them out to that session's
current subscribers as ordinary `session.event` frames. This is the one request the gateway issues on
its own behalf rather than forwarding, so `from` names the gateway instead of an app connection, and
the gateway consumes the reply itself.

Apps need no special handling. The backfilled events arrive as normal session events and are applied
by `block_id` like any other.

### 7.2 Resuming after a usage limit

Claude Code and Codex stop a turn when the vendor's usage window is used up — the five-hour window,
or the weekly one — and say when it resets. The device tells such an end apart from a completed
turn whatever the account prefers (`turn_completed` with `limit`, 5.9), and when the account's
`resume_after_limit` (3.2) is on, resumes the session itself (amendment A35):

- **Scheduling.** At a limit stop on a session whose `control` is `remote` or `shared`, the device
  sets `Session.resume` to one minute after `limit.resets_at` — or, when the vendor named no time,
  to now plus the window's length, five hours when the window is unknown, with `estimated: true` —
  publishes `resume {status: "scheduled"}`, and keeps the record across its own restarts. A
  `terminal` session gets none: the device has no way in. A person can set or move one with
  `session.resume_set` and remove it with `session.resume_cancel`.
- **Firing.** The device looks at pending resumes at least every 30 seconds, so a machine that slept
  through the time fires on waking. When `at` has passed: a session that is `running` was taken
  further by someone else, and the resume is `cancelled`; a session that was `shared` when the
  resume was scheduled and whose CLI is gone (`control` now `none`) is `dropped`, because the
  person who closed the terminal has said they are done with it; any other session gets the prompt
  as a `session.send` would deliver it — a `none` session the device runs is resumed first, as 6.3
  says — with `resume {status: "fired"}`, the `user_message {source: "resume"}` and a
  `turn_started {trigger: "resume"}`.
- **The prompt** is one fixed sentence, the same on every device and never edited per session:
  `The usage limit has reset. Continue where you left off, and let any subagents you started continue their work.`
- **Again.** When the resumed turn ends with `limit` once more, the device reschedules from the new
  `resets_at` (`rescheduled`, `attempts` + 1); after the third such turn it drops the resume.
- **Cancelling.** A `session.send` or `session.command` from a person on a session with a pending
  resume cancels it; so does turning `resume_after_limit` off, which cancels every pending resume on
  every device of the account, each with `resume {status: "cancelled"}`. Turning it on schedules
  nothing for stops that already happened.
- **Grok Build and pi** report no limit stop the device can read; their turns never carry `limit`
  and no resume is scheduled for them. The device keeps one place where an agent's limit signal is
  read, so an agent that gains one joins without a change to the wire.

The gateway stores the preference per account (3.2), sends it to each device after `hello_ack` and
on every change, and pushes (3.7) `limit_reached` on `resume {status: "scheduled"}`, `resumed` on
`fired` and `resume_dropped` on `dropped` to the account's registrations.

### 7.3 Terminals

A person taps a device and gets a shell on it (amendment A38). It is not SSH: the device dials out
as it always has, nothing on the host listens, no key is exchanged. The device starts the person's
login shell — `$SHELL`, else `/bin/sh` — in a pseudo-terminal with `TERM=xterm-256color`, in the
home directory, as the user the client runs as, and streams its bytes through the gateway to the
one app connection that asked. The gateway relays bytes and never reads them.

- **Opening.** `terminal.open {cols, rows}` (6.3) replies `{terminal_id}`; from then on every
  `terminal.output` for that terminal names the requesting connection in `to`, and the gateway
  delivers it to that connection alone — a terminal is one person's view, never broadcast to the
  account's other sockets. A device runs at most four terminals; a fifth `open` is `conflict`. A
  device that offers no terminal — the capability is off in its configuration, or the client is
  older than A38 — says `terminal: false` or nothing in `hello` and answers `unsupported`.
- **Output.** The device coalesces what the shell writes for about 16 ms and sends it in
  `terminal.output` frames of at most 16 KiB (decoded), each with a `seq` that starts at 1 and
  rises by one per frame per terminal, so an app can tell a gap from a pause. It keeps the last
  64 KiB of every terminal's output as scrollback for `attach`.
- **Input and size.** `terminal.input {data}` writes bytes as typed — key sequences included, the
  app's terminal emulator produces them. `terminal.resize {cols, rows}` follows the app's view.
- **Detaching and attaching.** When the app connection that holds a terminal closes, the gateway
  sends the device `terminal.detach {terminal_id}` on its own account; the device stops streaming,
  keeps the shell running and its scrollback for **ten minutes**, then ends it as `terminal.close`
  would. Within that time any app connection of the same account may `terminal.attach
  {terminal_id}`: the reply carries the terminal's current `cols`, `rows` and `scrollback`, output
  resumes to the attaching connection, and `seq` continues where it left off. A second `attach`
  while another connection holds the terminal moves it: the previous holder gets nothing more.
- **Ending.** `terminal.close` ends the shell (SIGHUP, then SIGKILL after a grace); the shell
  ending on its own — `exit`, or the ten minutes — produces `terminal.exited {code}` to the
  holder, if any, and frees the id. Every terminal ends when the device process does.
- **What travels.** Bytes, both ways, base64 in JSON. Nothing about them is stored by the gateway
  or the device beyond the scrollback ring; nothing is logged but the fact that a terminal opened
  and closed.

`fixtures/device/terminal.output.json`, `fixtures/device/terminal.exited.json` and
`fixtures/device/forwarded/terminal.detach.json` are the three frames.

---

## 8. Semantics every UI must honour

1. **State comes from events, never from silence.** Only `status`, `turn_started` and
   `turn_completed` change a session's state. Never infer that a turn finished because nothing
   arrived.
2. **Apply by `block_id`, order by `first_seq ?? seq`.** Streaming text goes into the block named
   by `block_id`. Drop any event whose `seq` is less than or equal to the last applied `seq` for that
   session: it is a late or duplicate frame. Sort blocks by `first_seq` when it is present and by
   `seq` otherwise, so a block that finishes late stays where it started.
3. **Sending during a running turn.** With `mode: "auto"` the message is queued, or steered for
   agents with capability `steer` and, on a `shared` session, for agents reporting
   `shared_steer: true` (A51). The UI shows "working · your message will be queued" and offers
   "Interrupt & send". A steered message stays an optimistic row until the device's
   `user_message` arrives, which happens when the agent takes it, not when it was sent (A14).
4. **Approval and question cards go inactive** as soon as `status` becomes `resolved` or `expired`.
   Only the server-supplied option ids are ever sent back.
5. **Terminal control disables the composer**, which reads "Controlled by the terminal · Take
   over". Decide this from `control`, never from `state` alone: a mirrored session reports
   `running` while its terminal-driven turn works, and `readonly` only between turns.
6. **Reconnect order.** Render the cached list, open the socket and read `hello`, then
   `session.subscribe` with `since_seq`, then `session.history` if `resync` is true or there is no
   cache.
7. **Never auto-resend.** After an uncertain `session.send` an app shows "delivery unconfirmed" and
   lets the user retry, reusing the same `id`. Device-side idempotency (2.4) makes that retry safe.
8. **Apply snapshots wherever they arrive.** `todos` and `queue` snapshots must be applied when
   they turn up in replayed events and in history, not only in the live stream, along with the
   `queue` field of a `session.subscribe` reply. An app that only handles them live shows a stale
   todo chip and a stale queue count after every reconnect.
9. **Render unknown agents generically**, using the agent id as the label, and ignore unknown fields
   and unknown event kinds rather than failing.
10. **Treat `shared` like `remote`.** A session with `control: "shared"` gets the same composer,
    approvals, queue and user-message rows as a remote session. Never offer "Take over" on it, and
    show Stop only when the agent has capability `interrupt` and reports `shared_interrupt: true`.
    Render `delivery: "absorbed"` as a quiet "will be re-sent" chip on the bubble; a held message
    is a queue entry until it lands (A19). Enable the model, permission-mode and effort
    pickers on a `shared` session when the agent reports `shared_settings: true`, and the
    attachment button when it reports `shared_attachments: true`. When both are true the composer
    status line says only that the session is attached to the terminal, with nothing disabled.
11. **Hint how to attach.** On a `terminal` session whose agent has `attach` set, the take-over bar
    may carry one line about the attachment: with `attach_ready: false`, that the terminal must be
    started through the device's shim (Claude) or with the shared daemon running (Codex); with
    `attach_ready: true`, that the running CLI was started without the attachment.
12. **Label `shared` as "terminal · attached"** in the status line, and give it the same dot colour
    as `remote` in the session list.
13. **Render whatever `options` an approval carries**, in the order they arrive, and read the
    resolution from `decision`. A block resolved with `option_id: "elsewhere"` and `by: "terminal"`
    was answered on the other side of a shared session and reads as "answered in the terminal"; an
    app never offers `elsewhere` as a button and never sends it back.
14. **Sign-in is one account on one gateway.** An app signs in with a username and a password,
    keeps its stored credential per gateway and account, offers "Create an account" only when
    `GET /api/health` reports `registration_open`, shows the signed-in account and its `role`, and
    shows the accounts screen of 3.9 only to `admin` (A24).
15. **Polished dictation is a draft, never a send.** When the user has turned dictation polish on
    (A29), the words the recogniser produced land in the composer the instant dictation ends; the
    app then asks `POST /api/polish` with the dictated span, the chosen model and strength, and the
    recent conversation, and replaces that span — never text the user typed — when the answer
    arrives, keeping the dictated words one undo away until the next edit or send. A send while the
    request is out sends the words as dictated and drops the request; a failure leaves the words as
    dictated and says so in one line. The switch, the model and the strength are the user's own
    settings, off by default, disabled with a note when `polish.enabled` is false.
16. **An app below the gateway's minimum updates before it does anything else.** A separately
    installed app compares its own `major.minor.patch` with `apps.<app>.minimum_version` from
    `GET /api/health`, `GET /api/config` and `hello` (A31), whichever it sees first, and when it is
    older shows a blocking "Update required" screen — its version, the gateway's minimum, a button
    to `update_url` when there is one, and Sign out — and nothing else until it is updated. An equal
    or newer version, or a gateway that sends no `apps`, changes nothing.
17. **A pending resume is shown where the session is, and is the person's to end.** A session
    whose `resume` is set says, above its transcript, that it was paused by the usage limit and when
    it resumes (A35, 7.2), with one action to change the time and one to cancel; the row goes when
    `resume` is null. The Settings switch for `resume_after_limit` reads and writes the account's
    preferences (3.2), so the phone, the browser and every device agree; a gateway that sends no
    `preferences` in `hello` predates the switch, and the app shows it disabled with a note. A
    `user_message` with `source: "resume"` is drawn in the person's bubble, captioned as sent for
    them after the limit reset, and a `turn_started` with `trigger: "resume"` reads in the status
    line as a remote turn does.

---

18. **A device keeps itself current; an app shows only what went wrong.** The gateway updates
    every device to the wheel it serves without being asked (A36), so an app never shows a device's
    client version or an "Update available" on the device row, and never offers Update while
    nothing is wrong. The row says "Updating…" while `update_state` is `updating` and "Update
    failed · <message>" while it is `failed`, and only then offers **Retry update**, which sends
    `device.update` with `config.client.build` exactly as A22's Update did. Neither the row nor
    the device's own page states the client version or its build: a person is never asked to know
    it.

19. **A folder can be made where a session will work.** The directory picker offers **New folder**
    wherever it shows a listing: it asks for a name, sends `device.mkdir {path, name}` for the
    directory on screen, and on success the new directory is the listing on screen — empty, ready
    to be chosen — so the picker's choose action picks it (A37). A `conflict` is said beside the
    name and the name kept for editing; a `bad_request` says what a name may not contain. The
    picker makes one folder at a time and never deletes, renames or moves anything.

20. **A device row opens a terminal; its menu holds the rest.** Tapping a device that is online
    and offers a terminal opens a full-screen shell on it (7.3, A38), on the phone as on the web; an
    offline device or one without the capability says so instead of opening anything. The row's
    swipe or menu offers, in this order, **Rename**, **Retry update** (only while `failed`, rule
    18), **Show quota** (the device page of A33) and **Revoke**. The terminal screen renders the
    bytes with a real terminal emulator, sends what the person types as bytes, resizes with its
    view, reconnects with `terminal.attach` after a lost socket for as long as the device keeps the
    shell, and says plainly when the shell has exited or the device is gone. A phone's terminal
    carries a key bar above the keyboard — Esc, Tab, a sticky Ctrl, the arrows, Ctrl-C, Ctrl-D and
    the characters a shell needs — and a way to paste, copy a selection and change the type size.
21. **Closing a session the device drives, then the Archive.** On a row whose `control` is
    `remote` and that is not archived, the app's action is **Close**, not Archive: `session.archive
    {archived: true}` asks the device to end the session — interrupt a running turn, end what it
    holds for the agent (Claude's CLI, pi's process, the Codex thread, the Grok leader session) —
    and only then to mark it `archived: true`, `control: "none"`, `state: "stopped"`, in one reply
    and one publish. Nothing the agent says while it is being closed revives it. While the session
    is working the app asks first ("Close this session?"); an idle one closes on the tap. A row a
    terminal holds offers nothing, a row already in the Archive offers nothing, and writing to a
    closed session brings it back as before (A15).
22. **A session that stopped working and waits for you is marked until someone looks.** A session
    whose `unseen` is true (4.4) carries a red dot on its row in every list that draws the row, and
    the number of such sessions among the unarchived ones is the app's icon badge where the platform
    has one, none at zero. An app sends `session.seen` (6.2) for the conversation the person has in
    front of them, so a session already on screen when its turn ends never keeps a dot: that
    conversation draws none and is left out of the count while its `session.seen` is on its way, so
    nothing flickers during the round trip. Signing out clears the badge (A47).

## 9. Conformance checklist

### 9.1 Gateway

- [ ] `hello` on `/ws/app` carries `protocol: 1`, the full device and session lists and `server_time`.
- [ ] Rejects a `hello` whose `protocol` is not 1 with error code `unsupported`.
- [ ] Adds `device_id` to forwarded requests addressed by `session_id`, stamps `from`, and strips
      `from` from the reply before delivering it to the originating connection only.
- [ ] Answers `device_offline` when the device has no socket, and `timeout` after 60 s.
- [ ] Pings every 25 s on both sockets; closes a connection silent for 90 s.
- [ ] Enforces the `Origin` check on cookie-authenticated upgrades and mutating requests.
- [ ] Rate limits `POST /api/login` to 5 per minute per IP.
- [ ] Reports the served wheel as `client` in `GET /api/config`, stores `client_build` from each
      `hello`, forwards `device.update`, marks the device `updating` on an accepted reply and
      `failed` with the message on `update.failed` or when no `hello` follows within five minutes,
      and clears both on the next `hello` (A22).
- [ ] Forwards `device.mkdir` to the device it names, as it forwards `device.dirs` (A37).
- [ ] Forwards the five `terminal.*` requests by `device_id`; delivers `terminal.output` and
      `terminal.exited` to the one app connection `to` names and to nobody else — `to` stripped,
      `device_id` added, and only when that connection belongs to the device's account; remembers which
      connection holds each terminal from the `open` and `attach` replies, and sends the device
      `terminal.detach` on its own account when that connection closes; stores `terminal` from
      `hello` on the `Device` (A38).
- [ ] Sends `device.update {build}` itself, `from: "gateway"`, to a device whose `hello`
      carries a `client_build` that is neither null nor the served build — once per served build
      per device, retried while the device answers `conflict` for a running session, never again
      after `update.failed` or a timeout until an app retries or the served build changes; a
      device that answers `unsupported` is left alone (A36).
- [ ] Issues claim tokens for hosts, lets a signed-in user claim one, mints the pairing code for
      the host on that claim and hands it out exactly once (A23).
- [ ] Sends every `/ws/app` frame after `hello` — `device.updated`, `session.updated`,
      `session.removed`, `session.event`, `pairing.progress` — only to sockets of the account that
      owns the device; answers `not_found` to a `session.subscribe` or a forwarded request naming
      another account's session or device; lists only the caller's sessions in `GET /api/sessions`;
      delivers push only to the owner's registrations (A24).
- [ ] Takes registrations only while registration is open, stores passwords hashed, refuses a
      disabled account at `/api/login` with `403`, at `/ws/app` with 4401 after revoking its login
      sessions, and at `/ws/device` with 4403; restricts 3.9 to `admin`; never disables, demotes,
      re-passwords or deletes `admin` (A24).
- [ ] Issues pairing codes as `RC-XXXX-XXXX` in Crockford base32 without I, L, O and U, single use,
      10-minute lifetime, and emits `pairing.progress` through `waiting`, `enrolled`, `online`,
      `agents`.
- [ ] Drops session-event frames over 64 KiB and logs them.
- [ ] Keeps a replay buffer of 2 000 events or 4 MiB per session and sets `resync: true` when a
      `since_seq` falls outside it.
- [ ] On a device reconnect, backfills every session whose device `last_seq` is ahead of the
      replay buffer tail with `session.history {after_seq}`, appends the result to the buffer and
      fans it out to subscribers as `session.event` frames.
- [ ] Stamps `device_id` on every `session.event` and `session.removed` frame sent to an app.
- [ ] Remembers the latest `queue` event payload per session and returns it as `queue` in the
      `session.subscribe` reply.
- [ ] Persists the latest `Session` summary per session so `GET /api/sessions` works while a device
      is offline.
- [ ] Closes the older socket with code 4001 when a device reconnects.
- [ ] Uses close code 4401 for a missing, invalid, expired or revoked credential, 4403 for a
      credential that is valid but not allowed here, and 1008 only for protocol violations.
- [ ] Push payloads contain only the `rc` object of 3.7 and no message content.
- [ ] Reports `polish.enabled` in `hello` and `GET /api/config`, answers `GET /api/polish/models`
      from the configured provider and `POST /api/polish` with the polished text only, refuses both
      with `503` `unsupported` when no polish model is configured, and stores nothing from either
      (A29).
- [ ] Sends the model exactly the text and the context the app supplied, with the strength
      instructions of 3.5, and never a device's history of its own reading (A29).
- [ ] Reports `minimum_version` as `major.minor.patch` for each of `apps.ios`, `apps.macos`,
      `apps.android` and `apps.windows` in `GET /api/health`, `GET /api/config` and `hello`, each
      with its `update_url` when configured, and raises an app's minimum in the same release that
      stops supporting its older builds (A31, A45, A46).
- [ ] Sets `unseen` when a session moves from working to waiting and clears it on `session.seen`,
      on working again, and on archive or removal, keeps it across restarts, sends every change as
      `session.updated`, carries `rc.badge` (and `aps.badge`) in every push, and sends APNs
      registrations a badge-only notification when the count changes without one, 3 s after it
      settles (A47).

- [ ] Stores `preferences` per account, answers `GET` and `PATCH /api/preferences` for the caller's
      account only, carries the object in `hello`, sends `preferences.updated` to the account's app
      sockets and `preferences` to its devices after `hello_ack` and on every change, forwards
      `session.resume_set` and `session.resume_cancel`, and pushes `limit_reached`, `resumed` and
      `resume_dropped` on the matching `resume` events (A35).
- [ ] Stores the Settings preferences of A41 — `language`, `stt_language`, `polish_enabled`,
      `polish_model`, `polish_strength`, `timeline_detail` — per account beside the switch; leaves a
      field absent until it has been set; validates a `PATCH` value against the field's type and
      words and answers `bad_request` otherwise; publishes the whole object on every change in the
      order the writes arrived (A41).
- [ ] Has its speech provider detect the language of every transcription, on `/ws/stt` and
      `POST /api/stt/transcribe` alike; ignores a `language` a client sends; reports `stt.languages`
      as `["auto"]` (A44).
- [ ] Names a device with the `name` it enrolled with and changes it only on
      `PATCH /api/devices/{device_id}`, for the owning account alone; accepts `name` in `hello`
      without applying it, so a rename survives reconnects and restarts (A50).

### 9.2 Device

- [ ] Assigns `seq` per session, strictly increasing, persisted across restarts.
- [ ] Reports every agent it knows how to drive — `claude`, `codex`, `grok`, `pi` — with
      `available: false` when the binary is missing, and drives a session of any of them through
      the same frames (A25, A26).
- [ ] Lists capability `commands` only for an agent whose sessions can list and run slash
      commands remotely, answers `session.commands` with what the agent offers now, echoes every
      `session.command` as a `user_message` under the request's `id`, and refuses one while a
      turn is running with `conflict` (A27).
- [ ] Emits `status` on every state change and `turn_started` / `turn_completed` around every turn.
- [ ] Reports `readonly` only for a terminal-controlled session with no turn in progress, and
      `running` while a terminal-driven turn is working.
- [ ] Coalesces streaming deltas to at most one flush per 80 ms per block, and ends every stream with
      a `done: true` event that carries the full `text` and no `delta`.
- [ ] Truncates `input`, `output` and `diff.patch` at 8, 16 and 32 KiB and sets the matching
      `*_truncated` flag; serves the untruncated block through `session.block`.
- [ ] Produces a one-line `title` for every `tool_call` and `approval`.
- [ ] Sets `first_seq` on every replacement event and on every `session.history` and
      `session.block` result, equal to the `seq` at which the block first appeared.
- [ ] Includes at least one `primary` and one `danger` option in every `approval`.
- [ ] Treats a repeated `session.send` id within a session as a duplicate and replays the original
      result.
- [ ] Refuses `session.send` on a `control: "terminal"` session with `conflict`, and resumes a
      `control: "none"` session before sending.
- [ ] Echoes `id` and `from` in every `reply`.
- [ ] Returns `session.history` exactly as 6.4 describes: latest event per block, ascending `seq`, no
      deltas, no `status`, no `meta`, no `queue`.
- [ ] Honours `after_seq` as well as `before_seq`, rejects a request carrying both with
      `bad_request`, and caps either form at `limit` with an accurate `has_more`.
- [ ] Reports accurate `capabilities` per agent, and never advertises `steer` or `takeover` it
      cannot perform.
- [ ] Reports `control: "shared"` only while the attachment is live, and moves the session to
      `terminal` or `none` as soon as it drops.
- [ ] On a `shared` session injects only while the CLI's own turn is idle, never waiting on its
      subagents; steers a `mode: "auto"` message into a running turn through the turn hooks when it
      reports `shared_steer`; holds everything else as a `queue` entry with no `user_message`,
      injecting the oldest as each turn ends; and emits the block with `delivery: "delivered"` only
      once the message is injected or handed to the model (A19, A51).
- [ ] Keeps every queue in `ts` order with no two entries sharing a `ts`: holds a queued message
      that carries `queue_ts` under that `ts`, in front of every entry with a later one, any other
      at the end under a `ts` later than the last entry's, and a message it puts back itself under
      its own `ts`; refuses a `queue_ts` that is not a non-negative integer with `bad_request`;
      reports `attachments` on an entry that holds files (A43).
- [ ] Reports `terminal` in `hello`; runs the login shell in a pseudo-terminal per `terminal.open`
      (at most four), coalesces output into `terminal.output` frames of at most 16 KiB with a rising
      `seq`, keeps 64 KiB of scrollback, writes `terminal.input` and applies `terminal.resize`,
      keeps a detached shell ten minutes for `terminal.attach`, ends it on `terminal.close` or when
      the shell exits (`terminal.exited`), and never logs what travels (7.3, A38).
- [ ] Answers `device.mkdir` with the new directory's listing; makes exactly one level as `mkdir`
      would; refuses a `name` with a `/`, a leading `.` or more than 255 bytes with `bad_request`,
      a `path` that is not a directory with `not_found`, an existing entry with `conflict`, and a
      directory it may not write with `forbidden` (A37).
- [ ] Reports `client_build` in `hello`, answers `device.update` as 6.3 says, verifies the wheel's
      SHA-256 before installing, restarts itself only after a successful install, and sends
      `update.failed` otherwise (A22).
- [ ] Advertises `speeds` from the agent's own catalogue, maps `session.set` / `session.create`
      `speed` to the agent's setting (Codex: `thread/settings/update {serviceTier}`), refuses a tier
      the session's model does not offer with `unsupported`, and reports the tier in `Session.speed`
      and `meta` (A21).
- [ ] Raises a `question` block with `needs_input` for an `AskUserQuestion` its `PermissionRequest`
      hook reports on an attached Claude session, answers the hook from the first `session.answer`,
      and resolves the block with `by: "terminal"` when the transcript shows the terminal answered
      first (A20).
- [ ] Raises a `question` block with `needs_input` for every `_x.ai/ask_user_question` Grok Build
      sends, on a private child and on the leader, and hands Grok the first `session.answer`;
      resolves the block `by: "terminal"` from the tool's own result when another client answered
      first, and expires it with its turn. On the leader it never settles a prompt another client
      may answer by itself — only an app's answer or Stop does: it sends nothing for a session it
      does not route, nothing when it lets go of a session a terminal shares, and puts no deadline
      of its own on such a prompt (A52).
- [ ] Offers exactly `allow` and `deny` on an approval relayed through a Claude channel, offers the
      options the Codex daemon lists for that request, resolves either with `decision.by: "terminal"`
      when the terminal answered first, and expires it when the CLI exits or the attachment drops.
- [ ] Hands an agent that takes no image in a prompt — Claude Code, Grok Build — each attachment as
      a file its prompt names, publishes the typed words and `Attachment` metadata and never a
      path, reads its own prompt back the same way from the agent's records, and deletes the files
      of a message that cannot reach the agent any more (4.8, A49).
- [ ] Reports `shared_settings` and `shared_attachments` truthfully per agent, and honours them:
      `session.set` and `session.send` attachments succeed on a `shared` session exactly when the
      matching flag is true and are refused with `unsupported` when it is false. Reports
      `shared_steer` only when a running `shared` session steers `mode: "auto"` (A51).
- [ ] Counts a turn that a row the Claude CLI hides starts — a teammate's or another session's
      message under `origin.kind: "peer"`, a turn the CLI continues by itself — as a turn, and
      publishes the words of its `<agent-message>` or `<cross-session-message>` as A30 says (A51).
- [ ] Publishes what the Claude CLI folds into a running turn at a tool boundary as the same words
      would be published had they started a turn, never its own folded injection as a block, and
      emits a message steered at that boundary beside them in the order the model reads them (A51).
- [ ] Sets `attach_ready` for Codex from a successful handshake on the shared daemon socket, not
      from the socket file existing, and falls back to a per-session app-server with
      `attach_ready: false` when there is no daemon.
- [ ] Derives Codex `origin` and `control` as 4.4 describes, keeps a thread `shared` until it is
      unloaded, and reports `terminal` for a rollout held by a Codex process that is not the daemon.
- [ ] Resolves a Codex approval it did not answer with `decision: {option_id: "elsewhere",
      by: "terminal"}`.
- [ ] Sets `attach_ready` for Grok Build from the person's configuration (`[cli] use_leader` on and
      no sandbox profile), joins a registered leader-mode terminal session with `session/load` and
      reports it `shared`, and leaves a session a terminal holds outside the leader as `terminal`
      (A28).
- [ ] Derives Grok `origin` and `control` as 4.4 describes, never sends `session/close` for a
      session a terminal registered or one it did not create, and publishes a replay only above
      the `eventId` it already applied (A28).
- [ ] Offers the leader's approval options except the one enabling always-approve mode, and resolves
      an approval another client answered with `decision: {option_id: "elsewhere", by: "terminal"}`
      (A28).
- [ ] Publishes a teammate's message or a task notification the Claude CLI filed as a user turn as
      `user_message {source: "agent"}` with the envelope and every `<system-reminder>` removed,
      never as `terminal`, and starts its turn with `trigger: "agent"` (A30).
- [ ] Publishes no block for Claude's compaction summary or interruption marker, ends the turn an
      interruption marker closes with `stop_reason: "interrupted"`, emits the compaction `notice`
      from the `compact_boundary` row, and publishes a slash command typed at the terminal once, as
      `user_message {source: "terminal"}` that starts no turn (A32).
- [ ] Reports each agent's `accounts` from the agent's own files in `hello` and `agents.updated`
      without `limits`, answers `device.agents` with `limits` read fresh for every `account` — or
      `limits_error` when the read failed — and never writes a credential or refreshes a token (A33).
- [ ] Reads the end of a mirrored Claude turn from the assistant row's `stop_reason`, never from a
      row that happens to hold no `tool_use` — the CLI writes each content block as a row of its
      own — so a message held for a running turn goes out only once the turn has really ended; and
      injects an absorbed message, or one the CLI let go, again — an `auto` one at the running
      turn's next batch of tool calls when the session steers, any other at a turn's end — at most
      three times, then publishes an `error` naming it under the bubble's `block_id` and never
      `delivered` (A34, A51).
- [ ] Moves a Claude attachment to the session id the CLI's `SessionStart` hook names, and removes
      a terminal-origin session with no events and no transcript with `session.removed` the moment
      its terminal leaves it (A16).
- [ ] Reports `model`, `permission_mode` and `effort` for a mirrored Claude session from its
      transcript and publishes each change as `meta` (A17).
- [ ] Publishes a Codex thread only when its `source` is a string and either its `originator` is
      the device's own, its `source` is `cli` or `exec`, or its `originator` is `codex-tui` and its
      `source` is `vscode`; other applications' `vscode` threads and every subagent stay foreign.
      Removes with `session.removed` any other thread it published before (A18, A48).

- [ ] Ends a turn the usage limit stopped with `stop_reason: "error"` and `limit`, read from the
      agent's own signal — Claude's 429 result, Codex's `usageLimitExceeded` — never from words,
      and publishes the vendor's sentence as `error`, not as the agent's text; with
      `resume_after_limit` on, schedules the resume of 7.2 on a `remote` or `shared` session, keeps
      it across restarts, fires it a minute after the reset with the fixed prompt as `user_message
      {source: "resume"}` under `trigger: "resume"`, drops it when the terminal that owned a
      `shared` session is gone, cancels it when the person sends first or the switch goes off,
      reschedules up to three times when the resumed turn hits the limit again, publishes each step
      as a `resume` event, and answers `session.resume_set` and `session.resume_cancel` (A35).

- [ ] Closes a `remote` session on `session.archive {archived: true}`: interrupts a running turn,
      ends what it holds for the agent — the Claude CLI process, the pi process, the Codex thread
      (`turn/interrupt`, then `thread/archive`), the Grok leader session (`session/cancel`, then
      `session/close`, safe because no terminal is in a `remote` session) — and replies and
      publishes only when that is done, with `archived: true`, `control: "none"`, `state:
      "stopped"`; nothing the agent says while the close is in progress revives the session (A39).
- [ ] On a `shared` Claude session, applies `session.set` for `model` and `effort`, and
      `session.command` for `compact`, by typing into the CLI's terminal; types only when the
      terminal is idle and nobody is typing there, answers `conflict` otherwise and queues nothing;
      confirms a settings change from the transcript before replying and before `meta` says so;
      writes no settings file of the person's; reports `shared_settings: true` with
      `shared_settings_keys: ["model", "effort"]` and the `commands` capability only when the shim
      that provides the pseudo-terminal is installed (A40).
- [ ] On a `shared` Claude session, applies `session.stop` by typing Escape into the CLI's
      terminal only while a turn is running and no approval or question is on screen; answers
      `conflict` while one is; types nothing and returns `{}` when no turn runs; reports
      `shared_interrupt: true` with the same condition as `shared_settings` (A42).

### 9.3 App

- [ ] Ignores unknown fields, unknown event kinds and unknown agent ids.
- [ ] Labels `grok` and `pi` by name and by logo, draws no permission picker for an agent whose
      `permission_modes` is empty, and no effort control for one whose `efforts` is empty (A25,
      A26).
- [ ] Signs in with a username and a password, offers registration only when `registration_open`,
      shows the signed-in account and its role, lets a `member` change its own password, and shows
      the accounts screen only to `admin` (A24).
- [ ] Shows `model`, `permission_mode` and `effort` on a terminal-held session as values it cannot
      change, by label when `AgentInfo` lists the id and by the id otherwise (A17).
- [ ] Opens a terminal on a device row's tap and orders the row's menu Rename · Retry update ·
      Show quota · Revoke, as rule 20 says; renders with a terminal emulator, resizes, attaches
      again after a lost socket, and on a phone carries the key bar of rule 20 (A38).
- [ ] Offers **New folder** in the directory picker as rule 19 says: a name, `device.mkdir` for
      the directory on screen, the new directory shown and choosable, a clash said beside the name
      (A37).
- [ ] Offers Rename and Revoke on every device row, shows no client version or build on the row
      or the device page and no "Update available", reads "Updating…" and "Update failed ·
      <message>" from `update_state`, and offers **Retry update** only while it is `failed`
      (A22, A36).
- [ ] Offers `speeds` as one control that cycles standard → each tier → standard, drawn only when
      the list is non-empty, and shows a terminal-held session's `speed` as a value it cannot change
      (A21).
- [ ] While a `question` is pending, sends the composer's draft as that question's free-text answer
      through `session.answer` instead of queueing it, and shows a resolved question's `by` (A20).
- [ ] Applies events by `block_id` with the replacement and streaming rules of 5.1, orders blocks
      by `first_seq ?? seq`, and drops events whose `seq` is not newer than the last applied one.
- [ ] Follows the reconnect order of 8.6 and treats 60 s of silence as a dead connection.
- [ ] Applies `todos` and `queue` snapshots from replayed events, from history and from the
      `queue` field of a `session.subscribe` reply, not only from the live stream.
- [ ] Renders exactly the approval and question options it was given, keyed by id, and disables them
      once resolved or expired.
- [ ] Disables the composer from `control`, not from `state`, and offers "Take over" when the
      agent has that capability.
- [ ] Treats `control: "shared"` like `remote` for the composer, approvals and queue, never offers
      "Take over" on it, and shows Stop only when the agent has capability `interrupt` **and**
      reports `shared_interrupt: true`.
- [ ] Enables the model, permission-mode and effort pickers on a `shared` session only when the
      agent reports `shared_settings: true`, and the attachment button only when it reports
      `shared_attachments: true`.
- [ ] Renders a `decision` of `option_id: "elsewhere"` with `by: "terminal"` as answered in the
      terminal, and never offers `elsewhere` as a button.
- [ ] Words the hint on a `terminal` session per `attach` kind, `leader` included: with
      `attach_ready: false`, that the device needs `rc-client grok setup` and Grok restarted; with
      `attach_ready: true`, that this `grok` was started without the leader (A28).
- [ ] Renders `user_message.delivery` rather than assuming every message reached the agent.
- [ ] Stops reconnecting on close code 4401, clears the stored credential and returns to login;
      reconnects with backoff on any code other than 4401 and 4403.
- [ ] Never auto-resends a `session.send`; retries reuse the original `id`.
- [ ] Shows the queued or steered outcome from `accepted` rather than guessing.
- [ ] Words a running session as steering — before sending as after — when the agent's
      `capabilities` has `steer`, or when the session is `shared` and the agent reports
      `shared_steer: true`, and as queueing otherwise (A51).
- [ ] Shows the queue as one "Up next · N" control that opens the list of queued messages; every
      entry can be removed, and one without `attachments` can be edited — taken out with
      `session.queue_remove`, edited in the composer, and sent back with `mode: "queue"` and its
      `queue_ts` (A43).
- [ ] Opens the command list when `/` is typed into an empty composer on a session whose agent
      has capability `commands`, filters it by name as the user types, shows each command's
      description and argument hint, sends a matched first word as `session.command` and
      anything else as `session.send`, and draws nothing for an agent without the capability
      (A27).
- [ ] Draws a `user_message` with `source: "agent"` on the agent's side, muted and captioned "from
      another agent", never in the person's bubble, hides it at the Simple detail level, and treats
      `trigger: "agent"` like `terminal` in the status line (A30, A34).
- [ ] (iOS) Compares its version with `apps.ios.minimum_version` from health, config and `hello`, and
      below it shows the blocking "Update required" screen of 8.16 and nothing else (A31).
- [ ] (macOS) Does the same with `apps.macos.minimum_version`, and never measures itself against the
      iPhone app's entry (A45).
- [ ] (Android) Does the same with `apps.android.minimum_version`, and (Windows) with
      `apps.windows.minimum_version`; each reads its own entry and no other (A46).
- [ ] Draws the red dot on every row of a session whose `unseen` is true, sends `session.seen` for
      the conversation the person has in front of them (on opening it, on its window coming to the
      front, and on a mark arriving while it is on screen), keeps the app icon's badge at the count
      of unarchived unseen sessions where the platform has a badge, and clears it on sign-out; the
      conversation in front draws no dot and is not counted (A47).
- [ ] Offers the dictation polish switch, model and strength only when `polish.enabled` is true
      (disabled with a note otherwise), polishes only the dictated span, keeps the dictated words one
      undo away, sends the words as dictated when the user sends first, and never sends a polished
      text by itself (A29).
- [ ] Opens a device from its row, lists the agents found on it with how each is signed in, draws a
      meter per `AgentLimit` for accounts only, asks `device.agents` for fresh limits when the page
      opens, and draws nothing where `plan`, `email`, `endpoint` or `limits` are absent (A33).
- [ ] Binds the interface language, the dictation language, polish and its model and strength, and
      the timeline detail to the account's `preferences` (A41): shows the value `hello` and
      `preferences.updated` carry the moment they arrive, writes a change up with `PATCH` and takes
      the reply as the value, writes its own value up once for a field the account has not set,
      and keeps on the device only what belongs to it — notifications, the app lock, the
      transcription backend, the terminal font size, list folds.
- [ ] Offers no dictation language where the gateway transcribes, which detects it (A44); where
      the phone's own recogniser transcribes, offers that recogniser's languages without an
      automatic choice, Chinese when `stt_language` is unset or `auto`.
- [ ] Offers the "Resume after the limit resets" switch in Settings bound to the account's
      `preferences` — disabled with a note when `hello` carries none — shows a session's pending
      `resume` above its transcript with the time, a way to change it and a way to cancel it, draws
      the `resume` rows and a turn's `limit` end in the timeline, draws a `source: "resume"`
      message in the person's bubble with its caption, and treats `trigger: "resume"` like
      `remote` in the status line (A35).
- [ ] Offers **Close** — not Archive — on a row whose `control` is `remote` and that is not
      archived, asks first only while the session is working, and offers nothing on a row a terminal
      holds or a row already in the Archive (A39).
- [ ] Reads `shared_settings_keys`: on a `shared` session the settings it names are pickers and the
      others are the values the terminal set (A17), never a control that fails when tapped; a
      `conflict` from `session.set` or `session.command` is shown in the reply's words (A40).

- [ ] Decodes every fixture under `fixtures/` in its test suite.

---

## 10. Fixtures

| Path | What it is |
| --- | --- |
| `fixtures/app/` | One frame per app-socket type, in both directions, plus typed replies |
| `fixtures/device/` | One frame per device-socket type |
| `fixtures/device/forwarded/` | All seventeen forwarded requests as the device receives them, plus the A9 backfill and A43 re-queue variants |
| `fixtures/events/` | One event per kind, and one `tool_call` per `tool_kind` |
| `fixtures/objects/` | Bare `Session` and `AgentInfo` objects that no frame fixture carries, including the two attachable agents and the shared sessions |
| `fixtures/http/` | One body per HTTP request and response |
| `fixtures/stt/` | The five speech-to-text text frames |
| `fixtures/timelines/claude.json` | A complete Claude Code turn, 41 events |
| `fixtures/timelines/codex.json` | A Codex CLI session with steering and a queued message, 37 events |
| `fixtures/history/page.json` | A `session.history` reply that obeys 6.4 |
| `fixtures/replay/subscribe.reply.json` | A `session.subscribe` reply served from the replay buffer |
| `fixtures/replay/subscribe.codex.reply.json` | The same, mid-turn, carrying a `queue` snapshot |
| `fixtures_invalid/` | Frames that must be rejected, used to self-test the validator |

A timeline file is a test harness object, not a wire frame: `{name, description, device, session,
session_final, frames}`. Feed `frames` into a timeline reducer and the result must match
`session_final`. Its schema is `schema/timeline.json`.

---

## 11. Amendments

Changes made after the contract was first frozen, each part of it. This document carries the
amended wording; the list is here so a reader who knows the original text can see what moved.

**2026-09-09 A1 — `tool_kind`.** The tool-category field inside `tool_call` and `approval` events is
named `tool_kind`, with values unchanged:
`shell | read | edit | write | search | web | mcp | subagent | todo | other`. `kind` is exclusively
the event discriminator. Sub-agent parents are `tool_call` events with `tool_kind: "subagent"`.
See 5.1, 5.5 and 5.7.

**2026-09-09 A2 — `Usage`.** `input_tokens`, `output_tokens` and `total_tokens` are required
integers. `context_used`, `context_window` and `cost_usd` are optional and may be `null` or absent,
because Codex reports no cost. See 4.7.

**2026-09-09 A3 — Attachments.** `user_message.attachments[]` items are `{name, mime, size}`;
`session.send.attachments[]` items are `{name, mime, data_base64}`, and the device computes `size`.
See 4.8.

**2026-09-09 A4 — WebSocket close codes.** The gateway closes an `/ws/app` or `/ws/device` socket
with **4401** when the credential is missing, invalid, expired or revoked, and apps must stop
reconnecting and return to login; **4403** when the credential is valid but not allowed for the
requested resource; **4001** when a device connection is replaced by a newer one, and the device
should not reconnect immediately; and **1008** only for protocol violations. Any other close code
means reconnect with backoff. See 2.5.

**2026-09-09 A5 — `device_id` on forwarded session frames.** The gateway adds `device_id` to every
`session.event` and `session.removed` frame it sends to apps, because it knows the device from the
socket. `session_id` remains globally unique and is the primary key for apps; `device_id` is
informational and for routing. Devices do not send it. See 6.1.

**2026-09-09 A6 — Queue snapshot on subscribe.** The gateway remembers the latest `queue` event
payload per session and returns it as an optional `queue: {pending: [...]}` field in the
`session.subscribe` reply, absent when it has seen none. Apps must apply `todos` and `queue`
snapshots found in replayed events and history, not only in live streams. See 6.2 and 8.8.

**2026-09-10 A7 — `readonly` versus `running` for mirrored sessions.** `state: "readonly"` is used
only when `control == "terminal"` **and** no turn is in progress. While the terminal-driven turn runs
the device reports `running`, with `needs_approval` and `needs_input` as usual. Apps decide whether
the composer is enabled from `control`, never from `state` alone. See 4.5 and 8.5.

**2026-09-10 A8 — Block ordering across reloads.** Every block event carries an optional integer
`first_seq`, the `seq` at which that block first appeared in the session, equal to `seq` on the first
event. Devices set it on replacement events and on `session.history` and `session.block` results.
Apps order blocks by `first_seq ?? seq`, so a long-running tool call that finishes late stays where
it started. `session.history` keeps returning the latest event per block in ascending `seq` order.
See 5.1, 6.4 and 8.2.

**2026-09-10 A9 — Backfill after a device link outage.** `session.history` accepts an optional
integer `after_seq`. When present the device returns its stored events with `seq` greater than
`after_seq`, the latest version per block plus the non-block events of 6.4, ascending, at most
`limit`, with `has_more`. `before_seq` and `after_seq` are mutually exclusive. When a device
reconnects, the gateway compares each session's `last_seq` from the device `hello` with the tail of
its replay buffer; for every session whose device `last_seq` is ahead it requests
`session.history {after_seq: <buffer tail>}`, appends the returned events to the buffer and fans them
out to current subscribers as ordinary `session.event` frames. Apps need no change, because they
apply events by `block_id`. See 6.4 and 7.1.

**2026-09-10 A10 — `control: "shared"` for attached terminal sessions.** `control` gains a fourth
value, `shared`: a live CLI process owns the session **and** the device is attached to it, through a
channel the Claude CLI loads or through the Codex shared app-server. Apps treat `shared` like
`remote` for the composer, approvals, queue and user-message rows, never offer "Take over" on it,
and show Stop only when the agent has capability `interrupt` and reports `shared_interrupt: true`.
`AgentInfo` gains three optional fields, `attach` (`channel | daemon | null`), `attach_ready` and
`shared_interrupt`; `capabilities` is unchanged. `user_message` gains an optional `delivery`
(`pending | delivered | absorbed`), set only on `shared` sessions, because the device may inject a
message only while the CLI is idle and holds it otherwise. A relayed `approval` offers exactly
`allow` and `deny`, carries `{tool_name, description, input_preview}` as its `input` and no `diff`,
and resolves with `decision.by: "terminal"` when the terminal answers first. On a `shared` session
`session.send` and `session.approve` work, `session.answer` and `session.takeover` do not,
`session.stop` needs capability `interrupt` together with `shared_interrupt`, and `session.set`
accepts only `title`. `session.send`
keeps the existing result vocabulary: `accepted: "sent"` when the message is injected straight away,
`accepted: "queued"` with a `queued_id` when it is held. `readonly` stays reserved for
`control == "terminal"` (A7). See 4.2, 4.4, 4.5, 5.2, 5.7, 6.3, 8.10 and 9.

**2026-09-10 A11 — Codex through the shared app-server daemon.** Codex runs every bare `codex` TUI
inside one local app-server daemon, and the device attaches to it as a second client, so a Codex
`shared` session can do more than a Claude one. `AgentInfo` gains two more optional booleans,
`shared_settings` (whether `session.set` for `model`, `permission_mode` and `effort` works on a
`shared` session) and `shared_attachments` (whether `session.send` attachments are delivered);
both default to false and both are false for Claude. Codex reports `attach: "daemon"`,
`attach_ready` from a successful handshake on the daemon socket rather than from the socket file
existing, and `shared_interrupt`, `shared_settings` and `shared_attachments` all true, with
`capabilities` unchanged. On a shared Codex session `session.send` behaves as it does on a remote
one, including `accepted: "steered"` and attachments, `session.stop` interrupts, `session.set`
reaches the daemon, and `session.answer` is supported; `session.takeover` is still `conflict`. A
relayed Codex approval offers the daemon's own options, drawn from `allow`, `allow_session`,
`allow_always` and `deny`, carries `{command, cwd, command_actions}` as its `input` for a command and
a `diff` when the daemon supplies one; when the request is resolved somewhere else the block ends as
`decision: {option_id: "elsewhere", by: "terminal"}`. A Codex thread has no rollout until its first
turn starts, so the device cannot subscribe to a freshly created thread and subscribes when that
turn creates the rollout, while a thread created from an app is reopenable with `codex resume <id>`
only after its first turn. A10's "exactly allow and deny" applies to
Claude channels only, and `session.approve` rejects any `option_id` the block did not offer. `origin`
and `control` for a Codex thread follow the table in 4.4, where `shared` lasts only while a terminal
still has the thread: the daemon says nothing when a TUI exits, so the device watches for the TUI
process in the thread's `cwd` and reports `remote` or `none` once it is gone. Apps stay
agent-agnostic: they read the five attachment fields.
See 4.2, 4.4, 5.7, 6.3, 8.10, 8.13 and 9.

**2026-09-11 A12 — the app's request id is the `user_message` block id.** Apps waited for the
device's `user_message` before showing what the user had just typed, which costs a full
app→gateway→device→gateway→app round trip on every send. The `user_message` a device emits for a
`session.send` now carries the request's `id` as its `block_id`, so an app renders the message at
once under that id and the device's event replaces it by the ordinary replacement rule; queued
messages keep the same id from `queue.pending[].id` through to their `user_message`. Devices that
predate this still emit their own ids, and an app must then fall back to reconciling by text and
`source`. See 2.4, 5.1, 5.2 and 5.12.

**2026-09-11 A13 — one keepalive clock, and a grace period before `online: false`.** Devices were
reported offline for half a minute at a time about twice an hour. The gateway's WebSocket server
had its own default 20 s ping/pong timeout on top of the 25 s / 90 s contract in 2.5, so a device
whose event loop stalled for twenty seconds was closed with `1011` and broadcast as offline the same
instant. 2.5 now states that the 25 s / 90 s application pings are the only keepalives, and that the
gateway holds `online: true` for a 20 s grace period after a transient close, answering requests
addressed to the device only once the period ends without a replacement connection (they wait for
one in the meantime). `4401`, `4403` and an explicit removal still flip `online` immediately. See
2.5 and 4.1.

**2026-09-11 A14 — a steered `user_message` is emitted when the agent takes it.** A message sent
into a running Codex turn was drawn where it was sent, while the terminal drew it where Codex read
it: after the agent message that was already streaming. The two orders disagreed on every send made
mid-turn. The device now emits the `user_message` for an `accepted: "steered"` send only when the
agent reports having taken it (the daemon's `userMessage` item), so `first_seq` lands after the
output that preceded it, and falls back to the end of the turn when the message was never taken.
Apps keep the optimistic row from A12 at the bottom in the meantime. See 5.2 and 9.

**2026-09-11 A15 — a session that comes back to life leaves the Archive.** An archived session that
was resumed from a terminal, or written to from an app, ran on with `archived: true` and stayed
folded in the Archive while it worked. The device now clears `archived` whenever a turn starts in
the session or a terminal attaches to it, and publishes the change, so the row returns to the
device's Active list by the ordinary layout rule. See 6.3.

**2026-09-12 A16 — the attachment follows the CLI, and a session that never held a message is
removed.** A Claude CLI started through the shim registered its channel under the id it was started
with; `/resume` and `/clear` inside the TUI moved the process to another id without telling anybody,
so the live session showed `control: "none"` while an empty row stayed `shared`, and every CLI start
that was quit or resumed away from left an empty session behind for good. The device now takes a
`SessionStart` hook from the CLI (installed through a settings file the shim passes), moves the
attachment to the id the hook names, and removes any terminal-origin session with no events and no
transcript the moment its terminal leaves it. The device sends `session.removed` on its own
initiative for those; the gateway and the apps already treat the frame as authoritative. See 4.4
and 7.

**2026-09-12 A17 — a terminal-held session reports what the terminal chose.** A `shared` Claude
session cannot take `session.set` (the channel has no such method), so the apps drew nothing where
the model, permission-mode and effort pickers go, and a person on the phone could not tell which
model the terminal was running. The device now reads the three values from the transcript Claude
Code writes for itself and publishes changes as `meta`; apps show them as values that cannot be
changed from there. Nothing new on the wire: `Session` already carried the fields and `meta` already
carried the updates. See 4.4, 5.11, 9.2 and 9.3.

**2026-09-12 A18 — work another application owns is not a session.** The daemon's `thread/list`
is the history of every Codex thread on the machine, and the device turned all of it into sessions:
the ChatGPT desktop app's scheduled automations and chats showed up in the apps as terminal
sessions, one of them `control: "terminal"` for as long as that app held its rollout open, none of
them anything a person had started at a terminal. The device now reads the `originator` and
`source` the index carries and publishes only threads its own daemon holds or a terminal started;
the rest are that application's and are removed if they were ever published. Nothing new on the
wire: `session.removed` already exists and the apps already honour it. See 4.4 and 9.2.

**2026-09-12 A19 — a held message is a queue entry until the CLI takes it.** A message sent into a
running attached Claude turn was published at once as a `user_message {delivery: "pending"}`, so
its `first_seq` pinned it in the middle of the turn — between the tool call that was running and
the rest of the answer — while the terminal drew it after the turn, where Claude Code read it. The
device now holds such a message as a `queue` entry only and emits the block when it injects it, as
A14 already did for a steered Codex message; `delivery: "pending"` is gone from the protocol and
`fixtures/events/user_message.pending.json` with it. See 5.2, 4.4 and 9.

**2026-09-12 A20 — a question Claude Code asks in an attached session can be answered from an app.**
The channel never relays `AskUserQuestion` (the CLI relays only tools that need no user
interaction, and its permission reply carries no answers), so a shared session showed the question
as a running tool call and the composer queued whatever was typed. Claude Code runs a
`PermissionRequest` hook beside its own dialog and takes whichever answers first, so the device now
installs one for `AskUserQuestion` in the settings file the shim passes, raises the `question`
block from it with `needs_input`, feeds the first `session.answer` back as the tool's answers, and
resolves the block `by: "terminal"` when the dialog was answered there. `question` gains an
optional `by`; `session.answer` is no longer `unsupported` on a shared session. See 5.8, 4.4 and 9.

**2026-09-13 A21 — a speed tier beside the model, the effort and the permission mode.** Codex offers
a faster tier per model (`serviceTiers` in its catalogue, `priority` named "Fast") and the TUI
toggles it with `/fast`; the apps could not. `AgentInfo` gains `speeds`, `Session` and `meta` gain
`speed`, `session.create` and `session.set` accept it, and the device maps it to the agent's own
setting. Nothing else changes; an agent that lists no speeds draws no control.

**2026-09-13 A22 — a device is updated from an app.** Bringing a device to a new client meant
re-running the installer on each machine. The gateway now reports the wheel it serves by its
SHA-256 (`GET /api/config` `client`), every device reports the build it runs (`hello`
`client_build`, `Device.client_build`), and `device.update` asks a device to fetch exactly that
build, install it and restart. `Device` gains `update_state` and `update_message`; the device
gains the `update.failed` frame. See 3.2, 4.1, 6.3, 7 and 9.

**2026-09-13 A23 — a host is paired by scanning.** The only way to pair was to mint a code in an
app and type it into the host's terminal, which is awkward from a phone. A host may now ask the
gateway for a claim token (`POST /api/pairing/requests`), print it as a QR code, and long-poll
for the outcome; a signed-in app claims the token (`POST /api/pairing/requests/{token}/claim`),
the gateway mints the ordinary pairing code for that host and hands it back to the poll, and
enrolment proceeds exactly as before, `pairing.progress` included. The QR encodes
`<public_origin>/pair#<token>`, which the web app honours too. See 3.1, 3.3 and 9.1.

**2026-09-13 A24 — accounts: every person on a gateway has their own devices, sessions and
settings.** The gateway had one password and one user, `admin`, and the devices table already
carried a `username` that nothing else honoured: every app socket received every device's
`device.updated` and every session's `session.updated`, and any signed-in socket could subscribe
to any session. The gateway now keeps accounts (`admin` from `RC_PASSWORD`, members by
registration while the admin allows it, or made by the admin), scopes every frame and every
listing to the account that owns the device, and refuses cross-account subscribes and forwards as
`not_found`. `User` gains `role`; `HealthResponse.auth` gains `registration_open`;
`LoginRequest.username` is required; `POST /api/register` and `POST /api/password` are new, and
3.9 gives the admin the account routes. Devices, `rc-client` and the device socket are unchanged:
a pairing code was always the account's, and so the device it enrols. See 3.1, 3.2, 3.9, 4.10, 6.1,
8.14 and 9.

**2026-09-13 A25 — three more agents: Grok Build, Cursor and pi.** The protocol never named a
closed set of agents, and nothing on the wire changes: an `AgentInfo` describes what each one
offers and a session of any of them is driven through the same frames. What is new is the
device's knowledge of three more CLIs and two shapes the apps had not met — an agent with no
permission modes (pi) and one with no effort levels (Cursor) — so 4.2 and 4.3 say what those
empty lists mean, and the worked examples in `fixtures/objects/` show them. Grok Build is driven
over its ACP JSON-RPC and its terminal sessions are mirrored from the update log it keeps; Cursor
is driven in its print mode one turn at a time, with approvals brokered by its hook; pi is
driven over its RPC mode. See 1.1, 4.2, 4.3, 9.2 and 9.3.

**2026-09-14 A26 — Cursor withdrawn; pi attaches, asks and takes images through an extension the
device installs.** Two changes to A25, nothing else on the wire. First, `cursor` is no longer an
agent id a device reports: the Cursor adapter is withdrawn until it can be exercised against a
signed-in Cursor, so 1.1, 4.2 and 4.3 drop it and `fixtures/objects/agent.cursor.json` is gone.
A UI that still meets the id renders it generically, as 1.1 has always said. Second, pi is no
longer the agent with nothing to ask and no terminal presence. The device ships a pi extension —
installed by `rc-client pi setup` into pi's global extension directory and loaded by the device
into every session it starts — and that extension is what `attach: "extension"` names in 4.2: a
pi session a person starts in a terminal registers itself with the device and is a `shared`
session (4.4) that an app can read, write to, stop and re-model, exactly as a Codex TUI under the
shared daemon is. The same extension gives pi the three permission modes of 4.3 by blocking a
tool call until an app or the terminal answers, so `permission_modes` is no longer empty for pi
and `approval` blocks appear on its sessions; pi's `prompt` takes images, so pi gains the
`attachments` capability and `shared_attachments`. `fixtures/objects/agent.pi.json` is the worked
example. See 1.1, 4.2, 4.3 and 4.4.

**2026-09-14 A27 — slash commands from the apps.** A terminal offers `/compact`, `/review`, a
project's prompt templates and its skills the moment `/` is typed; the apps could send only prose.
`AgentInfo.capabilities` gains `commands`, 4.11 defines `Command`, and 6.3 gains `session.commands`
(the list a session offers now) and `session.command` (run one, echoed as a `user_message` under the
request's id, outcome in the stream). Grok Build and pi hand the command to the agent as the text of
a turn, which is how their agents interpret it; Codex has no such path, so the device maps each entry
of a fixed table to the app-server method it stands for and reports information as a `tool_call`
block titled with the command. Settings, lifecycle and terminal ergonomics are never commands.
Claude does not list the capability: a channel carries user text and nothing else. See 4.2, 4.11,
6.3, 9.2 and 9.3.

**2026-09-14 A28 — Grok Build attaches through its leader process.** A `grok` started in a terminal
was mirrored from its update log and reported `terminal`, so the apps could only watch it. Grok
Build has a leader — one backend process per machine that its TUI joins when the person's
`~/.grok/config.toml` has `[cli] use_leader = true`, started on demand by whichever client comes
first — and a `session/load` from a second client of that leader joins the session the TUI is in,
with every update fanned out to every client, prompts from any client run in the one conversation
and drawn by the TUI, `session/cancel` and `session/set_config_option` acting for all, and
permission prompts sent to every client. `AgentInfo.attach` gains `leader`; Grok reports it with
`attach_ready` read from the person's configuration (the flag on and no sandbox profile, since the
leader refuses sandboxed sessions and exists only once a client has started it), `shared_interrupt`
and `shared_settings` true and `shared_attachments` false. 4.4 gains the `origin`/`control` table
for a Grok session on the leader, keyed on Grok's own session registry because the leader reports
nothing when a TUI exits; `session/close` is never sent for a session a terminal registered, because
it unloads the session for everyone. `rc-client grok setup` turns the flag on in the person's
configuration, in place. `fixtures/objects/agent.grok.json` is the worked example. See 4.2, 4.4,
5.7, 9.2 and 9.3.

**2026-09-14 A29 — dictation polish through an operator-configured model.** Speech is immediate
and comes with fillers, false starts and references that made sense with the screen in front of the
speaker; sent as-is, a transcript makes the agent guess. The gateway operator may now configure one
OpenAI-compatible base URL and key for polishing dictation — the only model credential the gateway
ever holds, used on an app's request and for nothing else. `hello` and `GET /api/config` gain
`polish {enabled}`; 3.5 gains `GET /api/polish/models` (the provider's models) and `POST /api/polish`
(`PolishRequest`: the dictated text, a model, a strength `moderate` | `strong`, a language hint and up
to twenty recent conversation messages the app already shows; `PolishResponse`: the text only). The
gateway stores nothing and forwards nothing to a device. 8.15 fixes the app behaviour: the setting is
the user's and off by default; dictated words land at once and the dictated span alone is replaced
when the answer arrives, one undo away; a send while polishing sends the words as dictated. See 1,
3.5, 8, 9.1 and 9.3.

**2026-09-14 A30 — words another agent put into a Claude conversation are not the person's.** Claude
Code files a teammate's message and a background task's notification as user turns, so a mirror
that trusted the role showed them as terminal input and a phone read a teammate's JSON as its
owner's words. `user_message.source` and `turn_started.trigger` gain `agent`: the device publishes
such a row with `source: "agent"` and a text reduced to who reported and what they said, drops every
`<system-reminder>` block and every envelope, and starts the turn with `trigger: "agent"`; apps draw
the bubble muted with the caption "from another agent". Tool results and `isMeta` rows are unchanged.
See 5.2, 5.9, 9.2 and 9.3.

**2026-09-14 A31 — the gateway states the oldest iOS app it supports.** The web app is served by
the gateway and the device client is updated from the apps, but the iOS app is installed on its own,
so a gateway upgraded past what an installed app can talk to used to fail in ways the phone could not
explain. `GET /api/health`, `GET /api/config` and `hello` gain `apps` — `apps.ios.minimum_version`
as `major.minor.patch` and an optional `apps.ios.update_url` — and 8.16 fixes what an older app
does: a blocking "Update required" screen with the two versions, a button to the URL and Sign out,
and nothing else. A gateway raises the minimum in the same release that breaks compatibility and
leaves it alone for an additive change. See 3, 6, 8, 9.1 and 9.3.

**2026-09-15 A32 — Claude's compaction summary, interruption markers and typed slash commands.**
Claude Code files three more things as user turns that are not a prompt: the summary it writes after
compacting the context (`isCompactSummary`, hidden in its own view), the marker it leaves when the
person interrupts a turn (`[Request interrupted by user]`, `…for tool use`), and the record of a
slash command typed at the terminal (`<command-name>` with `<command-args>`). A mirror that trusted
the role published the first two as the person's words and started a turn on the interruption, so
a phone showed a page of summary as something its owner typed, and a "Turn failed" for a turn nobody
ran when the CLI was then restarted. Nothing changes on the wire: the summary produces no block and
the `compact_boundary` row the `notice` of 5.13, as a Codex compaction already does; the
interruption produces no block and ends the turn with `stop_reason: "interrupted"`; and the slash
command is the person's, `user_message {source: "terminal"}` with the command and its argument as
typed, once, though the CLI records `/compact` twice, and it starts no turn. The CLI's own reply to
a command (`<local-command-stdout>`) stays out of the timeline and ends a running turn. See 5.2,
5.13 and 9.2.

**2026-09-15 A33 — how an agent is signed in, and what is left of its quota.** A person with
several machines could not tell from a phone which of them ran Claude Code on the Max account and
which on a key through a relay, nor how much of a five-hour or weekly window was left before starting
work there. `AgentInfo` gains `accounts`: one `AgentAccount` per credential the agent holds on the
device — the vendor (`provider`), `account` or `api_key` (`method`), the plan and tier the vendor
records, the email, and for a key the third-party host it goes to — read from the agent's own files,
never from a session. The reply to `device.agents` adds `limits` to every account the device could
read them for: `AgentLimit` rows of `window_minutes`, `used_percent`, `resets_at` and an optional
model `scope`, from Anthropic's OAuth usage endpoint for Claude Code and for pi's Anthropic sign-in,
and from `account/rateLimits/read` on the shared daemon for Codex; Grok Build exposes none. `hello`
and `agents.updated` carry accounts without limits. Apps open a device from its row and draw the
accounts and the meters. See 4.2, 6, 9.2 and 9.3.

**2026-09-15 A34 — a turn ends when the CLI says so, and another agent's words sit on the agent's
side.** Two things seen on a phone during a long Claude Code turn. First, three messages sent from
the phone while the turn ran were each shown with "will be re-sent" and the warning that the
terminal took them as data, although the CLI had read every one: Claude Code writes each content
block of one assistant message as a row of its own, and a mirror that read a text-only row as the
end of the turn injected the held messages into a turn that was still running. The device now reads
a turn's end from the row's `stop_reason` — `tool_use` means the turn goes on whatever the row
holds — and, when an injection is absorbed a second time, publishes the bubble as `delivered`
beside the warning, because `absorbed` promises a re-send that will not come. Second, a teammate's
report was drawn in the person's own bubble, muted: apps now draw a `source: "agent"` message on
the agent's side, as a muted block captioned "from another agent", and their Simple detail level
hides it with the agent's other workings. Nothing changes on the wire. See 5.2, 9.2 and 9.3.

**2026-09-17 A35 — a session the usage limit stopped resumes itself when the limit resets.** A
Claude Code or Codex turn that ran into the five-hour or weekly window ended, from a phone, like any
other: the device read the CLI's 429 row as a completed turn and showed the vendor's sentence as the
agent's words, and the work waited until someone noticed. The device now reads the limit from the
agent's own signal and ends the turn with `stop_reason: "error"` and `limit {window_minutes,
resets_at}`; with the account's `resume_after_limit` on — a preference the gateway stores and every
app and device of the account reads the same, off by default — it schedules a resume a minute after
the reset, keeps it across restarts, and sends one fixed sentence into the session as `user_message
{source: "resume"}` under `trigger: "resume"`, so the terminal or the device-run agent continues
where it stopped and brings its own subagents back. A terminal closed before the time means the
person is done: the resume is dropped and said so. Each step is a `resume` event in the timeline
and a snapshot in `Session.resume`, which the apps draw above the transcript with a way to move the
time and a way to cancel; a person's own message cancels it too. New: `Preferences`,
`GET`/`PATCH /api/preferences`, `hello.preferences`, `preferences.updated`, the device frame
`preferences`, `session.resume_set`, `session.resume_cancel`, the `resume` event, `LimitStop`,
`SessionResume`, the `resume` trigger and source, and three push kinds. See 3.2, 3.7, 4.4, 5.2,
5.9, 5.15, 6, 6.4, 7, 7.2, 8, 9 and 10.

**2026-09-18 A36 — a device keeps itself current.** Bringing a device to the gateway's wheel was a
person's job: the row said "Update available" and someone had to press Update, on every device,
after every release, and nobody should have to know which client a machine runs. The gateway now
does it: when a device's `hello` carries a `client_build` that is not the served build, the gateway
sends `device.update {build}` on its own account (`from: "gateway"`), retries while the device is
busy with a running session, gives up on `update.failed` or a five-minute silence and leaves the
failure for a person, and tries again by itself only when the served build changes. Apps drop the
client version and its build from the device row and the device page alike, drop "Update
available", and offer **Retry update** only on a failed device; "Updating…" and "Update failed ·
<message>" stay. Nothing changes on the wire: the
request, its replies and `Device.update_state` are A22's. See 3.2, 4.3, 6.3, 7, 8 and 9.

**2026-09-18 A37 — a folder can be made where a session will work.** The directory picker could
only choose a directory that existed, so starting a project in a new folder meant a terminal first.
`device.mkdir {device_id, path, name}` asks the device to make one directory, `name`, inside a
directory it listed, and replies with the new directory's listing as `device.dirs` would, so the
picker stands in it at once and its choose action picks it. One level, as `mkdir` would; a name is
one path component with no `/`, no leading `.` and at most 255 bytes; `conflict` when something is
already there. Rule 19 says how a picker offers it. New: `device.mkdir`, forwarded by `device_id`,
and its reply. See 6.3, 8 and 9.

**2026-09-18 A38 — a device row opens a terminal.** Remote control of an agent is most of what a
person wants from a machine they are not at, but not all of it: sometimes there is a shell command
to run, and until now that meant SSH from somewhere else. A device now offers a **terminal**: the
device starts the person's login shell in a pseudo-terminal and streams its bytes through the
gateway to the one app connection that asked — no SSH, no listening port, no key, the device dials
out as before and the gateway relays without reading. New requests `terminal.open`,
`terminal.input`, `terminal.resize`, `terminal.attach`, `terminal.close`; new device frames
`terminal.output` and `terminal.exited`, addressed to one connection by `to`; a gateway-originated
`terminal.detach` when that connection is gone, after which the device keeps the shell ten minutes
for an `attach`; `hello.terminal` and `Device.terminal`. Four terminals per device, 16 KiB frames,
64 KiB scrollback. Rule 20 says what the apps do: tapping a device row opens it, the row's menu
reads Rename · Retry update · Show quota · Revoke, and a phone's terminal carries a key bar. This
reverses the v1 decision not to ship a terminal emulator. See 4.1, 6.3, 7, 7.3, 8 and 9.

**2026-09-19 A39 — archiving a session the device drives closes it.** `session.archive {archived:
true}` was documented as stopping the session, and did so for a Claude or pi session, whose process
the device owns; a Codex thread on the shared daemon was only unsubscribed and a Grok session on
the leader only unrouted, so both stayed loaded, a running turn ran on, and the thread's next word
pulled the row back out of the Archive (A15). The frame is unchanged. Its meaning is now the whole
of it: the device interrupts the turn, ends what it holds for the agent — the Codex thread is
archived in Codex as well, the Grok session closed on the leader, which is safe because no
terminal is in a `remote` session — and only then marks the session archived, stopped and unowned,
in one reply and one publish; nothing the agent says meanwhile revives it. The apps call the
action **Close** and ask first only while the session is working. See 6.3, rule 21, 9.2 and 9.3.

**2026-09-21 A40 — the device types into an attached Claude Code terminal.** A `shared` Claude
session could only take messages and answer prompts: the channel has no method for settings or
commands (A17, A27 left Claude out), and a phone could not change the model, the effort, or compact
the context of a session running in the terminal. The shim that already starts every attachable
Claude Code now runs it inside a pseudo-terminal the device owns, and the device types into that
terminal what the person would type — `/model` and `/effort` through their own pickers, confirmed
for this session only, and `/compact` — only while the terminal is idle and nobody is typing there,
confirming each change from the transcript before it replies. `AgentInfo` gains
`shared_settings_keys`, so an agent can say that some settings are the device's to change on a
`shared` session and others still the terminal's; Claude reports `shared_settings: true` with
`["model", "effort"]` and the `commands` capability. Apps draw a listed setting as a picker and an
unlisted one as the value the terminal set. See 4.2, 4.4, 6.3, 9.2 and 9.3.

**2026-09-22 A41 — the Settings preferences are the account's.** The Settings screens kept their
choices in the browser and on the phone, so polish turned on in one place stayed off in another and
the person set every device by hand. The account already had one preference on the gateway (A35);
`Preferences` now carries the Settings screen's own — `language`, `stt_language`, `polish_enabled`,
`polish_model`, `polish_strength`, `timeline_detail` — all optional, absent until set, written with
the same `PATCH /api/preferences` and published with the same `preferences.updated`, the latest
write to reach the gateway winning everywhere within the round trip. An app writes its own value
up once for a field the account has not set, so nothing changes for the person on the day of the
upgrade. What belongs to a device stays there: notifications, the app lock, the transcription
backend, the terminal font size, list folds. See 3.2, 9.1 and 9.3.

**2026-09-22 A42 — Stop on an attached Claude Code terminal.** A message the CLI has taken cannot be
recalled, in the terminal or from a phone; what the terminal has is Escape, which stops the turn.
A `shared` Claude session could not offer it — a channel cannot interrupt — so its Stop was the one
control missing beside Codex, pi and Grok. The pseudo-terminal of A40 types Escape as the person
would: `session.stop` on such a session presses it only while a turn is running and no approval or
question is on screen (those are answered, not escaped; the request answers `conflict` until they
are), and does nothing when no turn runs. Claude reports `shared_interrupt: true` whenever it
reports `shared_settings`. Nothing new on the wire. See 4.4, 6.3 and 9.2.

**2026-09-28 A43 — editing a queued message.** A queued message could be removed but not changed,
and the web app drew every one of them above its composer. An app now takes an entry out with
`session.queue_remove`, so the device cannot deliver words that are still being changed, edits its
text in the composer, and sends it back with `mode: "queue"` and the new optional
`session.send.queue_ts` — the `ts` the entry had. The device holds such a message under that `ts`,
in front of every entry queued later, so the edit keeps its place however the queue moved
meanwhile, and a queue is always in `ts` order; a message without `queue_ts` joins the end as
before. `queue.pending[]` gains `attachments`, the number of files a held message carries: the files
stay on the device and no frame brings them back, so apps offer Remove for such an entry and not
Edit. Both fields are optional; a device without them queues an edited message at the end. See
5.12, 6.3 and 9.

**2026-09-28 A44 — the gateway detects the dictation language.** The composer offered a dictation
language on both apps, `auto` among them, and the gateway validated it against `STT_LANGUAGES`. The
gateway's providers detect the language themselves, so a choice there only adds a control and a
way to be wrong; a phone's own recogniser cannot detect it and must be told. The gateway now has
its provider detect the language of every transcription, ignores a `language` a client sends —
the `/ws/stt` query parameter, the `POST /api/stt/transcribe` field — and reports `stt.languages`
as `["auto"]`, kept for the apps that read it; `STT_LANGUAGES` is gone. `stt_language` is the
language a phone that recognises speech itself listens for, Chinese when unset, and an `auto`
written before this amendment reads as unset. Apps draw no dictation-language control for gateway
transcription and offer the recogniser's languages without Automatic where the phone transcribes.
Nothing else changes on the wire. See 3.2, 3.5, 3.8 and 9.

**2026-09-28 A45 — the gateway states the oldest Mac app it supports.** A macOS app joins the web
and iPhone apps: the web app's screens drawn natively, installed on its own like the iPhone app,
and so able to fall behind the gateway it talks to in the same way. `apps` gains `macos`, the same
shape as `ios` — `minimum_version` and an optional `update_url` — and the Mac app holds itself to
that entry alone, with the "Update required" screen of 8.16 below it. The two minimums move
separately: a gateway change that only an older Mac app cannot follow raises `apps.macos` and leaves
the iPhone app alone. Absent, as on every gateway older than this amendment, it states no
requirement. Nothing else changes on the wire. See 3, 6 and 9.

**2026-09-30 A46 — the gateway states the oldest Android and Windows apps it supports.** Two more
separately installed apps join: an Android app, the iPhone app's screens drawn for Android, and a
Windows app, the Mac app's screens drawn for Windows. Each can fall behind the gateway it talks to
as the iPhone and Mac apps can, so `apps` gains `android` and `windows`, the same shape as `ios` —
`minimum_version` and an optional `update_url` — and each app holds itself to its own entry alone,
with the "Update required" screen of 8.16 below it. The four minimums move separately. Absent, as
on every gateway older than this amendment, an entry states no requirement. Nothing else changes
on the wire. See 3, 6 and 9.

**2026-10-03 A47 — a session that stopped working and waits for you is marked until someone looks.**
A session whose dot went from green to amber gets a red dot, and the apps' icons count those
sessions. The mark is the gateway's, because only the gateway sees every
transition while every app may be closed, and because opening the session on one app should clear
it on all of them: `Session.unseen` is set when the device moves a session from a running turn to
waiting
and cleared by `session.seen`, by the session working again, or by its archiving or removal. Every
push carries the count as `rc.badge` (APNs `aps.badge`), and a badge-only APNs notification follows
a change no push carried. Only a `running` turn
brings the mark (a session that only started has done nothing to look at), and the conversation in
front of the person draws no dot and is not counted while its `session.seen` is on its way.
Additive: an app that predates it ignores the field, sends no
`session.seen` and shows no dot; a gateway that predates it sends no field, which reads as false.
See 3.7, 4.4, 6.2, 8 and 9.

**2026-10-03 A48 — terminal Codex threads can come from an app-server.** Codex 0.160.0 reports
terminal TUI threads with `originator: "codex-tui"` and `source: "vscode"`, so A18's provenance
filter rejected them before the device could publish a session. The device now accepts that exact
pair alongside its own originators and the existing `cli` and `exec` sources, both in the daemon's
index and in rollout discovery. `vscode` by itself remains foreign, and a non-string `source`
still excludes every subagent, including one whose parent is a terminal TUI. Nothing changes on
the wire: the existing session summaries and events reach every app unchanged. See 4.4 and 9.2.

**2026-10-14 A49 — Claude Code and Grok Build take attachments as files the prompt names.** A
shared Claude session refused every attachment, because a channel carries text alone, and Grok
Build took none on any session, because it answers `promptCapabilities.image: false`. The device
now writes each attachment of a `session.send` for either agent to a folder of its own on the
device and appends `Attached files:` and one `- name: path` line per file to the prompt, which the
agent opens with its own file tool (Claude's `Read`, Grok's `read_file`, both of which read
images); on a `shared` Claude session the injected words carry the list, and on Grok's leader the
prompt does. Claude reports `shared_attachments` true whenever `attach_ready` is; Grok adds
capability `attachments` and reports `shared_attachments` true. The `user_message` keeps the typed
words and `attachments` metadata, a held message's `queue` entry counts its files as before (A43),
and a prompt the device reads back from the agent's own record is published the same way, so no
path ever reaches an app; a title or an `error` the agent words itself names a file where it quoted
a path, and the leader's echo of the device's own prompt is recognised and never becomes a second
block. The agent reading one of the session's own files with its own read tool is
allowed by the device without an `approval` block. The files are kept a week at most and deleted
sooner when the message can no longer reach the agent or the session is deleted. The same change
states what A39's close does with held messages: they are discarded, never sent, and the device
publishes an empty `queue` with the close. Nothing else changes on the wire;
`fixtures/objects/agent.claude-attach.json` and `agent.grok.json` show the new values. See 4.2,
4.8, 6.3 and 9.

**2026-10-19 A50 — a device keeps the name it was given.** The gateway wrote `hello.name` over a
device's stored name on every connection, and a device names itself from its own configuration or
its hostname, so a rename made in an app lasted only until the next reconnect or gateway restart. A
device's name is now the `name` it enrolled with until `PATCH /api/devices/{device_id}` renames it:
the gateway accepts `name` in `hello` without applying it and stores the rest of `hello` as before.
Changing the name in a device's own configuration after enrollment no longer renames it; an app's
Rename does. `name` stays required in `hello`, so devices and gateways on either side of this
amendment exchange the same frame, and a gateway that predates it still overwrites a rename.
Nothing else changes on the wire. See 3.3, 4.1, 7 and 9.1.

**2026-10-20 A51 — a phone and a terminal take turns in one Claude session.** With the person typing
in the terminal of an attached Claude session and a phone sending into it, the phone's message could
be lost. Claude Code hands a channel message that arrives during a turn to the model as untrusted
data it is told not to act on, and the device, reading turns from a transcript that lags and hides
some of them, injected into turns already under way — one a hidden message from another Claude
session started, one the person had just begun, one Claude Code's own queue began while a dialog
held it — and gave up after one re-send; it also held every message while any subagent worked,
though the CLI takes one between turns as readily as the person's. The settings file the shim passes
now also carries `UserPromptSubmit`, `PostToolBatch` and `Stop` hooks, which tell the device where
each turn stands as the CLI knows it. `AgentInfo` gains the optional `shared_steer`: when it is true
and a `shared` session is running, `session.send` with `mode: "auto"` is steered — the device
answers `accepted: "steered"` and the message reaches the turn at its next step. For Claude that is
the turn's next batch of tool calls, where the hook hands the message to the model in the words
Claude Code uses for a message the person types mid-turn and the terminal shows a line saying so; a
turn with no batch left takes it as it ends, written into the channel while the `Stop` hook holds
the CLI, so it starts the next turn beside anything the person queued, as the CLI batches the
person's own queued messages, and its block is emitted when that turn starts, after the turn it
follows has ended. A message the device injects while the CLI still holds an earlier one — written
at a turn's end, or queued behind a dialog — is answered `accepted: "sent"` as before, and its block
is emitted when its own row shows rather than as it goes in, so the bubbles keep the order they were
sent in. A `Stop` hook never ends the turn the apps see: somebody's own `Stop` hook can send the
turn on, and the transcript says when it ended. The `user_message` is emitted when the
model takes the message (A14): for a batch, once the transcript shows the hook's context, beside
whatever the CLI folded into the turn at the same boundary. What it folds in — the person's prompt
queued in the terminal, a teammate's message, a task's notification — is published there as the same
words would be had they started a turn (A30): `source: "terminal"` for the person's, `agent` for the
others, nothing for what such a turn would not show; the device's own message folded that way is the
absorption below, never a second block. The blocks of one boundary come in the order the model reads
them, which the CLI decides whichever was sent first: what it folded in first from Claude Code
2.1.294 on, what the hook handed over first in 2.1.291. An app treats a running session as steering,
in its wording and its sending alike, when the agent's `capabilities` has `steer`, or when the
session is `shared` and the agent reports `shared_steer: true`, and as queueing otherwise; an app
that predates this amendment ignores the field, says the message will be queued, and draws the
bubble the `steered` reply describes. Claude reports `shared_steer` true whenever `attach_ready` is
and its Claude Code runs the `PostToolBatch` hook, 2.1.291 and later, and lists no `steer`
capability, because a session the SDK drives cannot steer; an older Claude Code gets no such hook
and steers nothing. The terminal proxy hands each CLI the settings file its version can load and
tells the device which, and a session steers, or delivers at its `Stop`, only with the hook. The
resume prompt of A35 is the device's own and never steers. `mode: "queue"` keeps A19 and A43: a held
message goes in as a turn ends, one per turn, now while its `Stop` hook runs. The CLI's own turn
decides injecting: subagents still at work keep the session `running` for the apps but never hold a
message back. A message the CLI absorbs anyway — it took the message as data, so the model has read
it once as such — goes in again until its own row appears: one sent with `mode: "auto"` at the
running turn's next batch of tool calls when the session steers, any other as a turn ends. So does a
message the CLI removes from its queue without running it, its queue cleared for one, and one the
CLI went past without its own row ever coming: a turn hook ran after it was written or handed over,
and the CLI's queue never showed it; the time gone by alone never makes a message lost. Should the
CLI run such a message after all, the copy still waiting to go in is withdrawn. Each such message
counts against three re-sends, except one written at a turn's end and absorbed by that turn going
on: when the session steers, that one goes in at the turn's next batch of tool calls whatever its
`mode`, uncounted, and when it cannot, as a turn ends, against a separate ten. Past its limit, the
device publishes an `error` naming the message under the bubble's own `block_id` — the app request
id of A12, or the queue's `queued_id` — so apps put the failure in the bubble's place, live and in a
history alike; this replaces A34's `delivered` beside a warning. A message the person pulls back
into the input box is theirs, and the device does not send it again. A turn that a row the CLI hides
starts — a teammate's or another session's message under `origin.kind: "peer"` in an
`<agent-message>` or `<cross-session-message>` envelope, or a turn the CLI continues by itself —
counts as a turn, and the former's words are published as A30 says. A CLI started without these
hooks, under somebody's own `--settings`, keeps the earlier behaviour apart from subagents and the
re-sends.
`fixtures/objects/agent.claude-attach.json` shows the new field. See 4.2, 5.2, 5.14, 6.3, 8 and 9.

**2026-10-20 A52 — Grok Build's questions reach the apps.** Grok asks through its own ACP request
`_x.ai/ask_user_question` rather than a tool the device could only watch, and the device refused
that request with an error, which failed the tool at once — in a TUI's session on the leader too,
where the first reply of any client settles a prompt for all of them. The device now raises each
question as an ordinary `question` block with `needs_input`, on a session it drives and on one a
TUI shares, and hands Grok the first `session.answer`, keyed by each question as Grok worded it and
with a single choice as its label, though an app sends every choice as a list. A question past the
fourth is not shown and goes unanswered. One another client answered first resolves
`by: "terminal"`, with `answers` only where Grok's own summary names the question and option labels
exactly; one declined, or out of Grok's own time, expires, and so does whatever is still open when
its turn ends. A device never settles a prompt another client may answer by itself; only an app's
answer, or Stop, does. It sends nothing for a session it does not route, nothing when it lets go of
a session a terminal shares, and nothing to a prompt it cannot answer, such as a plan approval, on
such a session, and an approval there has no deadline of the device's own. An option's `preview` and
the terminal's plan-mode interview choices are not carried. Nothing changes on the wire. See 4.4,
5.8 and 9.

**2026-10-20 A53 — Grok Build's quota reaches the device page.** A33 found no window Grok Build
exposed, but the TUI's `/usage` reads the account's allowance through an extension method of Grok's
own agent, `_x.ai/billing`, which the agent answers by asking xAI under the session it is signed in
with. A device joined to the leader (A28) now asks the leader the same for a `device.agents` reply,
and the current usage period it names, a week or a month, becomes one `AgentLimit`:
`window_minutes` the period's length, `resets_at` its end, and `used_percent` the share xAI
reports, which it leaves out while none of the period is used, so a reply without it is 0%. A reply
with no such period has no window and no error; a refusal, or a leader that does not answer in
time, is one line of `limits_error`. The device sends no token of its own and starts no agent for
the read, so Grok's quota is read only in leader mode (`rc-client grok setup`), and there it always
shows: while the device is still joining the leader, `limits_error` says so in one line. With leader
mode off, `limits` stays absent with no `limits_error`, as before. Nothing changes on the wire. See
4.2 and 7.2.
