# SideCode

Control the coding agents you already run — Claude Code, Codex, Grok Build and pi — from your
phone or a browser. SideCode (随码 in Chinese) is a self-hosted control plane: a **gateway** on
your own server sits between the **developer machines** where the agents are installed and the
**apps** you carry. Add a machine with one pairing command, see every session on it, and drive a
session the way you drive a chat: send text or dictate it, watch answers, thinking, tool calls and
diffs stream in, approve or deny a tool, answer a question, stop a turn, queue the next one. A
`claude` or a bare `codex` you started yourself in a terminal is not just mirrored: the device
attaches to the live session, so you can pick it up from a phone without killing what it is
running. Model credentials never leave the machine; the gateway routes and indexes but never runs
an agent.

This repository holds everything a self-hosted deployment needs:

- **The gateway** (`gateway/`), which you run with `docker compose up -d`.
- **The device client** (`client/`), `rc-client`, installed on each developer machine with one
  command the gateway serves.
- **The web UI** (`web/`), served by the gateway itself.
- **The wire protocol** (`protocol/`), which every client speaks.

The SideCode apps for iPhone, Android, Mac and Windows are distributed separately: the iPhone app
on the App Store, and every app from <https://sidecode.cc/download>. They connect to a self-hosted
gateway as well as to the official one. An official hosted service is also available at
<https://sidecode.cc>.

## Architecture

```
      apps                        gateway (your server)              developer machines
 ┌───────────────┐         ┌───────────────────────────┐         ┌─────────────────────┐
 │    web app    │──┐      │  your reverse proxy  :443 │      ┌──│ rc-client daemon    │
 └───────────────┘  │      ├───────────────────────────┤      │  │   claude  (SDK)     │
 ┌───────────────┐  │      │  rc_gateway         :8787 │      │  │   codex   (JSON-RPC)│
 │ SideCode apps │──┴─────►│  auth · devices · index   │◄─────┤  └─────────────────────┘
 │ iPhone·Android│         │  replay buffer · STT      │      │
 │ Mac · Windows │         │  push · web · install.sh  │      │  ┌─────────────────────┐
 └───────────────┘         └───────────────────────────┘      └──│ rc-client daemon    │
                                                                 │  (another machine)  │
                                                                 └─────────────────────┘

   https /api · wss /ws/app      SQLite in DATA_DIR                wss /ws/device
```

Devices dial out only — nothing listens on them. Apps never reach a device directly. The gateway
reads only the envelope fields it needs to route a frame and forwards the rest opaquely. The wire
contract is [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md), and it is normative for every client.

Package, image and protocol names keep the project's original `remote-control` and `rc` prefixes
(`rc-gateway`, `rc-client`, `~/.rc-client`, the `rc_session` cookie), so existing installations
keep working.

## Repository layout

| Path | What it is |
| --- | --- |
| `protocol/` | The wire protocol: `PROTOCOL.md`, JSON Schema, and fixtures every component decodes in its tests |
| `gateway/` | `rc_gateway`, the server. Python 3.12, FastAPI, SQLite, WebSockets |
| `client/` | `rc-client`, the device daemon and its installer. Python 3.12, `claude-agent-sdk`, Codex app-server |
| `web/` | The browser app. React 19, Vite, TypeScript, hand-written CSS |
| `docs/` | The documentation |
| `docker-compose.yml`, `.env.example` | The one-command stack and every setting it takes |

## Quick start on a server

