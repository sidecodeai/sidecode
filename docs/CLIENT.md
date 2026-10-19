# The device daemon

`rc-client` runs on every machine where your agents live. It dials out to the gateway, drives the
locally installed Claude Code, Codex, Grok Build and pi CLIs, and mirrors the sessions you start
yourself in a terminal. Nothing listens on the machine and no model credential ever leaves it.

## What the installer does

The gateway serves the installer at `GET /install.sh`, with its own origin substituted into the
script, so the command you copy from **Add device** already points at your gateway. There are two
ways to pair, and they differ only in where the pairing code comes from:

```sh
curl -fsSL https://rc.example.com/install.sh | sh                          # pair by scanning
curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M  # pair with a code
```

It installs `uv` into `~/.local/bin` if it is not already there, installs Python 3.12, creates a
private virtual environment at `~/.rc-client/venv`, downloads the `rc_client` wheel from the
gateway, records which wheel that was, enrolls the device, registers the background service, starts
it, installs the `claude` shim, sets up the shared Codex daemon, installs the pi extension, turns on
Grok Build's leader mode (`rc-client grok setup`), and prints the agents it found. Re-running
upgrades in place.

It reports as it goes. A bold title and a one-line tagline open the run, then each step is a single
line: a braille spinner and a present-tense label while it works, a green `✓` and the result when it
is done. The step's own output is captured, not printed, so a successful run is a dozen quiet lines;
a step that fails prints a red `✗` and that captured output indented underneath, and stops there.
Enrolment is the exception and keeps its terminal: the QR code, the claim URL and the `Enrolled as …`
lines all pass straight through, with the `✓ Enrolled` line printed after them. The `claude` shim,
the shared Codex daemon, the pi extension and the Grok leader are never fatal — each one that does
not come up prints a dim `–` line naming the `status` command that explains it, and the install
carries on. The run ends with **Detected agents**, one line per agent with its version or
`(not installed)`, and a two-column list of the commands you are most likely to want next.

The colours, the spinner, the hidden cursor and the Unicode marks are for a terminal only. Piped
into a file or another program, with `TERM=dumb`, or with `NO_COLOR` set, the same lines print with
`ok`, `error` and `-` in place of `✓`, `✗` and `–`, no escape codes and no spinner, so a logged
install reads as plain text. Unicode marks additionally need a UTF-8 locale or a terminal known to
have one. Ctrl-C at any point clears the spinner line, restores the cursor and exits 130.

With `--pair` the code comes from **Add device** in an app and you type it on the host. Without it
the host asks the gateway for a claim token, prints it as a QR code with its URL underneath, and
waits up to ten minutes; scanning the code in the phone app, or opening the link in a signed-in
browser, mints the pairing code for your account and hands it straight back to the waiting host
(A23). Nothing is typed either way, and enrolment is the same request in both.

It refuses to run as root on macOS, refuses an unknown operating system or architecture, and refuses
a plain `http://` gateway unless the host is a literal loopback or private address — the wheel it is
about to download will run as a service, so the path has to be one nobody can sit on.

| Flag | Effect |
| --- | --- |
| `--pair RC-XXXX-XXXX` | The pairing code from an app. Omit it to pair by scanning instead |
| `--name NAME` | The device name shown in the apps until it is renamed there. Defaults to the hostname |
| `--proxy env\|URL` | Reach the gateway through an http or https proxy: `env` takes the one this host's proxy variables name, a URL names one and is exported for the installer's own `curl` and `uv` steps. The default dials directly; see "Reaching the gateway through a proxy" |
| `--gateway ORIGIN` | Override the origin baked into the script |
| `--manual` | Print the steps instead of running them, for a host without `curl` in the pipeline |
| `--no-shell-rc` | Do not put the shim directory in front of `PATH` in your shell startup file |
| `--no-codex` | Skip the shared Codex app-server daemon setup |
| `--no-grok` | Leave Grok's leader flag alone in `~/.grok/config.toml` |
| `--uninstall` | Stop and remove the service, keeping `~/.rc-client` |
| `-h`, `--help` | Usage |

Pairing codes are single use and expire after ten minutes, and so do claim tokens. If enrollment
fails, mint a fresh code or run the installer again.

## Commands

`rc-client` takes a global `--log-level` (`debug`, `info`, `warning`, `error`) and `--version`.

| Command | What it does |
| --- | --- |
| `rc-client enroll --gateway URL --pair CODE [--name N] [--proxy env\|URL]` | Redeem a pairing code and write `config.toml` |
| `rc-client enroll --gateway URL --scan [--name N] [--proxy env\|URL]` | Print a QR code, wait for an app to scan it, then enrol with the code it returns |
| `rc-client run` | Run the daemon in the foreground |
| `rc-client self-update --build SHA256` | Install the wheel the gateway serves, if it is that build, and restart the service |
| `rc-client status` | Print the device identity, how the gateway is dialled, paths, build and service state |
| `rc-client agents` | Print the detected agents as JSON |
| `rc-client service install\|uninstall\|start\|stop\|status` | Manage the background service |
| `rc-client shim install\|remove\|status [--no-shell-rc]` | Manage the `claude` shim that makes terminal sessions attachable |
| `rc-client codex setup\|status [--no-install]` | Bring up and check the shared Codex app-server daemon that makes terminal Codex sessions attachable |
| `rc-client pi setup\|status\|remove` | Install, inspect and remove the pi extension that makes terminal pi sessions attachable |
| `rc-client grok setup\|status` | Turn on Grok Build's leader mode in the person's own configuration, in place, and report the leader that makes terminal Grok sessions attachable |
| `rc-client channel` | The channel bridge Claude Code spawns; never run it by hand |
| `rc-client hook session-start\|permission-request` | The hooks Claude Code runs; never run them by hand |
| `rc-client uninstall [--purge] [--no-shell-rc]` | Remove the service and the shim, and with `--purge` the config, state and logs |

Exit codes: `0` success, `1` runtime failure, `2` usage error, `3` refused with nothing done — the
device is not enrolled, or `self-update` was served a wheel that is not the build it asked for.

After the one-line install, the executable is at `~/.rc-client/venv/bin/rc-client`. Add that
directory to your `PATH` to call it by name.

## Files

```
~/.rc-client/
  config.toml               gateway_origin, device_id, device_token, name, proxy   (0600)
  venv/                     the private Python environment the installer creates
  state/rc-client.sqlite3   sessions, events, request idempotency, tail offsets, pending resumes
  state/client-build        the SHA-256 of the wheel this client was installed from   (0600)
  state/attachments/        files received with a message, one folder each, kept a week at most
  state/claude-mcp.json     the channel server definition the shim passes to Claude Code
  state/claude-settings.json  the two hooks the shim passes to Claude Code
  state/channel.sock        where channel bridges register (see below)
  state/pi-extension.sock   where pi extensions register (see "pi")
  state/link.json           what the gateway link last said about itself
  bin/claude                the shim that starts an attachable Claude session
  logs/                     rc-client.out.log and rc-client.err.log (macOS)
  logs/update.log           what the last `self-update` did, and why it stopped if it did
```

`RC_CLIENT_HOME` moves the whole directory, which is how you run a second daemon against a test
gateway without touching your real one.

## Reaching the gateway through a proxy

The gateway link, enrollment, pairing and `self-update` all dial the gateway **directly** by default:
following the environment at dial time would route a private tunnel through whatever `HTTPS_PROXY`
happens to hold, and the daemon's environment is not yours anyway — it comes from a launchd plist or
a systemd unit. A host that has no other way out, a cluster login node behind an HTTP proxy being
the usual case, opts in once, at enrollment:

```sh
curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-7K42-QX9M --proxy env
rc-client enroll --gateway https://rc.example.com --pair RC-7K42-QX9M --proxy http://proxy.example:3128
```

`config.toml` keeps the answer as `proxy`, and it is only ever `""` — dial directly — or one
`http://` or `https://` URL, used for every dial. **`env` is not stored**: it means "work it out
here, now". At enrollment the host reads its own proxy settings with the standard library's rules —
`HTTPS_PROXY` for an `https://` gateway, which is what the `wss://` link uses, `HTTP_PROXY` only for
a plain-http one, nothing at all when `NO_PROXY` bypasses the gateway's host, and on macOS the
system network settings when no variable is set — and writes down the URL it found, or `""`. So the service environment never matters: the plist and the unit need no
proxy variables, and neither the daemon's link nor `self-update` depends on the shell you enrolled
from.
`enroll` prints what it chose (`proxy: direct`, `proxy: http://proxy.example:3128`), and so does
`rc-client status`, with any `user:password@` removed. Edit the key and restart the service to
change it.

Any other scheme is refused at enrollment rather than failing every dial later; "Network path" below
says why.

`install.sh --proxy URL` also exports `HTTPS_PROXY` and `HTTP_PROXY` for its own run, because `curl`,
`uv` and the wheel download read the environment and would otherwise never get far enough to enrol.
`--proxy env` changes nothing there — the variables are already set — and is passed on to `enroll`.

## Service management

**macOS** installs a launchd user agent labelled `dev.remote-control.client` at
`~/Library/LaunchAgents/dev.remote-control.client.plist`, with stdout and stderr in
`~/.rc-client/logs/`. It starts at login and restarts on failure.

The plist deliberately sets no `ProcessType`. It used to say `Background`, which puts the job in
macOS's lowest scheduling band and throttles its disk I/O; on a loaded machine that was enough to
stall the event loop for tens of seconds at a time and drop the gateway link. Left unset, launchd
treats the job as `Adaptive`. **An install made before this change keeps the old plist**: re-run
`rc-client service install` to pick it up, which rewrites the file and restarts the service.

The Codex daemon's own agent, `dev.remote-control.codex-daemon`, dropped `ProcessType` for the same
reason: it is the app-server every Codex round trip goes through, and throttling it throttles them.
An existing install picks that up on the next `rc-client codex setup`.

**Linux** installs a systemd *user* unit, `rc-client.service`, under
`~/.config/systemd/user/`. It runs with `NoNewPrivileges` and `UMask=0077`, restarts after five
seconds, and logs to the journal (`journalctl --user -u rc-client -f`). A user unit stops when you
log out unless lingering is enabled:

```sh
loginctl enable-linger $USER
```

The Linux path has not been exercised: neither `rc-client service install` nor the systemd unit has
been run on a real machine.

## A terminal on the device (A38)

A device row in the apps opens a shell on this machine. It is not SSH: the device dials out as it
always has and nothing here listens. `rc_client/terminal/` is the whole of it — `shell.py`, one
pseudo-terminal, and `manager.py`, at most four of them per device. `terminal.open {cols, rows}`
forks the person's login shell with `pty.fork()` — `$SHELL`, else `/bin/sh`, run as `-l` in the
home directory with the daemon's sanitised environment plus `TERM=xterm-256color` and
`COLORTERM=truecolor`; the child sets its own window size before `exec`, so the shell reads the
app's size rather than a fresh pty's 0×0. The master fd is read non-blocking off the event loop,
output is coalesced for 16 ms and published as `terminal.output {terminal_id, to, seq, data}`
frames of at most 16 KiB decoded, `seq` rising by one per frame, and the last 64 KiB is kept in a
ring; `to` is the app connection id the request arrived with (`from`), so the gateway hands the
bytes to that one socket. `terminal.input` decodes base64 (over 64 KiB is `bad_request`) and
writes it to the master; `terminal.resize` applies `TIOCSWINSZ` within A38's bounds. The child is
reaped with `waitpid(WNOHANG)` off the loop, and its end is one `terminal.exited {code}` to the
holder — or to the last holder after a detach, so an app that reconnects learns the shell is gone
rather than waiting for it (the gateway drops the frame if that socket is gone and asks once for a
detach, which is answered `{}`).

`terminal.detach` (which only the gateway sends, `from: "gateway"`) stops streaming — output
goes only into the ring — and starts a ten-minute keep-alive, after which the shell is closed as
`terminal.close` would close it: SIGHUP, then SIGKILL two seconds later. `terminal.attach` makes the
new `from` the holder, replies with the size and the ring as `scrollback`, and output resumes to it
with `seq` continuing. A fifth `open` is `conflict`, an unknown id `not_found` (`close` and
`detach` are idempotent and answer `{}`), and every terminal ends when the daemon shuts down.
`[terminal] enabled = true` in `config.toml` (the default) is announced as `hello.terminal`; with
`false` the device answers `unsupported` and the apps say so. Nothing that travels through a
terminal is logged — only that one opened and closed. `tests/test_terminal.py` runs the whole flow
on a real pseudo-terminal with `/bin/sh` and kills what it starts.

## Updating a device

Bringing a host to a newer client used to mean going to that machine and re-running the installer,
then pressing Update on a row (A22). The gateway now does it itself (A36): the device is asked the
moment it says `hello` with a build that is not the one the gateway serves, and a person is only
involved when that failed.

The installer writes the SHA-256 of the wheel it installed to `state/client-build`, and the daemon
reports it as `client_build` in every `hello`. The gateway reports the wheel *it* serves the same
way in `GET /api/config`. A client installed from a source checkout has no build file, reports
`null`, and is left alone. The device does not know or care who asked: a `device.update` from the
gateway (`from: "gateway"`) and one an app sends to retry a failure are answered and carried out the
same way.

`device.update {build}` is answered before anything is installed:

| Answer | When |
| --- | --- |
| `unsupported` | there is no build file, so this client was not installed from a wheel |
| `conflict` "already on this build" | the requested build is the one it is running |
| `conflict`, counting them | a session it drives is starting, running or waiting on you |
| `{accepted: true, from}` | otherwise, with the build it is leaving |

Accepting spawns `rc-client self-update --build <sha>` in its own session, so the updater outlives
the service restart it performs at the end. The updater downloads
`<gateway>/dist/rc_client-latest.whl` into `state/`, never `/tmp` — the file is about to run as a
service — and **installs nothing unless its SHA-256 is exactly the build that was asked for**; a
mismatch exits 3 and leaves the old client in place. On success it installs the wheel with the same
`uv` the installer used, rewrites `state/client-build`, re-runs `service install` and `shim install`,
keeping whatever `--no-shell-rc` choice your shell startup file already reflects.

**The restart is the updater's own last step wherever `service install` does not restart the
service itself.** On launchd, `install` boots the job out and back in, so the new code is what runs
the moment it returns. On systemd, `install` only rewrites and enables the unit, and the running
process keeps the code it started with, so without a restart an update of a Linux host ends in
"the device did not come back": the wheel is installed, the log says "updated to …", and the same
old process is still connected minutes later. `manager.install_restarts()`
says which kind this platform is, and where it is false the updater ends with
`rc-client service restart`, after the shim and the pi refresh and after its "updated to" line,
because the updater runs inside the unit's own cgroup and the restart takes it with it. The
restart is queued with `systemctl --user --no-block restart`, so the updater exits cleanly first;
the job is systemd's once queued, and the new process comes up on the installed wheel.

Then it refreshes everything else the wheel ships into the person's tools, so an update leaves the
device where a fresh install would: `pi setup`, but **only when a pi extension is already
installed** — a device that never had one keeps having none, and the updater says so in one line.
That step is the new `rc-client`'s own, as the service and shim steps are, so what lands in
`~/.pi/agent/extensions/` is the new build's extension; a failure there is printed to the log and
does not fail the update, because it leaves a stale extension rather than a broken client. The
updater touches nothing it did not put there: it never installs Codex, whose shared daemon the
restarted device brings up by itself, and it never turns Grok's leader mode on or off, which is the
installer's question and the person's answer.

Everything the updater prints goes to `logs/update.log`, never the gateway token. The daemon watches
the process it spawned: if it exits non-zero, the daemon is still alive to send `update.failed` with
the log's last line, and the app shows it on the device row. If the daemon never comes back at all,
the gateway gives up after five minutes and marks the device failed on its own.

## Agent discovery

Every agent the device can drive is a package under `rc_client/agents/<id>/` with a `plugin.py` that
exposes three names: `AGENT`, `detect(context)` and `build_runner(spec)`. `rc_client/agents/registry.py`
holds the only list of ids — `("claude", "codex", "grok", "pi")` — gathers detection concurrently in that
order and dispatches a new session to the right adapter; an id that is not in the tuple is answered
with `unsupported`, which is what a session frame for an unknown agent returns. Teaching the device a
new agent is a new package and a new name in that tuple: the daemon, the hub and the CLI are
untouched.

On start, and whenever an app calls `device.agents`, the daemon locates each CLI and probes its
version. Resolution order for `claude`:

