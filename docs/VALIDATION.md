# Validation

What to run before a change lands, and a short end-to-end smoke flow for a release. The same
static checks run in CI (`.github/workflows/ci.yml`) on every push to `main` and every pull request.

## Checks

Run each from the repository root. They need `uv` (Python 3.12) and Node 22 or newer. The
second mypy pass checks the Linux view of `sys.platform` branches, which CI sees and a Mac does
not.

```sh
(cd protocol && uv run --no-project --with jsonschema python scripts/validate_fixtures.py)
(cd gateway && uv sync --locked && uv run ruff check . && uv run ruff format --check . \
  && uv run mypy rc_gateway tests && uv run mypy --platform linux rc_gateway tests && uv run pytest -q)
(cd client && uv sync --locked && uv run ruff check . && uv run ruff format --check . \
  && uv run mypy rc_client tests && uv run mypy --platform linux rc_client tests && uv run pytest -q)
(cd web && npm ci && npm test -- --run && npm run typecheck && npm run lint && npm run build)
docker build -f gateway/Dockerfile -t rc-gateway:check .
```

| Check | What it covers |
| --- | --- |
| `validate_fixtures.py` | Every fixture against its JSON Schema, the rules the schema cannot express, the invalid fixtures, and coverage of every frame, event and HTTP body |
| Gateway | Lint, formatting, types and the unit and integration tests, including the protocol fixtures replayed against the real routes and the voice assistant's calls against a fake Live API |
| Client | The same for the device daemon, with the agents' recorded transcripts and handshakes as fixtures |
| Web | The component and store tests, the TypeScript check, lint and the production build |
| Image | The release image: the web build, the `rc_client` wheel and the gateway in one container |

A change to the wire contract changes `protocol/PROTOCOL.md`, the schema, the fixtures and the
validator's inventory together, and every component's checks must pass against it.

`client/tests/test_real_agents.py` drives one real turn per installed agent and is skipped unless
`RC_REAL_AGENTS=1` is set:

```sh
(cd client && RC_REAL_AGENTS=1 uv run pytest -q tests/test_real_agents.py)
```

## Smoke flow

Roughly fifteen minutes, one short turn per agent, on a machine with Claude Code and Codex
installed. Use scratch directories so nothing touches a real installation.

```sh
# 1. Gateway from source, on a port the web mock does not use
cd gateway
PUBLIC_ORIGIN=http://127.0.0.1:8791 RC_PASSWORD=devpassword \
  DATA_DIR=/tmp/rc-smoke-data RC_HOST=127.0.0.1 RC_PORT=8791 uv run rc-gateway

# 2. Pair this machine, in a second shell
curl -s -X POST http://127.0.0.1:8791/api/login \
     -H 'content-type: application/json' \
     -d '{"username":"admin","password":"devpassword"}'                    # -> token
curl -s -X POST http://127.0.0.1:8791/api/devices/pairing \
     -H "Authorization: Bearer $TOKEN"                                     # -> RC-XXXX-XXXX
cd client
export RC_CLIENT_HOME=/tmp/rc-smoke-home
uv run rc-client enroll --gateway http://127.0.0.1:8791 --pair RC-XXXX-XXXX
uv run rc-client run
```

Then, from the web app at `http://127.0.0.1:8791` (build it once with `cd web && npm run build`), or
as an app on `WS /ws/app` with `Authorization: Bearer <token>`:

1. Start a Claude session in a git repository with "Reply with exactly OK" and watch
   `turn_started`, streamed text and `turn_completed` with usage arrive.
2. Ask it to create a file, answer the approval card with Allow, and confirm the file exists.
3. Send a long task and Stop it: the turn ends with `stop_reason: "interrupted"`.
4. Send while it runs with the queue mode: the queue snapshot shows it, and the next turn starts
   with `trigger: "queue"`.
5. Reload the conversation: history arrives in ascending `seq`.
6. Repeat 1 and 3 with Codex.
7. Mirroring: run `claude -p "Reply with exactly OK"` in another repository. A session appears with
   `origin: "terminal"`; after it exits, a send resumes it.
8. Restart `rc-client run` and confirm the sessions return with `seq` unbroken. Restart the gateway,
   confirm the device redials and a subscribe with a stale cursor is answered with the events the
   device holds, or with `resync: true` when the gap does not fit (A56).
9. Upgrade path: start the gateway against a `DATA_DIR` from an older build and enrol a device. A
   fresh volume hides every schema regression, so this is the check that matters before a release.
10. Docker: write a root `.env` with `PUBLIC_ORIGIN=http://127.0.0.1:18787`, `GATEWAY_PORT=18787`,
    `GATEWAY_BIND=127.0.0.1` and an `RC_PASSWORD`, then `docker compose build && docker compose up
    -d`. Check `GET http://127.0.0.1:18787/api/health` and its security headers, install a device
    with the one-liner from `POST /api/devices/pairing`, run one turn, then `docker compose down -v`
    and delete the `.env`.
11. Attaching: `rc-client shim install --no-shell-rc` in the scratch home, restart the daemon, start
    an interactive `claude` with the shim first on `PATH`, and confirm the session appears as
    `control: "shared"` and takes a message from the app. For Codex, `rc-client codex setup`, then a
    bare `codex` in a scratch repository appears as `control: "shared"` too. Never run
    `rc-client uninstall` or `service remove` on a machine that carries a real installation.
12. Continuity (A56): keep a session running with its conversation open in the web app, then swap
    the gateway once (`docker compose build gateway`, `docker compose up -d --force-recreate
    gateway`; the first swap onto A56 has no restart grace, so use a later one). The transcript
    keeps its rows and the reader's place, holds everything the session did meanwhile once and in
    order, and the device row stays online; the gateway logs `session subscribed` and `session
    caught up` for the session. Then serve a newer client build while a turn runs: the device keeps
    its old build until the session has been quiet for two minutes, and the gateway then logs
    `automatic client update`.

Delete `/tmp/rc-smoke-data` and `/tmp/rc-smoke-home` afterwards.