Prerequisites: Docker with the Compose plugin, and a reverse proxy you run yourself — Nginx Proxy
Manager, Traefik, plain nginx — holding the public hostname and its certificate. The stack publishes
the gateway on one host port and nothing else; see [docs/DEPLOY.md](docs/DEPLOY.md#reverse-proxy).

```sh
git clone https://github.com/sidecodeai/sidecode.git && cd sidecode
cp .env.example .env
```

Edit `.env` and set two values:

```sh
PUBLIC_ORIGIN=https://rc.example.com   # the exact origin apps will use, no trailing slash
RC_PASSWORD=<a long random password>   # the password of the operator account "admin"
```

`GATEWAY_PORT` (`8787`) and `GATEWAY_BIND` (`0.0.0.0`) decide where the stack publishes the gateway;
point your proxy at that address and put its own source network in `TRUSTED_PROXIES`.

Then bring the stack up and open the web UI:

```sh
docker compose up -d
curl https://rc.example.com/api/health
```

Sign in as `admin` with `RC_PASSWORD`. Go to **Devices → Add device**, pick the macOS or Linux tab,
and run the printed one-liner on the machine where your agents live:

```sh
curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M
```

The pairing code is single use and expires after ten minutes. The modal walks through the handshake
live — gateway ready, device handshake, detected agents — and the device appears in the list when it
connects. Now open **Sessions → New session**, choose the device, the agent, a working directory and
an optional first message, and start working. A SideCode app signs in to the same gateway by its
address and an account on it.

To also drive the `claude` sessions you start in your own terminal, install the shim on that machine
and open a new shell:

```sh
~/.rc-client/venv/bin/rc-client shim install
```

Every interactive `claude` in a new shell then shows a one-time development-channels confirmation —
choose "I am using this for local development" — and appears in the apps as **terminal · attached**,
with a live composer.

Terminal **Codex** sessions need no shim and no confirmation. The installer already brought up the
shared app-server daemon they run inside, unless you passed `--no-codex`, so on a fresh machine there
is nothing more to run. To check it, or to set it up on a machine installed with `--no-codex`:

```sh
~/.rc-client/venv/bin/rc-client codex status   # binary, socket, a real handshake, supervision
~/.rc-client/venv/bin/rc-client codex setup    # idempotent: bootstrap the daemon and supervise it
```

`codex setup` installs the standalone Codex only when `~/.local/bin` is already on your `PATH`, so it
never rewrites a shell profile, then runs `codex app-server daemon bootstrap` and installs its own
supervision, because Codex's bootstrap leaves none and the daemon would not survive a reboot. On a
healthy machine `rc-client status` then reports the daemon as `healthy (loaded)`.

Terminal **pi** sessions need the device's pi extension, which the installer sets up when `pi` is
already installed. To check it, or to set it up after installing pi later:

```sh
~/.rc-client/venv/bin/rc-client pi status   # installed, current, or missing
~/.rc-client/venv/bin/rc-client pi setup    # idempotent: copy the extension into ~/.pi/agent/extensions
```

Terminal **Grok Build** sessions attach through Grok's own *leader*, one shared backend process per
machine that every `grok` joins once `[cli] use_leader = true` is in your `~/.grok/config.toml`.
The installer turns that flag on when Grok is installed, unless you passed `--no-grok`; to check
it, or to turn it on after installing Grok later:

```sh
~/.rc-client/venv/bin/rc-client grok status   # the flag, the leader socket, registered terminals
~/.rc-client/venv/bin/rc-client grok setup    # idempotent: set [cli] use_leader = true, in place
```

One rule applies from then on: start Codex as a bare `codex`. Any `-c`, `--enable`, `--disable` or
`--dangerously-bypass-approvals-and-sandbox` on the command line makes the CLI run its own private
app-server, which the device cannot join, and the session falls back to read-only mirroring.
Everything you would have set with `-c` is set from the apps instead.

Full deployment reference, including every `.env` variable, TLS options, upgrades, backups, push
notifications and troubleshooting: [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Local development

Run the gateway from a source checkout on port 8787:

```sh
cd gateway && uv sync
PUBLIC_ORIGIN=http://127.0.0.1:8787 RC_PASSWORD=devpassword DATA_DIR=/tmp/rc-data uv run rc-gateway
```

`rc-gateway` takes no flags; it reads the repository root `.env` when one exists, and otherwise the
environment. It serves `web/dist` at `/` when that directory has been built, and a placeholder page
when it has not.

The web app:

```sh
cd web && npm ci
npm run dev        # Vite on 5173, proxying /api and /ws to 127.0.0.1:8787
npm run dev:mock   # the same server plus a bundled mock gateway; sign in with: dev
npm run build      # typecheck, then build to web/dist
npm test           # vitest
```

A device against that gateway, in a scratch home so your real one is untouched:

```sh
cd client && uv sync
export RC_CLIENT_HOME=/tmp/rc-dev
uv run rc-client enroll --gateway http://127.0.0.1:8787 --pair RC-XXXX-XXXX
uv run rc-client run
```

Plain `http://` is accepted only for loopback and private addresses; everything else must be https.
The checks every change has to pass, and a smoke flow for a release, are in
[`docs/VALIDATION.md`](docs/VALIDATION.md).

## What works

- **Devices** — one-line install for macOS and Linux, single-use pairing codes, live enrollment
  progress, rename, revoke. Installed agents and their models, permission modes and effort levels
  are detected on the device and reported to the apps.
- **Sessions** — create one on any online device with a working directory, an optional git worktree,
  a model, a permission mode and an effort level. Archive them, and change the model, permission
  mode or effort mid-session.
- **Agents** — Claude Code through `claude-agent-sdk`, Codex through its app-server JSON-RPC
  interface, either as a private process per session or as a second client on the machine's shared
  app-server daemon, Grok Build over its ACP JSON-RPC, and pi over its RPC mode with an extension of
  the device's own loaded into every pi process. Every agent is normalised to one block timeline,
  so the UI has no agent-specific code paths.
- **Slash commands** — typing `/` in the composer lists what the session's agent offers, the way its
  terminal does.
- **Timeline** — streamed assistant Markdown, collapsible thinking, one-line tool rows that expand
  to input and output, diffs with per-file counts, todo snapshots, approval cards, question cards,
  turn markers and usage.
- **Turn control** — stop, queue while a turn runs, steer a Codex turn mid-flight, or interrupt and
  send. Queued messages are visible, editable and removable.
- **Terminal sessions** — a `claude`, `codex`, `grok` or `pi` you started in a terminal is
  discovered and shown in the same timeline, and attached where the agent allows it: you send into
  the live CLI, its permission prompts arrive as approval cards, and whichever side answers first
  wins. Nothing is killed and nothing is resumed.
- **A terminal on a device** — open a shell on any online machine from the apps, inside the same two
  WebSocket connections.
- **Voice** — dictation through the gateway's speech-to-text proxy, streamed as 16 kHz PCM16 with
  live partial transcripts, and optional polish of the dictated text through an OpenAI-compatible
  model of yours. The result is always an editable draft; sending stays a separate action.
- **Voice assistant (experimental)** — a call in the web app and in the apps that tells you what
  your sessions are doing and what is waiting for you, and drafts a message to one of them. A
  message is sent only after the assistant has read it back and you have said yes, or pressed
  Send. It is off until you switch it on in `.env` with an OpenAI key of your own, and only the
  usernames you list may use it; see [docs/LIVE_ASSISTANT.md](docs/LIVE_ASSISTANT.md).
- **Accounts** — an operator account, member accounts with their own devices and sessions, optional
  self-registration, and account deletion by the operator or, through `DELETE /api/account`, by
  the account itself.
- **Notifications** — Web Push from your own gateway for approvals, questions, completed turns and
  errors; payloads carry no prompt or output text. What the native apps receive on a self-hosted
  gateway is described in [docs/DEPLOY.md](docs/DEPLOY.md#push-notifications).

**One consequence worth knowing.** A remote session must never be approved by the machine on behalf
of someone who is not looking at it, so remote Claude sessions do **not** load your user-level
`~/.claude/settings.json`. User-scoped MCP servers, skills, hooks and `permissions.defaultMode` do
not apply to them; project and local settings do. Change it with `[claude] setting_sources` in
`~/.rc-client/config.toml` — see [`docs/CLIENT.md`](docs/CLIENT.md).

## Security model

- **The device dials out.** Nothing listens on a developer machine, so no inbound port, no SSH key
  and no tunnel is involved.
- **Credentials stay where they belong.** The agents' model credentials never leave the device, and
  the gateway cannot run an agent. The only model API keys it can hold are the operator's own, for
  speech-to-text, dictation polish and the experimental voice assistant, and each is used for that
  feature alone.
- **The gateway forwards, it does not read.** It parses the routing envelope and the session
  summaries it indexes; message text, tool input and tool output pass through opaquely.
- **Accounts are separate.** Every device, session, pairing code and notification belongs to one
  account, and a request naming another account's resource is answered as if it did not exist.
- **Pairing codes are single use**, expire in ten minutes and are stored hashed. Device tokens are
  shown once at enrollment and stored hashed; revoking a device drops its socket and its sessions.
- **Browser sessions** use an HttpOnly, SameSite=Strict cookie and an `Origin` check on every
  cookie-authenticated write; native apps use a bearer token instead. Login is rate limited per IP.
  Issued sessions are recorded on disk, so signing out is durable and a restart does not quietly
  hand a revoked token back its access.
- **The machine cannot approve for you.** Remote Claude sessions skip user-level settings, so
  auto-approval configured on the device does not answer a request you never saw.
- **Notifications say nothing.** A push payload carries a device name and a reason, never content.
- **Logs redact.** Tokens, passwords, pairing codes, prompt text and tool output are never logged,
  and an email address is kept only as its domain.

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flow, the block timeline, storage, design trade-offs |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Deployment, `.env` reference, TLS, upgrades, backups, push, troubleshooting |
| [`docs/CLIENT.md`](docs/CLIENT.md) | The device daemon: installer, commands, services, mirroring, configuration |
| [`docs/WEB.md`](docs/WEB.md) | The browser app |
| [`docs/LIVE_ASSISTANT.md`](docs/LIVE_ASSISTANT.md) | The experimental voice assistant: what it does, what leaves the gateway, its call protocol, limits and removal |
| [`docs/DESIGN.md`](docs/DESIGN.md) | The interaction design the web app and the native apps share |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | The checks every change runs, and the release smoke flow |
| [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) | The normative wire contract |

## License

Licensed under the Apache License, Version 2.0; see [`LICENSE`](LICENSE).

SideCode builds on [cc-remote](https://github.com/muggle-stack/cc-remote), which is MIT licensed: the
gateway's forwarding core is adapted from it, and the device daemon reuses its hard-won details
about driving the CLIs. Both debts, with the MIT license text, are itemised file by file in
[`gateway/THIRD_PARTY_NOTICES.md`](gateway/THIRD_PARTY_NOTICES.md) and
[`client/THIRD_PARTY_NOTICES.md`](client/THIRD_PARTY_NOTICES.md).