`RC_CLAUDE_BIN`, then every `claude` on `PATH` in order, then `~/.local/bin`, `~/.claude/local`,
`~/.npm-global/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `~/node_modules/.bin`, `~/.yarn/bin`.
The device's own shim is never the answer: the service environment puts `~/.rc-client/bin` first on
`PATH` so `attach_ready` can see the shim, and one PATH walk — the same one the shim itself uses to
find what it wraps — skips that directory and every empty entry, which would mean the working
directory. A `claude` anywhere else that turns out to be a copy of the shim is rejected by its
contents.

For `codex`: `RC_CODEX_BIN`, then the standalone build at
`~/.codex/packages/standalone/current/bin/codex`, then `PATH`, then
`~/.codex/packages/standalone/releases/*/bin`, `~/.local/bin`, `/opt/homebrew/bin`,
`/usr/local/bin`, `/usr/bin`. The standalone build comes ahead of `PATH` deliberately: it is the
build the shared daemon runs, and a session this device starts has to be the same one. `CODEX_HOME`
moves the Codex home directory the daemon reads, and both standalone paths with it, exactly as the
official installer does.

For `grok`: `RC_GROK_BIN`, then `~/.grok/bin/agent`, then `grok` and `agent` on `PATH`, then
`~/.local/bin/grok`, `/opt/homebrew/bin/grok`, `/usr/local/bin/grok`. Grok's own launcher comes ahead
of `PATH` on purpose, and only executables are ever considered: `grok` is commonly a shell function
wrapping the binary with `--yolo`, and a device must never inherit that.

For `pi`: `RC_PI_BIN`, then `pi` on `PATH`, then `~/.local/bin/pi`, `~/.npm-global/bin/pi`,
`/opt/homebrew/bin/pi`, `/usr/local/bin/pi`. pi installs as an ordinary npm binary and ships no
launcher of its own, so there is no vendor directory to prefer ahead of `PATH`.

What each agent advertises:

| | Claude Code | Codex | Grok Build | pi |
| --- | --- | --- | --- | --- |
| Models | `default`, `fable`, `opus`, `sonnet`, `haiku`, most capable first. `default` means "do not pass a model"; the real id arrives from the SDK and is reported as `meta.model` | Read live from the CLI's `model/list` and cached for ten minutes | Read from `~/.grok/models_cache.json`, in the order the file lists them, hidden models skipped. No cache file means the two ids `agent.grok.json` names | Read from `pi --list-models` and cached for ten minutes: one `provider/model` id per row of the table it prints, in its order. Nothing logged in means no table, and the two ids `agent.pi.json` names |
| Permission modes | `default` (Ask before edits), `acceptEdits` (Auto-accept edits), `plan` (Plan mode), `bypassPermissions` (Bypass permissions) | `untrusted` (Ask for everything), `on-request` (Ask when needed), `never` (Never ask) | `default` (Ask when needed), `acceptEdits` (Auto-accept edits), `auto` (Auto mode), `dontAsk` (Deny unless allowed), `plan` (Plan mode), `bypassPermissions` (Bypass permissions) | `untrusted` (Ask for everything), `on-request` (Ask when needed, the default), `never` (Never ask). pi has none of its own; these are the device's, enforced by the extension it loads into every pi session (A26) |
| Efforts | `low`, `medium`, `high`, `xhigh`, `max` | Whatever the catalogue reports, clamped per model, from `minimal` to `ultra` | The union of the models' `reasoning_efforts`, weakest first, clamped per model when a session runs | pi's thinking levels: `off`, `low`, `medium`, `high`. `minimal`, `xhigh` and `max` exist too, but pi only says which of them a model exposes once a session is running, so they are advertised only when one of them is the person's own saved default |
| Speeds | none | The `serviceTiers` the catalogue lists, in catalogue order and with their own labels: `priority` ("Fast") today. `AgentInfo.speeds` is the union over every model; a model that lists none can run at no tier | none | none |
| Capabilities | `takeover`, `interrupt`, `queue`, `attachments`, `effort`, `history`, `worktree`, `commands` | `interrupt`, `queue`, `steer`, `history`, `worktree`, `attachments`, `effort`, `commands` | `worktree`, `interrupt`, `queue`, `attachments`, `effort`, `history`, `commands`. `attachments` although Grok takes no image in a prompt: the files are written to disk and the prompt names them (A49) | `worktree`, `interrupt`, `queue`, `steer`, `attachments`, `effort`, `history`, `commands`. `attachments` is images alone: pi takes them as base64 content on the prompt and refuses everything else |
| Attachment | `attach: "channel"`, `attach_ready` when the shim is installed and first on `PATH`; `shared_interrupt` and `shared_settings` follow `attach_ready`, because the device stops a turn and changes `model` and `effort` (`shared_settings_keys`) by typing into the terminal through the shim (A40, A42); `shared_attachments` follows it too, because the channel the shim loads carries the paths of a message's files (A49) | `attach: "daemon"`, `attach_ready` from a real handshake on the daemon socket, `shared_interrupt`, `shared_settings` and `shared_attachments` all true | `attach: "leader"`, `attach_ready` when Grok's own configuration has `[cli] use_leader` on and no sandbox (`rc-client grok setup` turns it on), so the next `grok` joins the machine's leader (A28); `shared_interrupt`, `shared_settings` and `shared_attachments` all true: a prompt names its files on the leader as anywhere else (A49) | `attach: "extension"`, `attach_ready` when the device's extension is installed in pi's global extension directory at this build, `shared_interrupt`, `shared_settings` and `shared_attachments` all true: the extension runs inside the pi process and can do all three |

Defaults follow the person's own configuration where there is one: pi's `default_model` and
`default_effort` come from `defaultProvider`, `defaultModel` and `defaultThinkingLevel` in
`~/.pi/agent/settings.json`, where `/model` and `/thinking` save them, and without that file the
first model of the catalogue and `medium` are used; Grok's `default_model` and
`default_effort` come from `[models] default` and `default_reasoning_effort` in `~/.grok/config.toml`,
and from the catalogue's own default when that file says nothing. Detection never starts an agent
process beyond `--version`.

An agent that is not installed is reported with `available: false` rather than hidden, so the apps
can say why a device offers only one agent.

### Accounts and quota (A33)

An agent runs on somebody's credentials, so detection also reports how each installed agent is
signed in: `AgentInfo.accounts`, one `AgentAccount` per credential the agent holds here, read from
the agent's own files and never from a running session. An agent that is not installed carries no
`accounts` key at all — the device did not look — and one that is installed and signed in nowhere
carries an empty list.

| Agent | Where the credential is read | What an account reports |
| --- | --- | --- |
| Claude Code | The login Keychain item `Claude Code-credentials`, read with `security find-generic-password -s "Claude Code-credentials" -w`, else `~/.claude/.credentials.json` where there is no Keychain. The email comes from `oauthAccount.emailAddress` in `~/.claude.json`. A key in `env.ANTHROPIC_API_KEY`, `env.ANTHROPIC_AUTH_TOKEN` or `apiKeyHelper` of `~/.claude/settings.json` and `settings.local.json`, or in the daemon's own environment, wins over an OAuth credential, and `ANTHROPIC_BASE_URL` names the `endpoint` when it is not Anthropic's own host | `provider: "anthropic"`, `plan` from `subscriptionType` (`pro`, `max`, `team`, `enterprise`), `tier` from `rateLimitTier` put into words: a leading `default_claude_` is stripped, underscores become spaces and the first letter is raised, so `default_claude_max_5x` becomes `Max 5x`; any other shape passes through unchanged, and a tier that says no more than the plan is left out |
| Codex | `~/.codex/auth.json` (honouring `CODEX_HOME`): `auth_mode` `chatgpt` is an account, `apikey` or a bare `OPENAI_API_KEY` a key. The plan and email are claims of the `id_token` beside it, whose payload is base64url-decoded and nothing else — never verified, never refreshed. The `endpoint` comes from the `[model_providers.<name>] base_url` that `model_provider` names in `~/.codex/config.toml`, or from `OPENAI_BASE_URL` | `provider: "openai"`, `plan` from `chatgpt_plan_type` (`pro`, `plus`, `team`, `business`) |
| Grok Build | `~/.grok/auth.json` (honouring `GROK_HOME`): an entry with `auth_mode: "oidc"` is an account and records its own email; any other entry with a `key` is a key | `provider: "xai"`, no plan |
| pi | `~/.pi/agent/auth.json`, one account per provider in the file's own order: `type: "oauth"` is an account, `type: "api_key"` a key. `openai-codex` is reported as `openai`; every other provider keeps pi's name | `provider` as pi names it, no plan |

**What the device never does.** It never writes a credential file, never refreshes a token, and
never logs, prints or puts on the wire a token, a key or a decoded id token. Reading the Keychain
through `security` is the only Keychain call there is, and it raises no new permission prompt
because Claude Code writes that item through the same tool. An endpoint is reported as a host and
never as a URL, because a person's own relay is exactly where a base URL could carry a secret in
its query string.

**Quota is read on request, not on a timer.** `hello` and `agents.updated` carry accounts without
`limits`, so re-detecting every quarter hour publishes nothing new; only the reply to
`device.agents` carries `limits`, `limits_error` and `limits_checked_at`, and the daemon stores that
reply's agents `without_limits()` so the refresh loop's diff stays quiet. Each check has a five
second budget:

- **Anthropic** (Claude Code, and a pi `anthropic` OAuth entry): `GET
  https://api.anthropic.com/api/oauth/usage` with the account's own access token. Its `limits[]`
  rows become one `AgentLimit` each — `session` a 300-minute window, `weekly_all` and
  `weekly_scoped` a 10080-minute one, with `scope` the model's display name — and a body without
  them falls back to `five_hour` and `seven_day`. A token whose `expiresAt` has passed is reported
  as a `limits_error` and never sent anywhere; only the person can refresh it, by opening the agent.
- **Codex**: `account/rateLimits/read` on the shared app-server daemon, which is where the `/usage`
  command reads it too. `primary` and `secondary` become the windows, seconds turned into
  milliseconds; an account with only a weekly window reports one. No daemon means the one line
  "Codex shared daemon is not running".
- **Grok Build**: nothing. The leader protocol of 1.0.30 is `session/*` and has no account or usage
  method, so `limits` stays absent with no `limits_error`, which is the wire's way of saying the
  vendor exposes no windows.

Every read is best effort: a missing, unreadable or malformed file is "signed in nowhere", and a
dead daemon, a failed request or a timeout is one line of `limits_error` against an account that is
still reported.

`session.set` validates `permission_mode` and `effort` against what the device advertises and
answers `bad_request` otherwise. `model` stays open, because model ids are the agents' own and a
cached catalogue can lag a release. `speed` is validated against the catalogue entry for the model
the session will run on: an agent with no tiers at all and a model that lists none both answer
`unsupported`, and a tier the model does not offer answers `bad_request`. A `speed` of null is the
standard speed, and a request that never mentions the key leaves the tier alone. A change that the running agent cannot apply live — an effort
change on Claude, sometimes a permission mode — is acknowledged immediately and applied on the next
turn.

## Sessions

`session.create` takes a device, an agent, a working directory, and optionally a model, a permission
mode, an effort, a speed tier, a first message and a title. With `worktree: true` the daemon runs `git worktree
add` on a new branch under `<repo>/.rc-worktrees/<slug>` and uses that as the working directory, so
an agent can work without touching your checkout.

A session id is the agent's own: for Claude it is the Claude session id, named up front and passed
to the SDK; for Codex it is the thread id; for Grok Build it is Grok's own session UUID; for pi it
is pi's session id, named up front with `--session-id` when the device starts the session. That is
what makes a session resumable and what lets a terminal session and a remote session be the same
thing.

The daemon queues a message that arrives while a turn is running and launches it at the turn
boundary, with `trigger: "queue"` so the UI can tell a queued turn from a typed one. Codex and pi
sessions advertise `steer`, so an app can redirect a turn in flight instead of queueing. A steer
carries the message's files too where the runner takes files into a running turn
(`AttachmentSteering`: both Codex paths and both pi paths); for any other runner a message with
files is queued, and so is one whose steer the agent refuses, files and all. A repeated
`session.send` with the same request id is recognised as a duplicate and never delivered twice.

The launch is a task of the session's own (`SessionHub.schedule_drain`). A turn's end — a runner's,
or the transcript read that says an attached terminal's turn is over — only schedules it and never
waits for it: the send holding the session's lock may itself be waiting on that turn's end (an
interrupt), and a settings change typed into a terminal holds the lock until the transcript reader
confirms it. One drain runs per session at a time, and one asked for while it runs makes it go round
once more. It hands over the oldest held message under the session's lock, as a send does, so a
message sent meanwhile waits behind it. A send that finds the session idle before the drain has run
hands the held message over first: an `auto` send to a session the device drives, and any send to
an attached terminal, which also holds the new message while anything is still held, so nothing
overtakes a held message. A Grok prompt is only over once its request has answered, which comes
after the `turn_completed` notification that ends its turn, so a Grok session drains then. A
session being closed (A39) drains nothing and stops a drain already under way, a deleted session's
drain is cancelled, and nothing is drained once the daemon stops.

The queue is kept in `ts` order, which is the order it drains in (amendment A43). An app edits a
queued message by taking it out with `session.queue_remove` and sending the new words back with
`mode: "queue"` and `queue_ts`, the `ts` the entry had; the device holds that message in front of
every entry with a later `ts` and behind any with the same, so it waits where it was however the
queue moved meanwhile. A message without `queue_ts` joins the end, stamped with the current time or
one millisecond past the last entry's `ts`, whichever is later, so no two entries share a `ts` and
an edit has exactly one place to go back to. A message the device puts back itself — an injection
the CLI absorbed, one the bridge refused — goes back under its own `ts` the same way, so an older
message sent back meanwhile stays in front of it. `queue_ts` never decides whether a message queues
— an idle session, and an attached terminal that can take the message, deliver it at once — and a
value that is not a non-negative integer is `bad_request`. The files of a held message stay on the
device; its `queue` entry says how many there are as `attachments`, only when there are any, and
apps offer such an entry Remove and not Edit. Every way into a queue — the hub's, an attached Claude
session's, and the device's own re-holds — goes through `rc_client/sessions/queue.py`.

The `user_message` the daemon publishes for a `session.send` carries **that request's id as its
`block_id`** (amendment A12), on all four paths: Claude through the SDK, Claude through a channel,
Codex through the shared daemon and Codex through its own app-server. The app has already drawn the
bubble under that id the moment the user pressed send, so the device's event replaces it rather than
adding a second one, and a retry of an unconfirmed send lands on the same block. A queued message
keeps the id from its `queue` entry through to the `user_message` that goes out when the queue
drains, and so does a steer. Messages the device originates itself — typed in a terminal, or
replayed from a queue entry that arrived without a request id — keep ids the device mints.

A steered message is the one exception (amendment A14). Codex does not read a message steered into
a running turn where it was sent but at its next step, after whatever it was already saying, and it
echoes the prompt as a `userMessage` item when it gets there. The device therefore holds the
`user_message` until that echo arrives and publishes it then, under the request id, so `first_seq`
places the bubble after the output that preceded it — where a terminal on the same thread draws it.
It carries the words that were typed and the files as `{name, mime, size}`, never the paths named
in the prompt Codex echoes (A49).
`accepted: "steered"` still comes back immediately, and the app keeps its optimistic row at the
bottom until the block lands. A turn that ends without ever reading the message publishes it at the
turn's end instead, with a `notice` of level `warn` when the turn was interrupted, so a steered
message is shown exactly once either way. This holds on both Codex paths, the shared daemon and the
private app-server.

Sending is arranged so that nothing the user waits for sits behind a round trip. The message is
published first and the agent is asked afterwards: a Codex send that has to resume its thread, and a
Claude send that has to restart the CLI to apply a new effort level, both show the bubble before
they start. A Codex thread whose first turn has not run refuses every `thread/resume`, so a refusal
is remembered and not repeated until a turn boundary on that thread says something has changed.

A Claude answer streams under the index its block has in the API message, and the CLI then hands
each finished block over alone, as a message of its own. The SDK translator
(`agents/claude/translate.py`) remembers where every streamed text and thinking block started and
gives the finished block that index, not its position in the one-block message, so a text block that
follows a thinking block the CLI does not show is still one block, streamed and then finished.
Taking the position instead would publish such an answer twice, once finished and once still
streaming.

`device.mkdir {device_id, path, name}` makes the folder a session will work in (A37): `fs.make_dir`
checks the name as one path component — non-empty, no `/`, no `\\`, no NUL, no leading `.`, at most
255 bytes of UTF-8 — and answers `bad_request` with a sentence a person can act on when it is not;
`path` has to be absolute and resolve to an existing directory (`bad_request`, `not_found`), and it
is resolved before the name is joined to it, so a name can never climb out of the directory the
picker showed. One `os.mkdir`, default mode under the umask, exactly one level: `FileExistsError`
(a directory *or* a file of that name) is `conflict`, `PermissionError` is `forbidden`, any other
`OSError` is `internal` with its `strerror`. The reply is `list_dirs` of the directory just made —
empty, with the same `recent` the picker already had — so the app stands in it at once and its
choose action picks it. The daemon runs it off the loop like `device.dirs`, with the same recents.

`session.history` pages backwards with `before_seq` and forwards with `after_seq`; the second form
is how the gateway backfills events produced while its link to the device was down.

An archived session is one the user folded away, not a different kind of session. The daemon reads
one back after a restart as `stopped` and leaves it there, but it clears `archived` and republishes
the summary the moment the session comes back to life (amendment A15): a turn starts in it, from an
app or from a terminal; a `session.send` arrives, including one that only joins the queue; or a
terminal attaches to it, through the Claude channel or by opening its Codex thread. The control a
revived session reports is the one the path that revived it gives it — `remote` when an app resumed
it, `shared` when a terminal attached — so it never comes back still claiming that nothing holds it.
Archiving closes a running agent, so archiving and then sending again is a restart of the CLI, not a
handover to a process that was still there.

**Closing a session the device drives (A39).** `session.archive {archived: true}` on a session with
a runner is a close, and `hub.archive` does it in one motion: it marks the channel `closing` first,
so `revive()` — the A15 path a turn ending or a status changing would otherwise take — returns at
once; interrupts the runner when it is busy; calls the runner's `shutdown()`; and only then writes
`archived: true`, `control: "none"`, `state: "stopped"`, clears the turn and the detail, publishes
the summary once and answers. The whole close is bounded (8 s); an agent that will not die is let
go of anyway, because the person asked for the session to end and a request that never answers
helps nobody. Messages held for the session are discarded, never sent: while the channel is
`closing` the end of the interrupted turn drains nothing, a drain already under way is stopped
before the agent is asked anything, and the close empties
the queue, deletes the folders of their attachments and publishes the empty `queue` before the
summary. A stopped drain deletes the files it was still writing, and those of a message whose
bubble it had already shown but whose prompt the agent never got: the turn that message opened
ends as `interrupted`, and a `notice` of level `warn` follows the bubble ("the agent was stopped
before it read your message"). Files already named to the agent are left to the age limit.
`archived: false` only clears the flag. `shutdown()` is the runner protocol's second ending beside
`close()` — `close` lets go of the agent, `shutdown` ends it:

| Runner | `shutdown()` |
| --- | --- |
| Claude (`ClaudeRunner`) | `close`: the SDK's disconnect ends the CLI — stdin EOF, then terminate, then kill |
| pi (`PiRunner`, and its terminal session) | `close`: the process is killed |
| Codex on the shared daemon (`CodexDaemonSession`) | tells the service the thread is closing, `turn/interrupt` when busy (waits up to 5 s for the turn to settle), `thread/archive {threadId}`, then `close` (`thread/unsubscribe`) |
| Codex standalone (`CodexRunner`) | `close`: its own app-server child is terminated |
| Grok on the leader (`GrokRunner`) | when no terminal is in the session (`control` is `remote`): `session/cancel` when busy (up to 5 s), then `session/close {sessionId}` — the one place that sends it, because it unloads the session for every client of the leader (A28) — then `detach`; a `shared` or `terminal` session only detaches |
| Grok private child | `close`: closing the child ends the agent |

**What Codex does on `thread/archive`, verified against Codex 0.154.0 in an isolated `CODEX_HOME`:**
it notifies `thread/archived` and `thread/status/changed` and **not** `thread/closed`; the thread
leaves both `thread/loaded/list` and `thread/list`, and its rollout moves to `archived_sessions/`;
`thread/read` still answers, `turn/start` says "thread not found", and `thread/resume` is refused
with "session … is archived. Run `codex unarchive …`"; `thread/unarchive` and then `thread/resume`
succeeds. So the daemon service keeps a `closing` set (a 30 s grace) that `_retry_attach`,
`_adopt_by_id` and `_adopt` respect, handles `thread/archived` by forgetting the thread in its index
without removing the session — a thread gone from `thread/list` would otherwise be taken for a
deleted one and the row removed — and, when an app writes to a closed session (A15), resumes it
through `thread/unarchive` first when Codex refuses the resume for being archived. `publish_control`
counts a live subscription as loaded, so a session reopened this way reads `remote` before the next
scan rather than `none`. End to end on the real hub: close → the row is archived, unowned and stopped
and the thread is unloaded; send again → unarchive, resume, a second turn completes, `control` back
to `remote`.

### Working means all of it

`docs/DESIGN.md` § "Working means all of it": a session is `running`
while any work it started is still under way — its own turn, or a subagent it spawned that has not
finished — and its turn does not end, so no `turn_completed`, no "Turn finished" push and no drain
of the held queue, until the last of it has. `needs_approval` and `needs_input` still mean exactly
that, whoever asked — the agent or a subagent working for it — and `idle` means everything has
finished. A held message therefore waits for the subagents as it waits for any turn.

**Claude Code.** The CLI writes each subagent's transcript under the session's own directory,
`<project>/<session id>/subagents/agent-<id>.jsonl`, beside an `agent-<id>.meta.json` that names the
agent and what it was asked and never says how it is doing. `agents/claude/subagents.py` is the one
rule: a subagent is working until the last row of its transcript is an assistant message that ended
its turn (`end_turn`, `stop_sequence`, `max_tokens`) or the API error that stopped it — a pending
`tool_use`, a tool result, a streamed block whose message never completed all mean it is still at
work — and a transcript nothing has written to for thirty minutes (`SUBAGENT_STALE_S`) counts as
abandoned, because nothing marks a subagent that was killed. `SubagentWatch` reads only each file's
tail, remembers the finished ones by `(mtime, size)`, and costs a `stat` per file per tail interval,
so even a directory of a hundred or more subagent files is checked in milliseconds. The rule
is deliberately not the parent's: the parent transcript is read row by row as it grows and a missing
`stop_reason` ends its turn (A34), while a large share of subagent files end on an
assistant row whose message never completed, and reading those as finished was the defect.

Where it applies. A mirrored or attached terminal session (`sessions/mirror.py`) is busy while its
transcript's turn is open **or** its subagents are working; the subagents are asked on every tail
interval even when the parent transcript has not grown, because the last one finishing writes
nothing to the parent the mirror could wait for, and only a change of answer is published. When the
terminal process is gone, so is everything it was running, and the flag is cleared with the turn. A
session this device drives (`agents/claude/adapter.py`) treats the SDK's `ResultMessage` the same
way: while subagents are working the completion is held and the turn stays open, polled every five
seconds (`SUBAGENT_POLL_S`); whatever the CLI streams meanwhile — the continuation when a subagent's
result comes back (A34) — belongs to that same turn, a later result replaces the held stop reason,
and the turn ends once with the whole duration. A turn the person interrupted is never held, and an
`interrupt` while one is held ends it as `interrupted` at once: the CLI's own turn is long over and
there is nothing on the SDK to stop. The `<task-notification>` row a finishing subagent lands in the
parent transcript starts no second turn while the first is still open.

**Codex.** A subagent runs in a thread of its own on the shared daemon. The thread object names its
parent — `parentThreadId` at the top level, and again as `parent_thread_id` inside the `source`
object a subagent carries (`SubAgentSource`: `thread_spawn`, `review`, `compact`,
`memory_consolidation`) — and `daemon/threads.py::parent_of` reads either. A thread whose parent is
a session of ours is remembered as that session's child (`daemon/children.py`, `ChildIndex`) the
moment `thread/started` or a `thread/read` describes it, and is never a session and never foreign
(A18 stands); everything it then says — `turn/started`, its items, `turn/completed`,
`thread/status/changed`, `thread/closed` — reaches the apps as one fact about the parent: whether
work the session started is still running (`ChildWatch`). The parent's own timeline is the second
source, for a daemon that never sends us a child's notifications: a `collabAgentToolCall` item
names the agents it addressed (`receiverThreadIds`) and carries the daemon's own `agentsStates`
(`in_progress`, `completed`, `failed`, … — anything but a finished word is work), and a
`subAgentActivity` item names the thread whose work it reports. A child that has said nothing for
thirty minutes (`CHILD_STALE_S`, checked on the scan interval) is let go, as an unwritten Claude
transcript is.

Loaded children are not necessarily working. Discovery and live status notifications require an
explicit `active` or `idle` thread status before updating the parent's child watch; unknown
statuses leave it unchanged.
An idle child therefore stays finished across index scans, and an `item/completed` notification
does not revive its work. Idle status notifications and periodic index scans also recover a
missing parent completion: after allowing the full notification to arrive, the runner reads the
thread's turns and settles only a confirmed completed, interrupted or failed turn. Live activity
and pending starts invalidate older reads; errors and usage are preserved, and duplicate end
notifications do not end a newer turn. Queue delivery runs outside the notification lock.

`CodexDaemonSession` holds the turn: when its own `turn/completed` arrives while children are
working, the completion's stop reason, usage and limit are kept (`HeldTurn`) and nothing is
published — the session stays `running` and `busy`, so held messages wait — until the last child
finishes, when the turn ends once with the whole duration. A parent `turn/started` or a status
going active while a turn is held is that same turn carrying on. An `interrupt` while held ends the
turn as `interrupted` at once: the parent has no turn for `turn/interrupt` to stop, and the children
are let go. A permission a child asks for still reaches only the terminal: the device is not
subscribed to child threads, and the daemon's routing of a subagent's approval is unverified.

**A session that speaks is alive.** A Codex thread the device holds a session for but no runner —
archived, or read back after a restart — that sends any notification is attached on that word:
`_retry_attach` revives the session (A15), resumes the thread, records a terminal in it
(`claim_terminal`, since the client that spoke is not this one) and publishes `shared`, and the
notification that triggered it is delivered to the new runner. Speaking is also proof the daemon has
the thread loaded, so the index being a scan behind no longer matters. Waiting for `thread/started`
— which a TUI's `codex resume <id>` may never produce for other clients (unverified) — is what left
a resumed session in the Archive while it worked. A thread that is merely in the loaded list, and
says nothing, stays where it was.

### Titles

A session has three possible names, in order of precedence:

1. **A title the user set**, through `session.create`, `session.set`, or `/rename` in a Claude Code
   terminal, which writes a `custom-title` row into the transcript. It sticks: nothing an agent
   produces replaces it, whichever way it was set. Which sessions were named this way is
   device-local state, kept in the registry rather than on the wire.
2. **The agent's own summary of the conversation**, which replaces a first-prompt title whenever it
   arrives and again whenever it changes. Claude Code generates one shortly after the first turn and
   writes it as an `ai-title` row, then writes another when a plan is accepted; Codex names a thread
   and announces it with `thread/name/updated`. The device reads the Claude rows in all three places
   a transcript is read: a session it drives itself, an attached session and a mirrored terminal one.
3. **The first line of the first message**, which is what a session is called until something better
   shows up. A first message of files and no words names the session after the files
   (`shot.png, notes.txt`), and then the agent's summary is not taken until the person has said
   something: the agent was given nothing but the files' paths, and its summary of them was made
   of words from those paths (A49). A typed slash command does not count as saying something.

Every source is capped at 60 characters. A change is published as a `meta` event and a republished
session summary, so the sidebars follow it live.

Both transcript rows are internal to Claude Code and documented as liable to change, so they are
parsed defensively: a row of an unknown shape, or one whose title field is missing, empty or not a
string, is read as "not a title" and never as an error. Nothing about the tail depends on them, so a
format change costs the titles and nothing else. When several titles arrive in one read they are
applied in order, which is what stops a generated title that lands after a rename from winning.

## Slash commands

`session.commands` and `session.command` (amendment A27) are answered by the hub and carried out by
the agent's runner. The hub gates on the agent's `commands` capability, refuses a command on a
`terminal` session with `conflict` as `session.send` would, resumes a `control: "none"` session
first, refuses one while a turn is running with `conflict` ("wait for the turn to finish"), and
remembers the request id so a retry is not run twice. The runner echoes the command as a
`user_message` under the request's id and reports what happened as ordinary events. A session with
no live process answers `session.commands` from the plugin's `commands(session)` — what the agent's
files say without starting anything — or with an empty list when the plugin has none. The commands
each agent offers, and how its runner carries them out, are in that agent's section below.

## Usage limits, and resuming after one

Claude Code and Codex stop a turn when the vendor's five-hour or weekly window is used up. The
device reads that stop from the agent's own signal, never from its words, ends the turn with
`stop_reason: "error"` and a `limit {window_minutes, resets_at}` (PROTOCOL 5.9), and publishes the
vendor's sentence as an `error` rather than as the agent's text (amendment A35).

Every reader lives in `rc_client/sessions/limits.py`, one per agent, so an agent that gains a signal
joins without a change to the wire:

| Agent | What is read | Where the window and the reset come from |
| --- | --- | --- |
| Claude Code, mirrored or shared | The transcript's assistant row with `isApiErrorMessage` and `apiErrorStatus: 429` (or `error: "rate_limit"`) | `quotaLimits.rateLimitType` (`five_hour` → 300, `seven_day` → 10080) and `quotaLimits.resetsAt`, in seconds |
| Claude Code, driven by the device | `ResultMessage.api_error_status == 429` | The same row, read from the end of the session's own transcript |
| Codex | `turn/completed` whose `turn.error.codexErrorInfo` is `usageLimitExceeded` | `account/rateLimits/read`, the window with the highest `used_percent` |
| Grok Build, pi | Nothing: neither reports a limit the device can read | — |

### The resume scheduler

`rc_client/sessions/resume.py` implements PROTOCOL 7.2. The account's `resume_after_limit`
preference arrives as the gateway's `preferences` frame, after `hello_ack` and on every change, and
is kept in the registry's `kv` table so a device that is offline at a limit stop still knows the
last value; absent means off. The pending resumes themselves live in the registry's `resumes` table
— `session_id, at, estimated, attempts, window_minutes, control` — so a restart keeps them, and one
loop looks at them every 30 seconds and once right after start, so a machine that slept through the
time fires on waking.

| When | What happens |
| --- | --- |
| A limit stop on a `remote` or `shared` session, switch on | `resume {status: "scheduled"}` at `resets_at` + 60 s, or now + the window (five hours when unknown) with `estimated: true`. A `terminal` session gets none: the device has no way in |
| The time passes, session idle | `resume {status: "fired"}`, then the prompt through the ordinary send path: `user_message {source: "resume"}` and `turn_started {trigger: "resume"}` |
| The time passes, a turn is running | `cancelled` — somebody took the session further |
| The time passes, scheduled as `shared` and the CLI is gone | `dropped` ("The terminal that owned this session was closed.") |
| The resumed turn hits the limit again | `rescheduled` with `attempts + 1`; after the third such turn, `dropped` |
| The person sends a message or runs a command | `cancelled` ("you sent a message") |
| The switch goes off | every pending resume `cancelled`, on every session |

The prompt is one constant, `RESUME_PROMPT`, verbatim from PROTOCOL 7.2 and never edited per
session; it is the one message the device writes for the person, so it never names the session
either. `session.resume_set` (at least a minute ahead, within eight days, `conflict` while a turn
runs or while the terminal controls the session) and `session.resume_cancel` (idempotent) answer
with the session. Every step is also a `resume` event, which `session.history` replays like a
`notice`, and the pending record travels as `Session.resume`.

## Terminal sessions

Every ten seconds the daemon scans for agent sessions it did not create:

- **Claude** — transcripts under `~/.claude/projects`, at most 200 files and nothing older than 14
  days. Growth is detected by file size, never mtime, because `claude --resume` touches mtime
  without appending a byte.
- **Codex** — rollout files under `~/.codex/sessions`, identified by the `session_meta` record that
  starts each one. That record also says whose thread it is, and only this device's own threads and
  a terminal's are mirrored: see "Work another application owns" below.
- **Grok Build** — update logs under `~/.grok/sessions`, for a `grok` the leader does not hold: see
  "Terminal sessions outside the leader" below.

pi needs no scan: its terminal sessions reach the device through the extension that
`rc-client pi setup` installs (see "pi").

An attached Claude session does not wait for the scan. A terminal attaches before its CLI has
written anything, and the transcript appears with the first row, so every tail pass (every two
seconds) looks for the transcripts attached sessions still lack, by session id and in any
project directory, and reads a new one from its first row. Attaching asks for a pass at once, and
so does a change typed into the terminal when it starts waiting for the rows that confirm it
(A40). Only attached sessions are looked for this way; the scan finds the rest.

A discovered session is published with `origin: "terminal"` and its rows are mirrored into the same
block timeline, with `source: "terminal"` on the user messages. The mirror keeps its place in each
file in the registry. While the device itself drives such a session — resumed from an app, or taken
over — its runner publishes the rows the session writes, and the mirror moves that place to the end
of the file on every tail pass, as the hub does once more when it lets go of the session (a close,
the daemon stopping); when the mirror takes the session back, once it is closed or after a restart,
it reads on from there, so those rows never reach the apps a second time as the terminal's. The end
of the file may fall inside a row the CLI is still writing, even inside one of its characters; the
reader skips what is left of that row like any line it cannot parse, and reads every row after it.
A process scan decides who owns the input, using `lsof` on macOS to find the process holding the
transcript:

| `control` | Meaning | What the apps do |
| --- | --- | --- |
| `terminal` | A live CLI process owns the session | Composer disabled, "Controlled by the terminal · Take over" |
| `shared` | A live CLI process owns it **and the device is attached** | Composer and approvals as for `remote`; see "Attached terminal sessions" |
| `none` | No process holds it; the session is resumable | Sending resumes it and control moves to `remote` |
| `remote` | This daemon is driving it | Normal |

Ownership is worked out for the whole scan round at once, and a process is credited to one session
only. A CLI started with `--resume` or `--session-id` names its session in its own command line, and
one started through the shim names it over the channel the moment its bridge registers; those are
the only two facts that say which conversation a process is in. The working directory is a last
resort, used only where one directory holds exactly one unidentified CLI and exactly one session
still without one. A home directory routinely holds a dozen finished sessions, and crediting the
same process to all of them made every one of them look like the live one. A shim-started CLI is
never matched by directory at all: it will name its session a moment later, and guessing in the
meantime is always wrong.

A `session.send` to a terminal-controlled session is refused with `conflict`. `session.takeover`
works only for Claude, and only while the terminal session is idle: the daemon sends SIGTERM to the
exact process it identified, confirms the release, and resumes the session itself. During a live
terminal turn it refuses. Codex advertises no `takeover` and needs none: a Codex session started as
a bare `codex` is `shared` rather than `terminal`, and the apps drive it in place. Only a Codex
started with configuration overrides falls back to `terminal`, and then quitting the CLI is the only
way to release it.

Two environment notes. A `claude` started from inside another Claude Code session inherits
`CLAUDE_CODE_CHILD_SESSION`, which turns transcript saving off, and the transcript is the only thing
the mirror can read. And a first run in a new directory shows a project-trust dialog that must be
answered before anything is written.

### What counts as the person's words

Claude Code files more than the person's prompts as `user` rows, and a mirror that trusted the role
showed all of them as terminal input. Three shapes are not the person (amendment A30). The message
one Claude session sends another arrives as a string beginning "Another Claude session sent a
message:" with a `<teammate-message …>` envelope around a JSON object; a background task's or
subagent's notification arrives as `<task-notification>` with a `<summary>` and sometimes a
`<result>`, its row marked `promptSource: "system"` and `origin: {kind: "task-notification"}` — a
person's own prompt carries `origin: {kind: "human"}`, and that kind and `channel` are the only two
that mean a person or this device; every other kind is another agent's; and
`<system-reminder>…</system-reminder>` blocks are appended to rows of every kind, a real prompt
included. `agents/claude/injected.py` classifies a user row before anything is published: the two
envelopes become `user_message {source: "agent"}` with the text reduced to who reported and what
they said — `<from>: <result>` for a teammate, the summary and result for a task — and the turn
they start carries `trigger: "agent"`; every system-reminder is stripped wherever it appears, so a
person's prompt that carried one keeps `source: "terminal"` without it, and a row that held nothing
else produces no block. Rows the CLI marks `isMeta` — the echo of a message this device injected
through its channel among them — stay out of the timeline as before, and a subagent's result that
returns through the `Agent` tool is a `tool_call` row already. The same classifier runs on the SDK
stream of a session this device drives, where the CLI hands the same rows over as user messages with
string content.

Four more shapes reach a transcript with the `user` role and are not a prompt either (amendment
A32). `agents/claude/markers.py` recognises each of them, and `injected.py` is never asked about
them: whose words a row holds is a different question from whether it holds any.

- **The compaction summary.** The CLI writes it as a user row marked `isCompactSummary` and hides it
  from its own view; a phone showed a page of it as something its owner had typed. It is no block at
  all. The `system` row before it, `subtype: "compact_boundary"`, becomes the `notice` of 5.13, in
  the words Codex already uses for the same event (`agents/base.py` `COMPACTION_NOTICE`). Both paths
  publish it: the transcript row for a mirrored session, the SDK's `compact_boundary` system message
  for one the device drives.
- **The interruption marker.** `[Request interrupted by user]`, and `[Request interrupted by user for
  tool use]` in the row that also carries the result of the tool it stopped, is what the CLI writes
  in place of the rest of a turn the person killed. It is no block, and it starts no turn — publishing
  it as one is what produced a "Turn failed" for a turn nobody ran. It ends the turn that was
  running: the tailer records `stop_reason = "interrupted"`, `sessions/mirror.py` hands it to
  `SharedControl.tick` in `sessions/shared.py`, and the next turn to start puts it back to
  `completed`. A tool result in the same row is published as usual.
- **A slash command typed at the terminal.** The CLI records it as `<command-name>` with the argument
  in `<command-args>`, and those are the person's keystrokes: `user_message {source: "terminal"}`
  whose text is the command as typed (`/model haiku`). It starts no turn, and it names no session:
  `models.py` `title_from_message` refuses a text that starts with a slash, because a command says
  what the person did to the CLI rather than what the conversation is about, and the CLI never
  titles from one either. A title the person types themselves keeps its slash. `/compact` is
  recorded twice — once as a plain `/compact` row, once as the tag — so a tag that repeats the last
  message the tailer published is dropped.
- **The CLI's reply to a command.** `<local-command-stdout>` carries terminal escapes and is no
  block, but it is the end of what the command did, so it ends a turn that was running as `completed`
  and clears the memory above, which is what lets the same command typed twice be published twice.
  `<local-command-caveat>`, `<command-message>` and `<command-args>` on their own stay hidden.

### What the terminal chose

A session a terminal holds takes no settings from an app, so the device reports what the terminal
chose instead and the apps draw the three values where their pickers would be (amendment A17). Every
Claude session the device mirrors — `terminal` or `shared` — has its `model`, `permission_mode` and
`effort` read out of the transcript: the model attachment Claude Code writes at start, after a resume
and on every `/model`; the permission-mode row it writes once per turn; and the effort carried on
each assistant message. A change is published as a `meta` event and a republished summary, so a
`/model` typed in the terminal reaches the apps within one tail interval. The model is recorded only
when it changes, which usually puts the value in force far behind the point a mirror starts reading,
so the whole transcript is read once — off the event loop, capped at 64 MiB — when the session is
adopted, and for every mirrored Claude session the daemon starts with. A line that is not JSON, or
not even UTF-8, is skipped like any other unreadable row, and only a read that worked counts: one
that failed is tried again at the next scan and keeps no other session from its read. The ids are
the agent's own and kept verbatim, suffix and all, whether or not they appear in the advertised
lists: `auto` is a permission mode only the terminal sets, `claude-opus-5[1m]` a model no list
carries, and an app shows either by its id. All three rows are internal to Claude Code, so one of an
unknown shape is read as "no settings" rather than as an error, exactly as the title rows are. A
session this device drives sets these itself and is never read this way.

## Attached terminal sessions

A terminal session normally has to be *taken over* to be driven from a phone, which means killing
the CLI and resuming it — losing its sub-agents, background commands and loops. Claude Code 2.1.267
and later can instead load a **channel**: an MCP server that injects user messages into the live
session and relays its permission prompts. When one is attached the session reports
`control: "shared"`, and the apps treat it exactly like a session the device runs itself.

### How it works

```
claude (your terminal)
  └── rc-client channel        stdio MCP server, spawned by the CLI
        └── state/channel.sock  newline-delimited JSON, one line per message
              └── rc-client run  the daemon
```

The bridge reads `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR` from its own environment,
registers with the daemon and stays until stdin closes. It survives a daemon restart: frames are
buffered and the registration is replayed on reconnect. It logs nothing but warnings, and never the
text of a message.

The bridge registers when Claude Code sends `initialize`, so the session is `shared` and the
device may inject from that moment. Claude Code, though, starts listening for channel messages
only once it has the server's tool list, and drops a message that comes earlier without a word,
which would lose a message sent within about 0.4 s of an attachment starting. So the bridge holds
what the daemon injects until a quarter of a second after it has answered `tools/list` — three
seconds after it started at the latest, for a CLI that never asks — and then writes it all, oldest
first; anything later goes straight through. Permission verdicts are never held: the CLI asks for
one only once it is listening.

The daemon injects a message **only when the session is idle**. A message injected during a turn is
handed to the model as untrusted external data it is told not to obey, which is why anything sent
mid-turn is held on the device as a `queue` entry and injected at the next idle point. Until then it
is not a message in the conversation at all: the bubble appears when the CLI takes it, with
`delivery: "delivered"`, so it lands after the output of the turn it waited for — where the terminal
shows it too. If the CLI absorbs an injection anyway the bubble becomes `delivery: "absorbed"` and
the device re-sends it once; absorbed a second time, the bubble is published once more as
`delivery: "delivered"` beside the warning notice, because the CLI has plainly read the message
both times and `absorbed` would promise a re-send that is not coming (amendment A34).

Turn state comes from the transcript, which the daemon already tails, so it lags reality by up to
two seconds — and **a turn ends when the CLI says it has**. Claude Code writes each content block of
one assistant message as a row of its own, and every one of those rows carries the message's final
`stop_reason`, so the sentence a model says before its tool calls arrives as a text-only row that
looks exactly like the last row of a turn. The device reads the row's `stop_reason` instead:
`tool_use` means the turn goes on whatever the row holds, and anything else — `end_turn`,
`stop_sequence`, `max_tokens`, or none at all — ends it. Reading the blocks instead once drained a
phone's held messages into a turn that was still running, which is how they came to be absorbed
(A34).

Each of those rows holds its block at position 0 and records the block's place in the message as
`apiBlockIndex`, so a mirrored or attached session names a text or thinking block after its
message and that place: the index the SDK streams the block under, the same however often the row
is read. A row without the field (a synthetic one, or a whole message in one row) counts from its
own first block. Counting from the row alone would give a thought the CLI shows and the answer
after it one id, and the answer would replace the thought.

### The shim

Claude Code refuses to load a self-hosted channel without
`--dangerously-load-development-channels`, and there is no way to allowlist one on a personal
account. The installer therefore writes `~/.rc-client/bin/claude`, a POSIX shell wrapper, and puts
that directory in front of `PATH` in your shell startup file inside a marked block:

```
# >>> remote-control >>>
export PATH="/Users/me/.rc-client/bin:$PATH"
# <<< remote-control <<<
```

The block is appended, never rewritten, so a startup file that is a symlink into a dotfile
repository stays one. `install.sh --no-shell-rc` skips it, and `rc-client uninstall` removes it by
its markers.

The wrapper appends the channel flags, and the settings file described below, **only** when stdin
and stdout are both terminals and the command line carries none of `-p`, `--print`,
`--input-format`, `--output-format`, `--sdk-url`, `--mcp-config` or
`--dangerously-load-development-channels`. Every other invocation reaches the real executable
unchanged, which is what keeps the device's own remote sessions — driven over pipes with
`stream-json` — out of the attachment path entirely. The daemon resolves the real binary itself and
never points the SDK at the wrapper.

Open a new terminal after installing, then run `claude` as usual. **Claude Code shows a one-time
confirmation per session** warning about development channels; choose "I am using this for local
development" and the device attaches within a second. `rc-client shim status` prints where the shim
is, whether it is first on `PATH`, which executable it wraps, and the two files it passes.

### The two hooks the settings file carries

An MCP server keeps the environment it was spawned with for as long as it lives, so the channel
bridge can only ever report the session id Claude Code picked when the terminal started. That is
wrong the moment you type `/resume` and pick another conversation, or `/clear` and start a new one:
the terminal is somewhere else and the device is still pointing at the id it was told at startup.
The session you are actually typing into then shows as `control: "none"` and a message sent from a
phone cannot reach it, while the session nobody is in claims to be attached.

So the wrapper also passes `--settings ~/.rc-client/state/claude-settings.json`. Its first hook is a
`SessionStart` hook: `rc-client hook session-start`. Claude Code runs it on startup and again on
every `/resume`, `/clear` and compaction. The hook reads the session id from the hook payload, walks
up to find the `claude` process that ran it, writes one line to the channel socket and exits; the
daemon then moves the live attachment to that session. It prints nothing on stdout, because anything
a `SessionStart` hook prints there is appended to the model's context, and it always exits 0, because
a failing hook would interrupt your session over a feature you did not ask for.

Its second hook is what lets a question be answered from a phone. Claude Code never relays
`AskUserQuestion` through a channel — the CLI relays only tools that need no person — but it does
run `PermissionRequest` hooks beside its own dialog and takes whichever answers first. So the
settings file carries `rc-client hook permission-request`, matched to `AskUserQuestion`, with a
timeout of a day. That hook sends the tool's questions to the daemon and then **waits** on the
socket: the daemon raises a `question` block the apps can answer, and the first answer to arrive
wins. An answer from an app comes back down the socket, the hook prints it as the tool's own
decision, and the CLI's dialog closes with it. An answer given in the terminal reaches the device
through the transcript instead, and the device then tells the hook to stand down; the hook prints
nothing, exits 0, and what the person typed in the terminal stands. Claude Code does not stop the
hook when its dialog is answered, so that release is the device's job, and a hook whose CLI exits
first sees its socket close and expires the block.

If you pass a `--settings` file of your own the wrapper leaves it alone: the session still attaches,
it just carries no hooks, `/resume` inside it goes unnoticed, and its questions can only be answered
in the terminal.

The settings file is written by `rc-client shim install` and again by the daemon at startup, so it
names whichever installation ran most recently. **A `claude` that was already running when the shim
was installed or re-installed keeps the wrapper it started with** — the command line and the settings
file are both read once, at startup — so quit and start it again to pick up a change.

### What works and what does not

This table is the Claude channel. Codex on the shared daemon does more; see the next section.

| From the apps | On a `shared` Claude session |
| --- | --- |
| Send a message | Yes, injected at the next idle point |
| Approve or deny a tool call | Yes, `allow` and `deny` only — the relay offers no session-scoped grant |
| Answer a question | Yes, through the `PermissionRequest` hook; the terminal's dialog and the card are one question |
| Stop the turn | Yes since A42: Escape typed into the terminal while a turn runs (or a just-injected message is still on its way into the transcript); `conflict` ("answer the prompt first") while an approval or a question is on screen; nothing typed when no turn runs |
| Change model or effort | Yes since A40, typed into the terminal through its pickers, this session only; `conflict` while the terminal is busy, and the reply waits for the transcript to confirm; a model whose effort is capped takes the nearest level it has, which the reply reports |
| Change permission mode | No, `unsupported`: change it in the terminal. The value is shown, read from the transcript (A17) |
| `/compact` | Yes since A40, typed into the terminal; other commands are `unsupported` |
| Rename | Yes |
| Take over | No, `conflict`: the session is already attached |
| Attachments | Yes since A49: the device writes the files and the words it injects end with their paths, which the CLI opens with `Read`; the bubble names the files and shows no path |

Whoever answers a permission prompt first wins. When the terminal answers, the device sees the tool
run or be refused in the transcript and closes the block with `decision.by: "terminal"`; a later
reply from an app is a no-op. A question works the same way and says so on the block: `by: "remote"`
when an app answered, `by: "terminal"` when the dialog did, with the terminal's own answer mapped
back from the tool result to the option ids the card offered. Only one question is open at a time;
a second one expires the first. While one is open the session reports `needs_input` — an approval
still outranks it, because an approval blocks the very work the question was asked about — and a
message an older app sends is queued rather than injected, because the dialog owns the prompt. When
the attachment drops, pending approvals and a pending question
become `expired`, and the session moves to `terminal` if the CLI process is still alive or to `none`
if it exited. Messages still held in the queue stay there and go out through the ordinary resume
path; their files are deleted now and written again by whatever delivers them (A49).

### The device types into the terminal (A40)

A channel carries user text and nothing else, so a `shared` Claude session used to refuse every
settings change. The shim now starts the CLI inside a pseudo-terminal the device owns, and the
device types into it what you would type yourself: `/model` and `/effort` through their pickers,
and `/compact`.

```
your terminal → rc-client pty proxy → claude → rc-client channel bridge
```

The wrapper runs `"$RC_PYTHON" -m rc_client.channel.pty -- "$REAL" "$@"` for an invocation it
attaches to, where `RC_PYTHON` is the interpreter this installation was written by. **If that
interpreter is gone the wrapper runs the CLI directly**: a venv of ours that someone deleted must
never stand between a person and Claude Code. Every non-attaching invocation is untouched.

The proxy (`rc_client/channel/pty.py`) is a pseudo-terminal and nothing else. Bytes go both ways
untouched, the window size follows, `SIGTERM`, `SIGHUP` and `SIGINT` are passed on, termios is
restored on the way out and the exit status is the CLI's. It imports nothing but the standard
library at startup — the device's configuration is read later, off the launch path — so the
terminal starts as fast as before. It dials the channel socket with backoff on the same loop that
relays bytes and registers the CLI's pid, which is the pid the channel bridge registers too
(`os.getppid()`), so the terminal and the attachment find each other (`sessions/ptys.py`).
**Relaying never waits for the daemon.**

The daemon asks it three things: type this, what is on screen, and is anybody typing. The screen
comes back with the escapes taken out and the horizontal jumps Claude Code lays its rows out with
turned into spaces (`channel/screen.py`), so `1. Sonnet` and the picker's `❯` can be read out of a
redraw; a `keys` frame may clear the screen buffer first, since a redrawn TUI leaves the previous
picker's frames in it. The terminal's own answers to the CLI's queries (`CSI c` and the like) are
not counted as typing. Nothing of what the person typed leaves the terminal: the proxy reports
**how many** characters are in their unfinished line, never which.

`sessions/typist.py` holds the scripts. Each one holds the session's lock, so a message from an app
waits behind the keystrokes rather than landing between two of them. The transcript reader that
confirms each change never waits for that lock: a held message it finds ready goes to the session's
drain task, which injects it once the script is done. Three rules make this safe to do to somebody
else's terminal:

- **Only into an idle terminal.** No turn, no dialog, an empty draft, and a keyboard still for two
  seconds. Anything else is `conflict` ("the terminal is busy; try again in a moment") and
  **nothing is queued**.
- **Never the argument form.** `/model opus` and `/effort low` apply at once and save the choice as
  the person's default. The scripts walk the pickers — `/model` by row, `/effort` all the way left
  and then right to the level — and press `s`, which is this session only and writes no settings
  file.
  On a conversation with cached history the CLI asks once more after `s` — "Switch model? Your
  next response will be slower and use more tokens … 1. Yes, switch to … 2. No, go back", yes
  highlighted — and the device presses Enter; a fresh session asks nothing. Claude Code 2.1.299
  asks the same about an effort level, titled "Change effort level?", and gets the same Enter.
  Its slider has the five levels the device offers, fewer on a model whose effort is capped, and
  keeps Ultracode apart as a toggle on Tab, which the device never presses; both sliders stop at
  their ends. The TUI rewrites only the cells that changed and the screen text marks each jump
  with one space, so a footer drawn over a half-matching screen reads "s for this se sion only":
  the slider counts as open when any of its own words is on screen whole (`←/→ to adjust`, that
  footer, `Smarter`, `Ultracode`).
- **The transcript decides.** The reply to `session.set` waits for the CLI to write the command and
  its answer (`Set model …` matched loosely; `Set effort level to <level>`); until then the app's
  `Session` says the old value. The level is the one the answer names, and that is what the
  `Session` and its `meta` then say: a capped slider stops short of the level asked for, the CLI
  sets the nearest one it has, and the change succeeds with that level. A cap in the person's
  settings or their organization's is answered the other way Claude Code 2.1.303 words it,
  `Effort '<asked>' exceeds the cap for <model> …; set to '<level>' instead`, and read the same.
  An answer naming a level the device does not offer is refused. The transcript is read the moment
  the wait begins, and a session that had no transcript until this command started one has it
  found then (see "Terminal sessions"), so the first change typed into a fresh session is confirmed
  like any other. A picker that does not open, or an answer that does not come within eight
  seconds, presses Escape and answers `conflict`. The rows the device typed are claimed, so A32
  draws no terminal bubble for them; `/compact` is echoed as the app's own `user_message` (A27) and
  the compaction notice is A32's as before.

**Stop is the fourth script, and the one the rules above do not fit (A42).** `session.stop` on a
`shared` Claude session presses Escape, which is how a turn is stopped in that terminal. It keeps
the lock and the dialog rule — an approval or a question is answered, not escaped, so the request is
`conflict` ("answer the prompt first") until it is — and drops the other two: there is no idle
gate, because a busy terminal is the whole point and one Escape does not clear a half-typed line
(Claude Code asks "Esc again to clear" first), and nothing is waited for, because the CLI files
`[Request interrupted by user]` and the transcript reader ends the turn from that row (A32). Busy
means the mirror's turn or an injection the transcript has not shown yet; with neither the device
types nothing and answers `{}`. Claude reports `shared_interrupt: true` on the same condition as
`shared_settings`: the shim is installed and first on `PATH`.

`AgentInfo` gains `shared_settings_keys`. Claude reports `shared_settings: true` with
`["model", "effort"]` and the `commands` capability **only while the shim is installed and first on
`PATH`**; the permission mode has no command to type, so it stays what the terminal set and an app
draws it as a value (A17). On a `remote` (SDK) Claude session the one command, `/compact`, is sent
as prompt text with the same echo. A `shared` session started by an older shim has no proxy: the
device answers `conflict` until the CLI is started again.

## Codex on the shared daemon

Codex 0.153 and later run **every bare `codex` TUI inside one local app-server daemon**, whose
control socket is `$CODEX_HOME/app-server-control/app-server-control.sock`. The device connects to
that socket as a second client, so a Codex session started in a terminal is `control: "shared"`
rather than `control: "terminal"`, and the apps drive it in place: no take over, no SIGTERM, no lost
sub-agents or background commands.

A shared Codex session can do everything a remote one can, which is more than a Claude channel can:

| From the apps | On a `shared` Codex session |
| --- | --- |
| Send a message | Yes. Idle starts a turn; during a turn `auto` steers it and `queue` holds the message until the turn ends |
| Stop the turn | Yes, `turn/interrupt`, whoever started it |
| Approve or deny a tool call | Yes, with whatever options the daemon offers for that prompt |
| Answer a question | Yes |
| Change model, permission mode, effort or speed | Yes, `thread/settings/update` changes the thread for everyone attached to it |
| Attachments | Yes, steered messages included. Images become image inputs; other files are written to disk and named in the prompt |
| Run a slash command | Yes, for the nine the device maps to app-server methods; see "Slash commands" below |
| Take over | No, `conflict`: the session is already attached |

### The speed tier

Codex offers a faster service tier per model — `priority`, which its own catalogue names "Fast" and
the TUI toggles with `/fast`. It is thread state, not a per-turn argument, so the device sets it
with `thread/settings/update {threadId, serviceTier}` and never on `turn/start`: a session created
at a tier asks for it immediately after `thread/start`, which accepts `serviceTier` and ignores it.
`serviceTier: null` takes the thread back to the standard speed. A thread that starts at a tier
because the user's own Codex configuration puts it there is adopted rather than cleared: the
session reports the tier the thread actually runs at.

The daemon does not validate the id, so the device does, against the `serviceTiers` of the model the
session runs on. It reads the tier back from the `thread/start` and `thread/resume` results and from
the `threadSettings` of every `thread/settings/updated` notification, which is how a `/fast` typed
in the terminal reaches the apps as `meta.speed` (A17's rule for model and effort, A21's for the
tier). Codex reports a thread that has never had a tier as `null` and one whose tier was cleared as
`"default"`; both are the standard speed, which `Session.speed` spells as null. `thread/list` and
`thread/read` carry no `serviceTier` at all in Codex 0.154, so a mirrored thread reports its tier
from the moment the device attaches to it, not from the index.

### Slash commands

Codex's app-server interprets nothing that begins with a slash: a `turn/start` whose input is
`/status` runs an ordinary model turn that answers the literal string. So Codex is the one agent
whose command list is a **fixed table written here** rather than something the agent is asked for,
and every entry is a named method call. The table is the same whichever connection carries the
session — the shared daemon or the device's own app-server — because `agents/codex/commands.py` is
written against a `call(method, params)` coroutine that both provide, and it is the same for a
session with no process at all, which is why `session.commands` can answer without waking anything.

Descriptions are Codex's own words, read out of the TUI's `/` popup in the 0.154.0 binary, trimmed
only where the terminal promises something an app does not get: `/usage` cannot spend a usage-limit
reset from here, `/hooks` and `/skills` are read-only, and `/mcp verbose` is a terminal flag.

| Command | What the device calls | What the apps see |
| --- | --- | --- |
| `/compact` | `thread/compact/start` | The compaction runs as a turn of its own; its `contextCompaction` item becomes the `notice` "Context was compacted; earlier turns are summarised." |
| `/review` | `review/start` `{delivery: "inline"}`, target `uncommittedChanges` with no argument and `{type: "custom", instructions}` with one | The notices "Review started" and "Review finished" around the reviewer's own turn, then Codex's summary as `assistant_text` |
| `/init` | `turn/start` with the TUI's canned AGENTS.md prompt | The bubble reads `/init`; the model reads the prompt |
| `/diff` | `command/exec` for `git diff --stat`, `git diff` and `git ls-files --others --exclude-standard`, in the thread's cwd | One `tool_call` block titled `/diff`. `command/exec` answers with the output instead of appending an item, which is what keeps `/diff` from reading as something the agent did |
| `/status` | Nothing, usually: the model, effort, speed, approval policy and sandbox all come back on `thread/start` and `thread/resume` and are kept current by `thread/settings/updated` | A `tool_call` block listing them with the cwd and the last token usage |
| `/usage` | `account/usage/read` and `account/rateLimits/read` | A `tool_call` block: plan, each rate-limit window with its reset time, available limit resets, lifetime tokens, streak |
| `/skills` | `skills/list {cwds: [cwd]}` | A `tool_call` block, one line per skill with its scope, disabled ones marked |
| `/hooks` | `hooks/list {cwds: [cwd]}` | A `tool_call` block, one line per hook: event, handler, matcher, trust |
| `/mcp` | `mcpServerStatus/list {threadId, detail: "toolsAndAuthOnly"}` | A `tool_call` block, one line per server: connection state, tool count, whether it needs signing in |

Every information block is `tool_kind: "other"` with `tool` and `title` `/name`, opened `running`
and replaced `succeeded` under a block id of its own, so the command's bubble and its output are two
rows rather than one. Long output is capped at 16 KiB with `output_truncated` by the ordinary event
bounds, which a long `/skills` or `/mcp` listing can reach.

`/status` has one fallback each way. A thread the daemon refuses to resume — one whose first turn
has not run, so it has no rollout — never told the session its settings, so the block fills them
from `thread/read`; and a session that therefore never learned its sandbox reads `sandbox_mode` from
`config/read`. Both were exercised against the real daemon.

Two translations exist only so that a command's turn reads as a turn. `contextCompaction`,
`enteredReviewMode` and `exitedReviewMode` are state changes rather than work, so they become
one-line `notice`s instead of the `tool_call` blocks a terminal draws banners for. And `review/start`
feeds the reviewer a `userMessage` item carrying the brief it was given — nobody typed it, so the
translator drops user messages for as long as the thread is in review mode. Without that, every
remote `/review` would put a paragraph beginning "Review the current code changes" on screen as if
the user had sent it.

Settings, lifecycle and terminal ergonomics are never listed, as A27 requires: `/model`, `/fast`,
`/permissions` and `/name` are `session.set`; `/new`, `/archive`, `/delete` and `/resume` are frames
of their own; and `/vim`, `/theme`, `/keymap`, `/copy`, `/pets`, `/raw`, `/cd` and `/pwd` change a
terminal the apps cannot see. Codex's `/goal`, `/plan`, `/fork`, `/memories`, `/experimental` and
`/ps` all have methods behind them and could be added, but each wants a surface of its own rather
than a line of text in a block, so none is in the table yet.

### Which TUIs the daemon owns

There is no rule about how to start Codex. A TUI started with
`--dangerously-bypass-approvals-and-sandbox` runs on the shared daemon exactly as a bare `codex`
does: the daemon holds that TUI's thread, holds its rollout open for writing, and relays the turns
typed into it. `-c` is different — a TUI started with `-c model_reasoning_effort=low` writes its own
rollout, and the daemon has no thread in its directory at all — but the device does not need to know
which flags do that, and does not look.

What it looks at is what the process holds. A TUI running its own embedded app-server writes the
thread's rollout itself, so `lsof` finds it holding a file under `$CODEX_HOME/sessions`; a TUI on
the shared daemon holds no rollout at all, because the daemon holds it. Only the first kind is left
out of the count of terminals the device attributes to daemon threads, so it never speaks for a
thread it cannot be in. It still appears in the apps, mirrored from its rollout file with
`control: "terminal"`, because the rollout holder scan finds the process holding it. Anything you
would have set with `-c` can be set from the apps instead, through the model, permission-mode,
effort and speed pickers.

### Setup

`install.sh` runs `rc-client codex setup` unless you pass `--no-codex`. It is idempotent, and it is
the only thing in this project that downloads anything:

1. Find the standalone Codex at `~/.codex/packages/standalone/current/bin/codex`. **Only that path
   counts.** An npm or Homebrew `codex` earlier on `PATH` is the same CLI but not the install the
   daemon manages, and every `daemon` subcommand refuses to run without the standalone one whichever
   build invokes it:

   ```
   Error: managed standalone Codex install not found at
   ~/.codex/packages/standalone/current/codex

   This command requires the standalone install managed by the Codex installer, because the
   daemon starts and updates app-server from that fixed path.
   ```

   If it is missing, download `https://chatgpt.com/codex/install.sh` to a temporary file, check that
   it really is a shell script, and run it with `CODEX_NON_INTERACTIVE=1`, `CODEX_HOME` and
   `CODEX_INSTALL_DIR` pinned to the real directories, and **`HOME` pointed at
   `~/.rc-client/state/codex-installer-home`**. That last one is the interesting part. Codex's
   installer appends a `# >>> Codex installer >>>` block to a shell profile whenever it finds
   another `codex` on `PATH`, whatever else is true — `add_to_path` returns early only when the
   target directory is on `PATH` *and* no other install was detected — and it reads the standalone
   build itself as npm-managed, because it greps the candidate for `#!/usr/bin/env node` and the
   standalone binary embeds that string in a bundled docs script. So on any machine that already has
   Codex the rewrite happens; `HOME` is the only thing that decides which file it writes, and the
   dotfiles here are symlinks that a rewrite would replace with a regular file. Confining it to a
   home of ours is what makes the install safe to run unattended. Nothing is prompted
   (`CODEX_NON_INTERACTIVE=1` makes every `prompt_yes_no` answer no, including the offer to
   `npm uninstall -g @openai/codex`), and the layout produced this way is identical to a plain run:
   the two trees differ only in the name of a per-run temporary directory. If `~/.local/bin` is not
   on `PATH` afterwards, `setup` prints one line — `export PATH="~/.local/bin:$PATH"` — and writes
   nothing.
2. `codex app-server daemon bootstrap` **on the standalone binary**, never `--remote-control`, and
   never on whatever `PATH` resolved. Remote control enrols the machine with OpenAI's relay, which
   this project does not use, and the device only ever logs the `remoteControl/status/changed` it
   receives. The bootstrap is not what makes a TUI join — `daemon start` alone is enough, and takes
   0.33 s — but it is what a machine set up by hand ends up with, and it is harmless to repeat.
3. Install supervision on the same standalone binary, because the bootstrap uses a pid backend and
   leaves none: a launchd agent `dev.remote-control.codex-daemon` on macOS, a
   `rc-codex-daemon.service` systemd user unit on Linux. `codex app-server daemon start` is
   idempotent and returns immediately, so both run it at login and again every five minutes rather
   than trying to hold a process open. On Linux run `loginctl enable-linger $USER` so it survives
   logging out.
4. Verify with a real WebSocket handshake on the socket, not a file-exists check.

`rc-client codex status` prints the standalone binary the daemon commands use, what `codex` on
`PATH` resolves to, whether the socket is there, whether it answers, the two versions involved, and
the state of our supervision:

```
daemon binary       /Users/you/.codex/packages/standalone/current/bin/codex
codex on PATH       /Users/you/.local/bin/codex
daemon socket       /Users/you/.codex/app-server-control/app-server-control.sock
socket present      yes
handshake           ok
app-server version  0.154.0
installed version   0.154.0
supervision         loaded
```

A `codex` on `PATH` from another build is reported and nothing more. It used to carry a warning that
terminal sessions started with it "may not join the shared daemon"; that was a guess, and it was
wrong. An npm-installed TUI joins the shared daemon exactly like the standalone one — verified by
watching `thread/loaded/list` grow while one ran. What an npm-only install does lack is
`$CODEX_HOME/packages/standalone`, without which no `daemon` subcommand will run at all, which is
why step 1 installs the standalone build rather than asking anyone to remove anything. Removing a
Codex install is always yours to do — this tool never uninstalls one. `rc-client uninstall` removes
our launchd agent or unit and leaves Codex, its daemon and its sessions completely alone.

### How the device uses it

One connection for the whole machine, opened at startup, identified as `remote-control`. In Codex
0.154.0 the first client to connect was observed to stamp its name on every daemon thread;
that is historical evidence, not a rule for every release. A terminal thread on Codex 0.160.0
carries `originator: "codex-tui"` and `source: "vscode"`. On top of the connection:

- `thread/list` is the session history and `thread/loaded/list` the live threads. Threads marked
  `ephemeral` are dropped: Codex spawns one per turn just to generate a title, and so is every
  thread another application on this machine owns (below).
- A loaded thread is subscribed with `thread/resume {excludeTurns: true}` and backfilled from
  `thread/items/list`, so the block timeline comes from the daemon rather than from a rollout file.
  Rollout mirroring stays only for the threads the daemon does not know about.
- `origin` and `control` follow the table in PROTOCOL.md 4.4. An unloaded thread is `none` and the
  next `session.send` resumes it.
- The daemon says nothing at all when a TUI exits, and a thread it once loaded stays loaded for as
  long as the daemon lives, so the TUI process is the only signal there is. **Being loaded therefore
  proves nothing**: a directory somebody works in holds one loaded thread for every `codex` ever
  started there, and treating them all as attached is what once put a row in the apps for each of
  them. A thread is `shared` only while a terminal is known to be in *that* thread. Two things say
  so. A thread another client opens or resumes is that client's, announced by `thread/started`; and
  a `userMessage` carrying a `clientId` that is not ours is somebody typing. Failing both, the
  ten-second process scan decides, and it decides by directory: it counts the live bare `codex`
  processes running in each `cwd` and hands out that many claims, to the thread that already had one
  first and then to whichever the daemon saw used most recently. A thread this device started is
  never handed a claim that way — it is not a thread a terminal opened — until somebody has been
  seen typing in it, after which a `codex resume` on it is as ordinary as any other terminal. A
  thread with no claim becomes `remote` while a turn this device started is running and `none` when
  it is idle; `origin` never changes, the subscription is kept, and the next message typed in that
  terminal makes it `shared` again. Evidence gathered since the last scan outranks the scan, so a
  TUI resumed from a different directory is not written off while it is being used, and a scan that
  cannot be completed changes nothing. The process test is by what the process holds, never by its
  flags: a `codex` on a terminal with no helper subcommand (`app-server`, `exec`, `mcp`,
  `mcp-server`, `proto`, `daemon`), minus any that is holding a rollout under `$CODEX_HOME/sessions`
  open, which is how a TUI running its own embedded app-server gives itself away.
- **A thread with nothing in it is not a session yet.** A TUI opens a thread the moment it starts,
  before anything is typed into it, and that thread has no name, no preview and no rollout to
  resume. Publishing it would put an untitled row in every app the moment a terminal window opens,
  and another one for every window opened and walked away from — the daemon never unloads them and
  never closes them either. Such a thread is remembered rather than published; the first thing it
  says both publishes it and proves whose it is, because the only client that can be in it is the
  one that opened it. A thread has no rollout until that first turn, so `thread/resume` refuses it
  until then; `turn/start` works throughout. The same applies in reverse: a thread created from an
  app can only be reopened with `codex resume <id>` **after** its first turn.
- The daemon echoes the prompt that started a turn back as a `userMessage` item, on `item/started`
  and again on `item/completed` with the same item id, and the observed `clientId` of our own
  injection is `null`. The device matches that echo against the prompts it has sent, keyed by text,
  and then remembers the item id, so the second event, a backfill and a reconnect all drop it
  instead of adding a `terminal` bubble beside the one the apps already show. One echo is spent per
  message sent, so a terminal user typing the same words still gets their own bubble. A `clientId`
  that has never appeared on an echo of ours is a TUI sharing the thread, which is what turns a
  `remote` thread `shared`.
- `thread/name/updated` carries `{threadId, threadName}` and reaches every subscriber, so a thread
  named in a terminal renames itself in the apps. It never overrides a title the user set.
- Approvals arrive as JSON-RPC requests that fan out to every subscriber. The options come from the
  daemon's own `availableDecisions` for that prompt, mapped onto `allow`, `allow_session`,
  `allow_always` and `deny`. Whoever answers first wins; when somebody else answers,
  `serverRequest/resolved` closes our block with `decision: {option_id: "elsewhere", by: "terminal"}`
  and our late reply is discarded silently.
- The connection reconnects with backoff. On every reconnect each followed thread is resumed again
  and backfilled from the last item we published, so a daemon restart loses nothing.

### Work another application owns

Codex keeps one history for the whole machine. The ChatGPT desktop app's chats and its scheduled
automations, an IDE extension's threads and the subagents a thread spawned all sit in `thread/list`
and in `~/.codex/sessions` beside what somebody typed at a terminal. Published as they are, a single
scheduled automation adds a row for every run, and one of them reads `control: "terminal"` while the
desktop app holds its rollout open, because anything holding a rollout reads as a terminal.

Two fields describe a thread's provenance, and both the index entry and the rollout's
`session_meta` carry them: `originator` is its recorded client name, and `source` describes the
entry point — `cli` for older TUIs, `exec` for `codex exec`, `vscode` for an app-server client,
an object for a subagent. A terminal TUI inspected on Codex 0.160.0 reports `codex-tui` with
`vscode`, so `vscode` alone does not identify a desktop or IDE thread. A thread is the device's to
publish when its `source` is a plain string **and** one of three conditions holds: its `originator`
is one of the names this device connects under (`remote-control` on the shared daemon, `rc-client`
on the app-server it spawns for itself); its `source` is `cli` or `exec`; or its `originator` is
exactly `codex-tui` and its `source` is exactly `vscode`. The last pair is accepted together:
another application's `vscode` thread is still foreign. A subagent carries its parent's
`originator`, so the `source` object tells the two apart: a thread another thread spawned is never
a session, not even one spawned by a thread of ours. Everything else belongs to the application
that started it: never published, never mirrored from its rollout,
its holder never asked about, and removed with `session.removed` if it was published before the rule
(amendments A18 and A48). The two device names live in `agents/codex/provenance.py` beside the
predicate and are imported by both handshakes; daemon indexing and rollout discovery use the
same predicate. An unrecognized provenance pair or a non-string `source` is foreign, because the
cost of guessing wrong is a row nobody here can open. Nothing ever changes a thread's provenance,
so the answer is remembered: a foreign thread being worked on elsewhere costs one `thread/read`
for the whole run, not one per event it sends.

Removing what was published before the rule needs the daemon, because `thread/read` is where the
answer comes from: a device on the fallback path (no daemon, rollout mirroring) still shows a
foreign session it stored earlier, and drops it the first time a daemon answers a handshake. New
foreign work never appears on either path.

### When there is no daemon

**Nothing else on the machine ever starts the shared daemon.** A bare `codex` TUI does not: with the
socket absent it runs its app-server embedded and never touches the control socket, verified by
watching a TUI reach its prompt and exit with the socket still absent throughout. So a device whose
daemon is not running would stay on rollout mirroring for ever — every terminal Codex
`control: "terminal"`, takeover only — and nothing the person did in the terminal would change it.
That is the same defect behind all three reports it came from: a machine where Codex was installed
with npm (no standalone, so `daemon start` exits 1 and `codex setup` never left a daemon behind), a
machine that upgraded Codex (the upgrade leaves no daemon running), and a machine enrolled under npm
and later switched to the curl build (the standalone appeared, but nothing looked again).

So the device does it itself, on the same ten-second scan that reconciles the thread index:

- Socket absent, or present and not answering, and the standalone build is there: run
  `codex app-server daemon start` and connect. It is idempotent, returns `alreadyRunning` when the
  daemon is already up, and takes about a third of a second. At most once a minute; after three
  consecutive failures, once every ten minutes, with Codex's own message logged once rather than
  once a minute.
- If the machine has no supervision at all (`status()` is "not installed" — a device enrolled before
  the daemon existed, or one whose setup step failed), install it, once per device start.
- Nothing is ever downloaded here. Installing Codex stays in `rc-client codex setup`, where somebody
  asked for it.
- A device pointed at a socket of somebody else's choosing (`RC_CODEX_DAEMON_SOCKET`) starts
  nothing: `daemon start` only ever creates the socket under `CODEX_HOME`, so that socket is not
  ours to bring up.

When the daemon does come up, `_on_mode_change` publishes `agents.updated` and the apps drop the
attach hint without anyone running anything. Until then the device falls back to today's behaviour:
one `codex app-server` process per session, spawned by the device, with terminal sessions mirrored
from their rollout files, and the agent reporting `attach: "daemon"` with `attach_ready: false`.
Sessions already discovered keep the runner they have until they are next resumed.

### When the daemon is out of date

`codex update` is a thin wrapper around the installer: it replaces the build on disk and leaves the
running app-server alone, printing "Please restart Codex", which means the TUI, not the daemon. So a
machine that upgrades keeps serving the old app-server indefinitely — our own five-minute
`daemon start` nudge returns `alreadyRunning` and never clears it. This costs nothing while it
lasts: TUIs join a drifted daemon perfectly well, verified across a nine-minor gap (daemon 0.145.0,
CLI 0.154.0), and neither the TUI nor the daemon says a word about the mismatch.
`codex app-server daemon version` is the only place it is visible at all, as `appServerVersion`
against `managedCodexVersion`, and `daemon restart` is the only thing that clears it.

Every fifteen minutes while connected, the device asks. When the two versions differ it restarts the
daemon — but only when nobody is in it, because a restart drops every subscriber: no session this
device drives may be mid-turn, no daemon thread may have a terminal in it, and the terminal scan
must have completed (an incomplete scan is not permission). Otherwise it logs once and asks again
next time. The existing reconnect path resumes and backfills every followed thread afterwards, so
the restart costs nothing visible. `rc-client codex status` says `drifted; restart pending` on both
the version line and its one-line summary while that is true.

The model catalogue follows the build too: it is cached per binary by real path, and
`packages/standalone/current` is a symlink into a per-version release directory, so an upgraded Codex
asks under a name of its own and never gets the old answer.

### `RC_CODEX_THREAD_CONFIG`

An optional JSON object merged into the `config` of threads **this device starts**, applied by Codex
to the thread wherever it is later resumed, including in a terminal. It is never sent on a thread
somebody else created. It exists because some settings have no wire equivalent; leave it unset unless
you know you need it.

## Grok Build

Grok is driven over the Agent Client Protocol, newline-delimited JSON-RPC on the stdin and stdout of
an `agent agent … stdio` child. There are two ways to run that child, and the person's own
configuration decides which (A28). When `~/.grok/config.toml` has `[cli] use_leader = true` and no
sandbox profile, every `grok` on the machine runs its agent inside one **leader** process, and the
device starts one `agent agent --leader stdio` client of that leader and puts every Grok session it
drives or joins on that one connection (`agents/grok/leader.py`, `service.py`). Otherwise each
session gets a private `agent agent --no-leader <flags> stdio` child of its own, as before, and a
`grok` in a terminal is only mirrored. `--no-leader` there is deliberate: without the leader mode
the person asked for, the device never shares a backend with anybody.

Only the model and the effort are process flags (`--model`, `--reasoning-effort`) on a private
child, and **the leader ignores them**: on the leader the device sets both with
`session/set_config_option` right after `session/new`. The working directory and the permission mode
are never flags — `agent agent` rejects `--cwd` and `--permission-mode`, which belong to the TUI.
The directory goes in `session/new {cwd, mcpServers: []}`, and the mode is
`session/set_mode {sessionId, modeId}`, whose ids are exactly the six of PROTOCOL.md section 4.3.
Resuming is `session/load` with the session id; the session id on the wire is Grok's own UUID, so a
session created here is rekeyed to it as soon as `session/new` answers.

A turn is `session/prompt`, which only replies when the turn is over, so it runs as a task and the
turn ends on whichever arrives first — the `turn_completed` notification or the prompt's own answer.
`session/cancel` interrupts. Live changes to the model and the effort are
`session/set_config_option`, whose value is a plain string and whose reply is the complete option
list, which the device republishes as `meta`.

### What the stream carries

Updates arrive under three method names — `session/update`, `_x.ai/session/update` and
`_x.ai/session_notification` — carrying the same objects, so the translator switches on
`update.sessionUpdate` and ignores the envelope:

| `sessionUpdate` | Becomes |
| --- | --- |
| `agent_message_chunk` | `assistant_text` deltas, closed with the whole text when the stream ends |
| `agent_thought_chunk` | `thinking`, the same way |
| `tool_call`, `tool_call_update` | one `tool_call` block keyed by `toolCallId`; the kind comes from Grok's own `_meta["x.ai/tool"]` name and namespace, then from ACP's `kind`. A non-`grok_build` namespace is an MCP tool |
| `plan` | `todos` |
| `turn_completed` | `turn_completed`, with the turn's tokens and the real cost: `costUsdTicks` is USD at 1e10 ticks to the dollar. Grok reports one turn at a time, so the device accumulates the session total |
| `model_changed`, `current_mode_update`, `config_option_update` | `meta` (amendment A17) |
| `available_commands_update` | the session's slash command list (see below); state, not an event |
| `hook_execution`, `hook_run_started`, everything else | nothing |

A tool's `rawOutput` also carries the raw bytes of a command's output as an integer array; only
`output_for_prompt` and the text content blocks are published. `locations` has no field of its own in
`tool_call`, so it rides inside `input`, the one open object an app already renders.

### Slash commands

Grok needs no table of our own: the agent pushes its whole command list over ACP as an
`available_commands_update` the moment a session opens, and again whenever plugins or skills are
reloaded. The list mixes built-in shell commands, skills, plugin commands and workflows, and
running one costs nothing: `/hooks-list` as the text
of `session/prompt` finished in 4 ms, spent no model tokens and answered in a single
`agent_message_chunk`, which the translator already publishes as `assistant_text`. So
`session.command` is the runner's own `send` path with the text `/name argument`, under the app's
request id, and the turn that follows is an ordinary turn.

`agents/grok/commands.py` maps the advertisement onto `Command` (§4.11):

| From the agent | Becomes |
| --- | --- |
| `name` | `name`, if it matches the protocol's `^[a-z0-9][a-z0-9_:.-]*$`; an entry that does not is dropped, because an app could neither filter it nor send it back |
| `description` | `description`, collapsed to one line of at most 160 characters — a Grok skill's own description runs to a paragraph of trigger phrases, and a composer row has one line |
| `input.hint` | `argument`, capped at 80 characters the same way |
| `_meta` | `group`: no `_meta` at all is `Built-in`, `workflowSource` is `Workflows`, `pluginName` is `Plugins`, and a bare `scope` is `Skills` |

Three advertised names are never listed, each for a reason the protocol already gives:

| Name | Why not |
| --- | --- |
| `always-approve` | Toggles "skip all permission prompts". Permission modes are `session.set`, and A27 says a device never gives a phone two controls for one thing — least of all one tap from approving everything |
| `context` | Verified against the real agent: the turn ends in 10 ms having emitted no update at all, because the TUI renders it in its own pager. `/session-info` is the one that does answer over ACP, and it is listed |
| `statusline` | Configures the Grok Build status line, a terminal surface no app draws |

The last list is kept in `~/.rc-client/state/grok-commands.json` and rewritten only when it changes,
so the plugin's module-level `commands(session)` can answer `session.commands` for a session with no
live process, across a restart of the daemon. A session that has opened but whose advertisement has
not landed yet — it is a notification, so it can lag the session by a beat — answers from the same
file. `command()` refuses a name that is not in the list it just answered with, which is what keeps
an unknown `/foo` from reaching the model as literal text.

### Setup

`install.sh` runs `rc-client grok setup` unless you pass `--no-grok`, and it is the one thing this
project changes in a person's own agent configuration. It sets `[cli] use_leader = true` in
`~/.grok/config.toml` — **in place**: the existing path is opened for writing, never unlinked,
renamed or replaced, because these dotfiles are often symlinks into a synced folder and the inode has
to survive; a `use_leader = …` line under an existing `[cli]` table is rewritten, a missing line is
inserted right under the header, a missing table is appended, a missing file is created, and every
other byte, comments included, stays as it was (`agents/grok/setup.py`). Then it prints what
`rc-client grok status` prints:

```
grok binary         /Users/you/.grok/bin/agent
use_leader          on
sandbox             off
leader socket       /Users/you/.grok/leader.sock
leader version      1.0.30
installed version   1.0.30
terminal sessions   1 registered
Restart any running grok to have it join the leader
```

`status` asks a leader its version only when the flag is on and no sandbox is set, and asking
starts a leader when none is running — which is exactly what the next `grok` would do, so it is never
a surprise; the summary line of `rc-client status` (`grok leader    ready (running); 1 terminal
session(s)`) never connects. Neither command starts or stops anything a person is using: a `grok`
that was already running keeps the agent it started with until it is restarted, which is why both
end with the restart note and why the apps' hint says the same. A non-`off` sandbox profile —
`GROK_SANDBOX`, or `[sandbox] profile` in the configuration — refuses leader mode in Grok itself, so
`attach_ready` is false and the note says so.

### On the leader

The leader is Grok's own: one backend per machine, started by whichever client comes first and left
running when they leave (`agent agent leader --no-exit-on-disconnect …`, a child of the TUI that
started it, its stderr in `~/.grok/leader.log`). The device's client is `agents/grok/leader.py`:
one `initialize`, whose `_meta.agentVersion` is the leader's version, and then every notification
and every `session/request_permission` routed by `params.sessionId` to the runner of that session;
rows for a session nothing here holds go to the service. When the child dies the service reconnects
on the next scan, re-`initialize`s, re-`session/load`s every session it held and republishes each
one's control.

Who is in a session is Grok's own registry, `~/.grok/active_sessions.json` — `[{session_id, pid, cwd,
opened_at}]`, written by every TUI while it runs and by nothing else — because the leader announces
nothing when a TUI exits and offers no way to ask who is attached. On every ten second scan
(`GrokLeaderService.tick`, driven from the mirror's scan) the service reads it. A live entry it does
not yet hold gets one question, `_x.ai/session/info {sessionId}`, which answers a full object for a
session the leader has loaded and `{}` for one it has not: loaded means a leader-mode TUI is in it,
so the service takes the session — adopting it if the mirror never saw it, taking over the mirrored
entry otherwise, at which point the mirror drops its tail (`_grok_is_leaders`) — and joins it with
`session/load`, publishes `control: "shared"`, the holder pid, and the model and effort the load
result's `configOptions` name; `{}` means a `grok` running its own agent, and the mirror keeps it as
`terminal`. Titles come from `_x.ai/sessions/changed` (`upserted[].title`), from the leader's
`_x.ai/session/list`, or from `summary.json`'s `generated_title`.

`agents/grok/control.py` is the A28 table of PROTOCOL.md 4.4 as one pure function: registered and
held here → `shared`; registered and not held → `terminal`; not registered and held → `remote`
while a turn this device started is running, `none` otherwise; not held → `none`. A session a TUI
leaves is therefore still the device's to drive, because the leader still holds it, and the next
`session.send` goes straight to the runner already attached. Two things never happen: the runner
never sends `session/close`, which unloads a session for every client of the leader including the
terminal that is in it (verified: it broke the TUI's session), and a private child is never spawned
for a session the leader holds.

`session/load` replays the whole conversation, every conversation row carrying
`_meta.isReplay: true` and `_meta.eventId` `<sessionId>-<n>`, the same counter `updates.jsonl`
carries. The device keeps one cursor per session, `grok-cursor:<id>` in the registry
(`agents/grok/cursor.py`), written by the mirror's tail and by the runner alike; on a join the
replay rows above it are published as history and the rest are skipped, so a session handed from
the mirror to the leader — or back, when the leader goes away — neither doubles nor loses a turn.
Grok echoes every prompt back as a `user_message_chunk`, the TUI's and the device's alike; the TUI's
become `user_message {source: "terminal"}` bubbles, and the device's are recognised by their text
against what this device sent in the turn and dropped (`agents/grok/echoes.py`), the same problem
Codex's daemon has and the same answer. The text compared is the prompt as sent, the attachment
paths of A49 included; every other prompt has those paths taken off before it is published.

A permission prompt is a `session/request_permission` sent to every client, the TUI's dialog
included; the runner offers the leader's own options except `enable-always-approve` — Grok's "yes,
and don't ask again for anything", which is a permission policy and so `session.set`'s business —
and whichever side answers first wins. `pending_interaction {tool_call_id, kind: "permission"}` and
`interaction_resolved {tool_call_id}` are broadcast around every prompt whoever answers, so a
request the terminal answered ends here as `decision: {option_id: "elsewhere", by: "terminal"}` and
the leader is told `cancelled`. Sessions the device creates on the leader are what a later
`grok --resume <id>` joins live, which is the reason device-driven sessions take the leader too
whenever the flag is on.

After `grok update` the leader keeps serving the build it started with; `_meta.agentVersion` against
`agent --version` is the drift. When they differ, no terminal is registered and no turn the device
drives is running, the service runs `agent leader kill` once per fifteen minutes and reconnects,
which starts a fresh leader; each attached session gets one notice. With a terminal registered the
drift is left alone, because a restart would take that terminal's session down with it.

### Terminal sessions outside the leader

Grok writes every session to `~/.grok/sessions/<percent-encoded cwd>/<session-id>/`, and
`updates.jsonl` there is an append-only log of the same ACP updates — written whether the session is
driven over ACP or by a person at the TUI. A `grok` the leader does not have — `use_leader` off, a
sandbox profile on, or one started before the flag was set — is mirrored from that log: a file tail
that needs no attachment and no change to anybody's configuration, `control: "terminal"`, and the
apps' hint saying what to run.

- `summary.json` is the session row: `info.cwd`, `generated_title` (Grok titles its own sessions, so
  there is no first-prompt guessing), `current_model_id` and `reasoning_effort`, which are published
  as `meta` the moment the session is adopted.
- A turn is over at the `turn_completed` row; the session reads `running` while a prompt's updates are
  flowing and `readonly` once that row lands.
- `_meta.eventId` is `<sessionId>-<n>`, a monotonic counter, and it is the resume cursor: rows at or
  below the last applied `n` are skipped. It is a floor, not a running high-water mark, because Grok
  writes ids slightly out of order — an `agent_message_chunk` can land after the hook rows that follow
  it, and filtering against the maximum would drop the agent's reply.
- Whether a terminal holds the session comes from `~/.grok/active_sessions.json`. The old fallback,
  the processes holding `updates.jsonl` open, never fires: nothing holds that file open in either
  mode (a standalone TUI holds `events.jsonl`; on the leader, the leader does).
- A session this device drives is never ingested from disk: it is `origin: "remote"` in the hub, which
  is the same rule that keeps Claude and Codex from doubling their own events.

### What is not covered

- **Approvals have not been seen live.** The handler answers `session/request_permission` with the
  options the agent sent, styled from ACP's `allow_once` / `reject_once` kinds, and replies
  `{"outcome": {"outcome": "selected", "optionId": …}}`. The authorized runs never triggered one, so
  the shape comes from the protocol, not from a recording.
- **`session/cancel` is sent as a notification.** As a *request* it answers "Method not found" on
  grok 1.0.25, which is what ACP says: cancellation has no reply. The notification form is untested.
- **Questions.** Grok asks the user through its own `ask_user_question` tool rather than an ACP
  request, so no `question` block is raised for one; it appears as a tool call.
- **Attachments travel as paths.** This build answers `promptCapabilities.image: false`, so a prompt
  carries no file: the device writes each one to disk and the prompt names it, for Grok to open with
  `read_file` (A49; see "Attachments" below), on a private child and on the leader alike.
- **Approvals on the leader were seen once.** One TUI-driven `rm` sent `session/request_permission`
  to the joined device client and drew the TUI's dialog; the device's answer resolved both. Later
  probes had Grok's default mode allow `rm` and writes outside the workspace by itself, so no second
  run was obtained; whether the TUI draws a dialog for a turn the device started, and whether
  `session/set_mode` from the device moves the TUI's mode, are unverified.
- **The drift restart** ran only against fakes; a real leader older than the binary was not
  produced. `agent leader kill` finds only `$GROK_HOME/leader.sock` and `leader-*.sock` there.

## pi

pi is the only agent this device extends rather than merely drives. `rc-client pi setup` copies one
file — `remote-control.ts`, shipped inside the wheel — into `~/.pi/agent/extensions/`, which pi
discovers on its own without the person's `settings.json` ever being opened. From then on every pi
process on the machine loads it, and that extension is what `attach: "extension"` names: it dials a
socket in the device home, says which session it is in, streams pi's own events out and carries out
what an app sends back. A pi somebody starts in a terminal is therefore a `shared` session, exactly
as a Codex TUI under the shared daemon is (A26).

Three rules hold the extension to its place. It never breaks pi: every handler is wrapped, and the
`tool_call` handler twice over, because an error thrown there would block the tool. It does nothing
at all when no daemon is listening — no dialog, no status line, no output — so a machine that has
never been enrolled keeps the pi it has always had, and the permission modes below apply only while
an app is actually there to answer. And it writes nowhere but the socket.

`attach_ready` is the installed file being byte-equal to the one in the running wheel. Nothing else
counts as current, so an `rc-client` update that changes the extension runs `pi setup` itself when a
copy is installed, and every pi started afterwards loads the new one. Where that step is skipped or
fails, `attach_ready` is false until `pi setup` runs again, and until then the device passes its own
copy to the sessions it starts with `pi -e <bundled path>`. A `globalThis` marker inside the file makes the second load of a double-loaded
process a no-op.

### Sessions this device starts

One long-lived `pi --mode rpc --session-id <uuid> --no-approve [--model <provider/id>]
[--thinking <level>]` child per session, speaking JSONL on its stdin and stdout. The session id is
the device's own — pi's `--session-id` creates the session when it does not exist and reopens it
when it does, so starting and resuming take the same path and nothing is rekeyed. `--no-approve`
is deliberate: it stops pi trusting the working directory's own `.pi` settings, resources and
extensions, so a remote session never starts running code that happens to be checked into the
repository.

Framing is strict: LF is the *only* record delimiter, because U+2028 and U+2029 are valid inside
pi's JSON strings. The device reads the stream as bytes, where `readline` splits on 0x0A alone and
no other character's UTF-8 encoding contains that byte.

Commands and events are decoupled: the reader answers responses immediately and hands events to one
worker, in order. A handler may therefore send a command of its own, which is how a turn's totals
are fetched when it ends.

| Command | Used for |
| --- | --- |
| `prompt` | a turn. `images` carries an attachment, steered or not; sent mid-turn with `streamingBehavior: "steer"`, which is the `steer` capability |
| `clear_queue`, then `abort` | Stop. That order is pi's own: `abort` alone resumes with whatever is still queued |
| `set_model` (provider and id apart), `set_thinking_level` | live setting changes |
| `get_state` | at startup: the model pi actually resolved, the thinking level, and the session id, published as `meta` (A17) |
| `get_session_stats` | at the end of every turn: tokens, real cost in dollars, and context usage |

Even here the extension is loaded, because approvals have nowhere else to come from: the child's
link announces itself as an `rpc` session, is told not to stream — the events are already arriving
on stdout — and carries the questions and their answers and nothing else.

### The socket

`state/pi-extension.sock` in the device home, beside the channel socket and under the same rules:
owner-only, and a home deep enough to overflow the 104-byte `sun_path` limit falls back to a short
per-user directory under the system temporary directory. Anything that can write to it can inject
prompts into a live agent and approve its tool calls. Children this device starts are told the path
in `RC_PI_SOCKET`; a terminal pi derives the same default for itself from `RC_CLIENT_HOME` or
`~/.rc-client`.

JSONL, LF only, one object per line. Both ends are ours, so the vocabulary is closed.

| From the extension | Carries |
| --- | --- |
| `hello` | the opening frame: pi's session id, session file, cwd, pid, `ctx.mode` (`tui` or `rpc`), model, thinking level, session name, and the branch the session already holds. Re-sent on every reconnect, with the branch as it stands then |
| `event` | one of pi's agent events, verbatim, so the same translator serves terminal and RPC sessions. `agent_end` and `turn_end` are trimmed to their stop reason, because both otherwise repeat the whole run's messages |
| `input` | a prompt pi accepted: `source` `interactive` (the keyboard), `extension` (one this device injected, with the app's own `block_id`) or `rpc` |
| `ask` | a tool call the extension has stopped, waiting for a decision |
| `ask_closed` | the terminal's own dialog answered first |
| `reply` | the answer to one `command` |
| `bye` | `session_shutdown`: pi is going |

| From the device | Carries |
| --- | --- |
| `welcome` | the session's permission mode, and whether this link should stream events |
| `command` `send` | text, `block_id`, optional `images`, optional `deliver: "steer"`, optional `expand` and `echo`. `expand` lets pi dispatch an extension command and expand a skill command or a prompt template before the turn; `echo: false` drops the `input` frame for that one injection, because the device has already drawn its bubble (A27) |
| `command` `abort` | `ctx.abort()` |
| `command` `set_model`, `set_thinking`, `set_permission_mode` | live setting changes |
| `command` `commands` | `pi.getCommands()`: the extension commands, prompt templates and skills this session offers (A27) |
| `command` `compact` | `ctx.compact()`, answered when the compaction finishes or when pi refuses it (A27) |
| `command` `stats` | the session's totals, summed from the branch, in the shape `get_session_stats` returns |
| `answer` | an app's decision on an `ask` |

The reading of that socket and the handling of it are separate tasks. A handler sends commands of
its own — a turn's totals when it ends — and only the reader can deliver the reply, so handling a
frame on the reading task would make it wait for itself.

### Approvals

pi runs every tool it decides to run; it has no permission system, no approval event and no
allow/deny rules. The three modes an app sees are therefore the device's own, enforced by the
extension in its `tool_call` handler (A26, PROTOCOL.md 4.3):

| Mode | Asks before |
| --- | --- |
| `untrusted` | every tool |
| `on-request` (default) | `bash`, `edit`, `write`, and every tool that is not one of pi's built-in readers `read`, `grep`, `find`, `ls` |
| `never` | nothing, which is pi's own behaviour |

Asking publishes an ordinary `approval` block offering Allow, Allow for this session and Deny. Allow
returns nothing and the tool runs; Deny returns `{ block: true, reason: "Denied from SideCode" }`,
which pi reports to the model as a failed tool result without ending the turn; Allow for
this session remembers the tool name for the life of that pi process.

In a terminal session the extension opens pi's own dialog as well and races it against the app
(A20): whichever answers first wins, the loser is dismissed, and a question the terminal took
resolves in the apps as `elsewhere`. In an RPC session there is no terminal at all, so the question
goes to the daemon alone — calling `ctx.ui` there would make pi raise a second copy of it through
its extension UI protocol.

### Slash commands

pi calls three things commands — the prompt templates and the skills it finds on disk, and the
commands an extension registers — and lists all three the same way: `get_commands` on an RPC child,
`pi.getCommands()` inside the extension for an attached terminal. Neither list carries any of pi's
own TUI commands, and that is deliberate on pi's part: its documentation says they are handled only
in interactive mode and would not execute if sent as a prompt, and a probe confirms it — `/session`
reaches the model as literal text. So the device offers exactly one built-in of its own, `compact`,
which pi exposes as a command rather than as text, and groups the rest as `Extensions`, `Prompts`
and `Skills`.

Running one takes one of two paths. `compact` is pi's own call — the `compact` command on an RPC
child, `ctx.compact()` through the extension on an attached one — and its refusal is the app's
error: "Nothing to compact (session too small)" comes back as `bad_request` carrying pi's words.
Everything else is sent as the text of a turn, `/name argument` exactly as it was typed, and pi
expands it: a prompt template becomes its body with the arguments substituted, a skill command loads
the skill, an extension command runs outright without any turn at all. That last case is why the
device draws the bubble itself and opens no turn: pi raises no `input` event for an extension
command and starts no run, so a turn opened here would never end. The turn, when there is one, opens
on `agent_start` like a turn somebody typed in the terminal.

pi's list carries no argument hint — only a name, a description and where the entry was loaded from
— while the terminal's own `/` menu shows one. The device closes that gap by reading `argument-hint`
back out of the file the entry names, which is a file on this machine. A command with no file of its
own, such as pi's bundled llama.cpp extension, reports a `<inline:…>` placeholder instead of a path
and simply gets no hint. A name that could not survive the protocol's `Command.name` pattern is
dropped rather than offered, and a description longer than one row is cut, because some skills write
a paragraph of trigger words.

A session with no live process is answered from disk, and it is answered the way that session would
actually be resumed. `pi --mode rpc --no-approve` makes pi ignore the working directory's own `.pi`,
so only the global directories count: `~/.pi/agent/prompts/*.md` non-recursively, and the skills
under `~/.pi/agent/skills` and `~/.agents/skills`, walked pi's way — a directory holding `SKILL.md`
is one skill and is not descended into. A project's own templates are never promised, because a
resumed session would not have them.

### Terminal sessions

A `hello` from a `tui` process creates or revives a session keyed by pi's own session id, with
`control: "shared"`, the cwd, model and thinking level the frame carries, and the branch replayed
once as history in the entries' own timestamps — so an app opening a conversation somebody started
an hour ago reads it from the beginning. Everything an app can do on a Codex shared session works
here: send, steer, stop, model, thinking level, permission mode, images and approvals, all of it
through the socket. A message an app sends gets its bubble when pi's own `input` event comes back,
which is where the terminal shows it too and under the id the app already drew; a steered one waits
for pi to take it off the steering queue, as amendment A14 requires.

`session_shutdown`, or the socket closing, drops the session to `control: "none"` — pi has exited,
and the next `session.send` resumes it with `pi --mode rpc --session-id <id>` in the same working
directory, which reopens the same conversation with its history intact. `/new`, `/resume` and
`/fork` in the TUI are a `session_shutdown` and then a `hello` under another id: two sessions, not a
rename. A terminal pi started before the daemon came up still appears, because the extension retries
the socket with backoff and sends its `hello` when it connects.

### Steering

pi delivers a steered message "after the current assistant turn finishes executing its tool calls,
before the next LLM call", and reports its whole steering queue in `queue_update` whenever it
changes. A message that has left the queue is one the agent read, and that is where amendment A14
says its `user_message` belongs, so the bubble is published then — after the output that preceded
it, not where it was sent. A message the turn ended without ever reading is published at the end of
the turn, with a warning `notice` when the turn was interrupted. Stop clears the queue first, so a
message cleared out of it counts as never read.

### What the stream carries

| Event | Becomes |
| --- | --- |
| `message_update` with `text_delta` / `text_end` | `assistant_text` deltas, closed with the whole text |
| `message_update` with `thinking_delta` / `thinking_end` | `thinking`, the same way |
| `message_update` with `toolcall_start` / `toolcall_delta` | nothing: the tool events below carry the same call whole |
| `tool_execution_start` / `_update` / `_end` | one `tool_call` block keyed by `toolCallId`. `partialResult` is the output so far, not a delta, so it replaces rather than appends; a running call's output is republished at most twice a second |
| `tool_execution_end` of an `edit` | the `diff` block: pi returns the unified patch it applied in `result.details.patch` |
| `message_end` with `stopReason: "error"` | an `error` event carrying pi's own message |
| `agent_start` | the turn, when nobody here opened one — a prompt typed in the terminal, or a retry pi began on its own |
| `queue_update` | not published. It is how a steered message's bubble is placed (above) |
| `compaction_end`, `auto_retry_start` | `notice`. An attached terminal has no `compaction_end` of pi's own, so the extension makes one out of `session_compact`, which is how an app learns that a compaction the terminal ran — typed or automatic — happened at all. A compaction an app asked for and pi refused publishes no notice: that refusal is already the reply to `session.command` |
| `agent_settled` | `turn_completed` |
| `turn_start`, `turn_end`, `agent_end`, `bash_execution_update`, everything else | nothing |

`message_update` is delta-only: it carries neither the cumulative message nor the partial content,
so a block's text is assembled from its deltas under the `contentIndex` the delta names.

The turn ends on `agent_settled`, not on `agent_end`: `agent_end` is one low-level run, which may
still be followed by a retry, a compaction or a queued continuation, and ending the turn there would
cut a turn in half. If pi exits while a turn is running, the open blocks are closed, an `error` is
published and the turn ends rather than streaming for ever.

Tool kinds come from pi's built-in tool names: `read` and `ls` are `read`, `bash` and `powershell`
`shell`, `edit` `edit`, `write` `write`, `grep` and `find` `search`. Anything else is an extension's
own tool and is `other`; its title is the first string argument it was given.

### Attachments

pi takes images and nothing else. An image rides as pi's own `ImageContent` —
`{type: "image", data, mimeType}`, where `data` is the upload's `data_base64`, checked as base64 of
at most 6 MiB — on `prompt.images` for an RPC session and inside the message content for an
attached one, on a steer as on any other message; the nested `source` form pi's documentation also
shows is not what the running binary accepts. The `user_message` names the images as
`{name, mime, size}`. Any other attachment refuses the whole message with `unsupported` before pi
is sent anything; a held message meets the same refusal, as an `error`, when it goes out
(`agents/pi/images.py`).

### What is not covered

- **`/tree`, `/fork` and `/clone` move the branch, and only the branch is replayed.** A pi session
  is a tree, and `hello` carries `getBranch()`, which is the path the session is on at that moment.
  Navigating to another branch inside the TUI is not republished; the app keeps the transcript it
  already has.
- **Totals on an attached session are summed by the extension** from the assistant messages of the
  branch, because the socket has no `get_session_stats` of its own. Usage a compaction or a branch
  summary billed is not in that sum.
- **Thinking levels are not clamped.** pi accepts a level the current model does not expose and
  decides for itself what it means; a session started with `--thinking off` on a model whose floor is
  higher reports the level pi actually settled on.
- **Whether `abort` always settles.** The turn is ended on `agent_settled`; if it does not arrive
  within sixty seconds of the abort, the device ends the turn itself with a warning.
- **A prompt template or skill a project holds is invisible to a session this device starts.**
  `--no-approve` is what keeps a remote session from running code checked into the repository, and
  project-local prompts and skills are part of what it withholds. An attached terminal pi whose
  project the person trusted does list them, and running one from an app works.
- **A second device on the same machine** would install the same extension at the same path and
  both would be dialled, one socket each. Nothing shares a session between them.

## Attachments

Claude, Codex and Grok read files from disk far more reliably than they accept inline binary
payloads, so an attachment sent with a message is written to disk and the prompt gains the
resulting paths: `Attached files:` and one `- name: path` line per file. Claude Code opens them with
`Read` and Grok Build with `read_file`, both of which read images, on a session started from an app
and on an attached terminal alike; neither is sent an image block, because Grok answers
`promptCapabilities.image: false` and a Claude channel carries text (A49). Codex takes its images as
`localImage` inputs and finds only the other files in the prompt; pi takes images inline (see pi).
Limits are 8 attachments and 6 MiB each after decoding.

Every message gets a folder of its own, `state/attachments/<session>/<message>/<name>`, with a fresh
twelve-character `<message>`, folders `0700` and files `0600`. A name is sanitised, and numbered
(`shot-2.png`) when the same message already has it, so no file ever overwrites another; a message
refused for one bad file writes nothing.

The apps see what the person sent: the `user_message` carries the typed words and
`attachments: [{name, mime, size}]`, and a held message's `queue` entry counts its files (A43). A
prompt read back from an agent's own record — a Claude transcript, Grok's `updates.jsonl`, the
leader's replay or echo — whose last lines name files under `state/attachments` is published the
same way: the words, and for each file its name, the type its extension suggests, and its size while
it is still there (0 once it has gone). A path never reaches an app: a title or an error the agent
words itself that quotes one is published with the file's name in its place, whether it quotes the
path as the prompt gave it or writes it its own way — from `~`, from the folder it works in,
through a link — as long as it ends in `attachments/<session>/<message>/<name>`. A message of files
and no words names its session after the files, since the agent's own title for it could only be
made of their paths (see "Titles").

Opening one of those files asks nobody. When Claude asks to `Read` — through the SDK's `can_use_tool`
on a session the device started, through the channel's permission relay on an attached one, whose
preview must parse as the tool's input — or Grok asks `session/request_permission` for its own
`read_file` (named so by `_meta["x.ai/tool"]` in the `grok_build` namespace, or by `rawInput.variant`
`ReadFile`), and every path the request names lies in this session's own folder under
`state/attachments`, written as the device wrote it, a regular file with one name and no link
anywhere from the root down to it, the device allows it at once and raises no approval. For Grok
that is the `allow-once` option and never another, and a `rawInput` key beyond the paths, `variant`,
`pages`, `format`, `offset` and `limit` is asked about. Any other tool, path or session is asked
about as before.

Nothing is kept for long:

- The daemon sweeps once when it starts and then every hour, off the event loop: every file older
  than 7 days is deleted, then the oldest files until the folder holds no more than 512 MiB, then
  every folder left empty except one changed in the last minute, which a send may be filling. Files
  an earlier build wrote straight into `<session>/` are swept the same way. The limits are constants
  in `rc_client/attachments.py`, not settings.
- Deleting a session deletes its folder.
- A message held for an attached Claude terminal writes its files when it is sent, so they are there
  when the CLI takes it. Taken out of the queue — `session.queue_remove`, which an edit begins with —
  it takes its folder with it at once; when the attachment drops, or the bridge refuses a message,
  the folders of the messages still held go too, and whatever delivers them later writes them again
  from the bytes the message kept.
  A message whose files the size cap took while it waited writes them again before the CLI gets
  its words. A message held for a session the device drives writes nothing until it goes out. The
  queue lives in memory, so the daemon stopping deletes the files of every message still held.
- A send that fails before the agent has the prompt deletes its folder: the SDK call that hands
  Claude the prompt failing, or Grok refusing `session/prompt` as it stood — a JSON-RPC
  invalid-request, method-not-found or invalid-params error — before it said anything after that
  prompt. So does a send stopped on its way, as a close stops a drain (A39): the bubble it already
  showed is followed by a `warn` notice that the agent was stopped before it read the message, and
  the turn it opened ends as `interrupted`. Any other failure keeps the files, which Grok may have
  opened, until the age limit.

The state folder may be reached through a link — it is resolved — but `state/attachments` itself
must be a real folder: if it is a link, attachments are refused with an error and nothing is
written, swept, deleted or recognised through it. Below the root none of this follows a link or
touches anything outside it, a file that vanishes meanwhile is skipped, and the log says how many
files and bytes went, never a name or a path. For Claude and Grok the files are written and deleted
off the event loop; Codex's sends are unchanged.
`rc-client uninstall --purge` removes the whole device home, attachments included.

## Logs

Logs are structured JSON on stderr and never contain tokens, pairing codes, prompt text or tool
output. On macOS the service writes them to `~/.rc-client/logs/`; on Linux they go to the journal.
Raise detail with `rc-client --log-level debug run`.

## Network path

The daemon dials the gateway directly unless the device was enrolled with a proxy, and it never
consults the environment it happens to run in: the WebSocket link, enrollment, pairing and the
`self-update` download all use the one URL `config.toml` holds, and `""` means a direct dial. The
link is a long-lived tunnel to a gateway the operator runs, so a proxy in the middle is something a
person asks for, not something the machine decides. How to ask, and how `--proxy env` is resolved on
the enrolling host, is "Reaching the gateway through a proxy" above.

The environment is ignored on purpose. Before that was pinned down, a machine with a SOCKS proxy in
its macOS network settings logged `gateway link lost` with an `ImportError` on every reconnect and
never came up: the client library had adopted the system proxy on its own, and SOCKS support needs
a package this client does not ship — which is why a `socks5://` setting is refused outright today.
A `gateway link lost` line carries the exception message, so a failure of that kind is readable in
`~/.rc-client/logs/rc-client.err.log`.

### Reconnecting, and when not to

The close code decides (PROTOCOL.md 2.5). `4401` and `4403` are the credential's answer: the link
stops, logs `gateway link stopped` with the reason, and writes `state/link.json`, so
`rc-client status` prints a `gateway link` line saying the gateway rejected this device and to run
`rc-client enroll` again. Nothing else in the daemon stops. `4001` means a newer connection replaced
this one, and racing it back would only replace that one in turn, so the reconnect waits at least
fifteen seconds. Everything else is transient and retried with backoff, and only a connection that
stayed up for thirty seconds resets it.

Work that belongs to a fresh connection — the mirror scan the gateway backfills from — runs beside
the frame pump, never in front of it. Holding the pump back left every forwarded request unanswered
until that work finished, including the ones it was meant to answer, and the backfill request timed
out against a device that was connected and silent.

### Staying on time

Two things used to put the loop on the disk's clock. Registry writes committed one per published
event, and a commit is a flush; they are now queued and applied in batches by a single worker
thread, with reads applying whatever is queued before they run so nothing observes a missing write.
`seq` is still handed out synchronously and in order, from memory, and is written back with the
events it numbered, so a batch lost to a kill takes its events and its counter together and no `seq`
is ever reused. Pruning a session's history no longer counts its rows first; it deletes below a
watermark the index finds by skipping to it.

A probe task asks only to be woken every second and logs `event loop stalled` with how late it was
when the answer takes more than five seconds. A device that goes quiet is either a bad network or a
stalled loop, and the two are fixed in different places.

## Uninstalling

```sh
rc-client uninstall           # stop and remove the service and the shim, keep the data
rc-client uninstall --purge   # also delete config, state and logs
```

`uninstall` also deletes `bin/claude` and `state/claude-settings.json`, takes the pi extension back
out of `~/.pi/agent/extensions/`, and strips the `# >>> remote-control >>>` block from your shell
startup file. Pass `--no-shell-rc` to leave that file alone. pi, Claude Code, Codex and Grok Build
themselves are left exactly as they were, and so is the `[cli] use_leader = true` that
`rc-client grok setup` wrote to `~/.grok/config.toml`: turn it off there if you no longer want
Grok's leader mode.

or, from the gateway's own script:

```sh
curl -fsSL https://rc.example.com/install.sh | sh -s -- --uninstall
```

Removing the device from the web UI revokes its token and drops its sessions from the gateway index.
The machine keeps its agents, its transcripts and its own history.

## Safeguards

- The machine's own settings never answer a Claude tool approval meant for a remote user: remote
  sessions load only project and local settings (next section), and a request the machine still
  answers by itself emits a warning notice instead of failing silently.
- The mirror skips any session whose persisted `origin` is `remote`, so a daemon restart does not
  re-read a session the daemon drove itself and duplicate its transcript.
- A stopped turn reports all-zero token counts; those never overwrite `Session.usage`, and a payload
  with no counts leaves the previous total in place.
- A fresh device's initial mirror import is capped at 50 sessions from the last 14 days, both
  configurable.

## User-level Claude settings are not loaded

This is the trade-off behind the first safeguard, and it changes what a remote Claude session can do.

A remote session must never be approved by the machine on behalf of someone who is not looking at
it. The Claude CLI loads user-level settings by default, and those settings can carry auto-approval
(`permissions.defaultMode: "auto"`, `autoMode`, and `PermissionRequest` or `PreToolUse` hooks that
answer for you). So the daemon starts remote sessions with `setting_sources` set to `project` and
`local` only.

The practical effect: **anything scoped to your user does not apply to a remote session** — user-level
MCP servers, skills, hooks, and `permissions.defaultMode`. Project settings (`.claude/settings.json`
in the working directory) and local settings (`.claude/settings.local.json`) do apply, so per-project
MCP servers and hooks keep working.

To change it, add to `~/.rc-client/config.toml`:

```toml
[claude]
setting_sources = ["user", "project", "local"]
```

Accepted values are `user`, `project` and `local`; anything else is ignored. Adding `user` back
restores your full local configuration and, with it, the possibility that the machine approves a
tool call before you see the card. When that happens the session shows a warning notice saying so.

## Configuration

`~/.rc-client/config.toml` is written by `rc-client enroll` and can be edited afterwards. Restart the
daemon for a change to take effect.

| Key | Default | Meaning |
| --- | --- | --- |
| `gateway_origin` | from enrollment | The gateway this device dials |
| `device_id`, `device_token` | from enrollment | The device's identity and its bearer token |
| `name` | the hostname | The name the device enrolled with, which the apps show until it is renamed there. The daemon still sends it in `hello`, where the gateway does not apply it, so editing it afterwards renames nothing (A50) |
| `[mirror] max_sessions` | `50` | How many terminal sessions the initial mirror import publishes |
| `[mirror] max_age_days` | `14` | How far back that import reaches |
| `[claude] setting_sources` | `["project", "local"]` | Which Claude settings layers a remote session loads |

Environment overrides, useful for development and for unusual installs: `RC_CLIENT_HOME` moves the
whole directory, `RC_CLAUDE_BIN` and `RC_CODEX_BIN` pin an agent executable, `CODEX_HOME` moves the
Codex home the daemon reads and the standalone build inside it, and `CODEX_INSTALL_DIR` moves the
directory the official Codex installer symlinks into. The last two are read here because Codex's own
installer reads them; setting either to something the installer does not use would put `codex setup`
and Codex out of step.

## Known limitations

- Linux has never been run: neither the systemd unit nor `service install`.
- Codex sessions cannot be taken over, by design: on the shared daemon they are attached instead.
- A successful `session.takeover` has not been exercised end to end; the refusal path has.
- `todos` events were unit-tested but never driven through a live agent.
- The session-scoped grant (`allow_session`) and an approval for a command have not been exercised
  end to end, so it is unconfirmed that a second tool call runs unattended after a session grant.
- Killing the daemon can orphan a `claude` child that holds a transcript open for one more scan, so
  a restarted daemon may briefly report a remote session as terminal-controlled. It self-corrects.
- A mirrored session reports `readonly` during a long *silent* tool call, because the mirror infers
  activity from transcript rows arriving and a `sleep 45` writes nothing.
- The process scan runs about every ten seconds, so a very short terminal session can finish before
  the mirror sees the CLI holding it and appears directly as `control: "none"`. Only the live-control
  window is missed, never the timeline.
- A build with no `$CODEX_HOME/packages/standalone` cannot run any `daemon` subcommand, which is why
  `codex setup` installs the standalone build rather than asking anyone to remove anything. An
  npm-launched TUI joins a running daemon like any other.
- The Codex daemon path is macOS only so far. The systemd unit that supervises it renders and is
  unit-tested, but has never been enabled on a real Linux machine.
- A Codex thread created from an app cannot be reopened with `codex resume` until its first turn
  exists, because the rollout the CLI resumes from is written when a turn starts.
- Nothing tells the device that a Codex TUI has exited: on Codex 0.154.0 neither a `/quit` nor a
  killed pane emits anything to another subscribed client, and the thread stays in
  `thread/loaded/list` long afterwards. The device therefore matches TUI processes to
  threads by working directory and by count, which still mis-answers where the directory holds more
  threads than terminals. A TUI resumed with `codex resume` from a directory other than the thread's
  own is invisible to the scan, so the thread reads as `none` once it has been silent for one scan;
  sending from an app still reaches it. Two TUIs in one directory are indistinguishable from each
  other, so which of that directory's threads each one is credited with is a guess, ordered by what
  the daemon last saw used. The count is what bounds the damage: one terminal speaks for one thread,
  never for the whole directory.
- Whether the daemon would unload a thread once its last subscriber leaves is unverified: this
  device subscribes to every loaded thread, so there is no moment without one, and answering it
  would mean stopping the user's own client.
- On Codex 0.154.0 the daemon's originator and user agent are set globally by whichever client
  connects first, so with the device first, terminal threads read `remote-control`; newer releases
  may assign provenance differently. On Codex 0.160.0 a terminal thread reads
  `originator: "codex-tui"`, `source: "vscode"` in `thread/list`, `thread/read` and its rollout's
  `session_meta`; A48 accepts that exact pair.
- A session that is attached before its first turn has no transcript, so after a daemon restart it
  is invisible to the scan that promotes a live CLI to `control: "terminal"`. It has no content
  either; the next turn creates the transcript and the session appears normally.
- `rc-client uninstall` and `rc-client service stop` reach launchd by service label, which is
  per-user and not per-`RC_CLIENT_HOME`. Running either against a scratch home therefore stops the
  real service as well. Restore it with
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.remote-control.client.plist`.
- `attach_ready` reports whether the *daemon's* `PATH` resolves `claude` to the shim, which the
  service files set at install time. It does not prove that your interactive shell does, so a
  session started before you opened a new terminal shows as `terminal`, not `shared`.
