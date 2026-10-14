# rc-client — remote-control device daemon

`rc-client` runs on a developer machine, drives the coding agents installed
there (Claude Code and Codex), and answers requests forwarded from the
remote-control gateway. It dials out only: nothing listens on the device, and
model credentials never leave it.

It also picks up sessions you started yourself in a terminal. A Claude session
started through the installed `claude` shim, and any session started as a bare
`codex`, is *attached* rather than mirrored: the apps get a live composer and
the session's permission prompts, and the CLI keeps running throughout. Codex
goes further still, because the whole machine shares one `codex app-server`
daemon, so stop, settings and attachments work on a terminal session too.
Anything the device cannot attach to is mirrored read-only and, for Claude, can
be taken over while its terminal is idle. See "Attached terminal sessions" and
"Codex on the shared daemon" in `docs/CLIENT.md`.

## Install

The gateway serves a one-liner from its web UI:

```sh
curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M
```

That installs `uv`, a private Python 3.12 environment under `~/.rc-client`,
the `rc_client` wheel from the gateway, enrolls the device with the pairing
code, and registers the background service. Re-running upgrades in place.

Pass `--manual` to print the steps instead of running them, `--name` to choose
the device name, `--gateway` to override the origin, and `--uninstall` to
remove the service.

## Commands

| Command | What it does |
| --- | --- |
| `rc-client enroll --gateway URL --pair CODE [--name N]` | Redeem a pairing code and write `~/.rc-client/config.toml` |
| `rc-client run` | Run the daemon in the foreground |
| `rc-client status` | Print the device identity, paths and service state |
| `rc-client agents` | Print detected agents as JSON |
| `rc-client service install\|uninstall\|start\|stop\|status` | Manage the background service |
| `rc-client shim install\|remove\|status [--no-shell-rc]` | Manage the `claude` shim that makes terminal sessions attachable |
| `rc-client codex setup\|status [--no-install]` | Bring up and check the shared Codex app-server daemon |
| `rc-client channel` | The channel bridge Claude Code spawns; never run it by hand |
| `rc-client uninstall [--purge] [--no-shell-rc]` | Remove the service and the shim, and with `--purge` the data |

Exit codes: `0` success, `1` runtime failure, `2` usage error, `3` not enrolled.

## Layout

```
~/.rc-client/
  config.toml               gateway origin, device id, device token, name (0600)
  state/rc-client.sqlite3   sessions, history events, send idempotency, tail offsets
  state/attachments/        files received with a message
  state/claude-mcp.json     the channel server definition handed to Claude Code
  state/channel.sock        where channel bridges register
  bin/claude                the shim that starts an attachable Claude session
  logs/                     service stdout and stderr
```

### Optional settings

`enroll` writes the four required keys. Three sections may be added by hand; all
have safe defaults, and an unreadable or out-of-range value falls back to them.

```toml
[mirror]
max_sessions = 50   # how many terminal sessions to import, newest first
max_age_days = 14   # and how far back to look

[claude]
setting_sources = ["project", "local"]

[terminal]
enabled = true      # offer a shell to the apps (A38)
```

`mirror` bounds what a freshly enrolled device publishes. Without it a developer
machine would hand the apps every session it has ever run.

`claude` chooses which Claude settings files a remote session loads. The user
file is left out on purpose: `permissions.defaultMode: "auto"` or a
`PermissionRequest` hook there answers a permission request locally, which
cancels the request the remote decision arrives on, so the tool runs before
anyone could approve it. Adding `"user"` restores the machine's MCP servers and
skills, at the cost of letting those settings approve on the remote user's
behalf.

`terminal` decides whether this device offers a shell. With it on, which is the
default, an app may open the person's login shell in a pseudo-terminal and drive
it through the gateway; `enabled = false` is reported in `hello` and every
`terminal.*` request is answered `unsupported`.

`RC_CLIENT_HOME` overrides the directory. The background service is a launchd
user agent labelled `dev.remote-control.client` on macOS, and a systemd user
unit `rc-client.service` on Linux (run `loginctl enable-linger $USER` so it
survives logout).

## How it works

* **Gateway link** (`gateway.py`) — one outbound WebSocket to `/ws/device` with
  a bearer token, `hello` / `hello_ack`, ping/pong, a byte-bounded send queue,
  and 1, 2, 4, 8, 15 s reconnect backoff. Receiver, sender and watchdog race
  each other, so 60 s of silence tears the socket down even when no close frame
  ever arrives. The socket, and the enrollment request too, are dialled
  directly unless the device was enrolled with a proxy (`proxy` in
  `config.toml`, see `proxy.py`); the environment is never read at dial time,
  because the link is a tunnel to the operator's own gateway and the daemon's
  environment is a plist's or a unit's, not the person's.
* **Session hub** (`sessions/hub.py`) — creates sessions, routes every
  forwarded request, queues messages while a turn runs, and launches the queue
  at the turn boundary. `session.history` pages backwards with `before_seq` and
  forwards with `after_seq`, which is how the gateway backfills events produced
  while the link was down.
* **Channel** (`sessions/channel.py`) — the single place that assigns `seq` and
  `first_seq`, coalesces streaming deltas to at most one frame per block per
  80 ms, applies the protocol size bounds, persists history and publishes
  session summaries.
* **Adapters** — `agents/claude/` drives `claude-agent-sdk`; `agents/codex/`
  drives a private `codex app-server` over stdio JSON-RPC. Both translate their
  agent's output into the same block timeline.
* **Codex daemon** (`agents/codex/daemon/`) — one JSON-RPC connection, spoken as
  WebSocket over the machine's shared `codex app-server` control socket, that
  becomes the session index and the event stream for every Codex thread when the
  daemon is running. Falls back to the per-session adapter when it is not.
* **Terminals** (`terminal/`) — up to four login shells on pseudo-terminals,
  each streamed to the one app connection that opened it: output is coalesced
  for 16 ms into frames of at most 16 KiB with a rising `seq`, the last 64 KiB
  is kept so a reconnecting app can redraw, and a shell whose app is gone is
  kept ten minutes for an `attach` before it ends. Nothing that travels through
  a terminal is logged.
* **Mirroring** (`sessions/mirror.py`) — discovers recent transcripts under
  `~/.claude/projects` and rollouts under `~/.codex/sessions`, tails them by
  file size, and decides who controls a session from a process scan. In Codex
  daemon mode it handles only the threads that daemon cannot see.

## Development

```sh
cd client
uv sync
uv run rc-client --help
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy rc_client
```

Tests never call a real agent by default. To exercise the installed CLIs with
one short prompt each:

```sh
RC_REAL_AGENTS=1 uv run pytest -q tests/test_real_agents.py
```

Point the daemon at a gateway running locally with:

```sh
RC_CLIENT_HOME=/tmp/rc-dev uv run rc-client enroll \
    --gateway http://127.0.0.1:8787 --pair RC-XXXX-XXXX
RC_CLIENT_HOME=/tmp/rc-dev uv run rc-client run
```

Plain `http://` is accepted only for loopback and private addresses.
