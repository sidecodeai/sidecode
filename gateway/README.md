# rc_gateway

The remote-control gateway. It is the only component apps and devices talk to: it authenticates
users, enrolls devices, routes requests between apps and devices, keeps a session index and a
bounded replay buffer, proxies speech-to-text, sends push notifications and serves the web UI plus
the client installer.

Wire protocol: `protocol/PROTOCOL.md` (frozen). The gateway implements §1, §2, §5, §6 and §8.

## Run from source

```sh
cd gateway
uv sync
uv run rc-gateway
```

Configuration comes from the repository root `.env` (see `.env.example`); `python-dotenv` loads it
when the gateway runs from a source checkout. `PUBLIC_ORIGIN` and `RC_PASSWORD` are required and the
process exits with a one-line message when either is missing. Everything else has a default.

The server listens on `RC_HOST:RC_PORT` (`0.0.0.0:8787`). It serves `web/dist` at `/` with an SPA
fallback; when that directory has not been built it serves a small placeholder page instead of
failing, so the API stays usable during development.

## State in `DATA_DIR`

Four SQLite files, each created 0600 in a directory created 0700, alongside the generated
`session_secret` and `vapid_private.pem`:

| File | Contents |
| --- | --- |
| `auth.sqlite3` | issued logins (`jti`, username, expiry, revocation), so a restart does not sign everyone out |
| `devices.sqlite3` | enrolled devices and pairing codes, as hashes only |
| `sessions.sqlite3` | the last known summary of every agent session, and the gateway's own `unseen` mark on it (A47) |
| `push.sqlite3` | Web Push subscriptions, APNs tokens and the delivery journal, badge-only deliveries included |

Schema changes are applied in place when a store opens: `rc_gateway/migrations.py` compares each
table against the columns added since its first release and issues the missing `ALTER TABLE`
statements. `CREATE TABLE IF NOT EXISTS` alone would silently skip an existing database, so a
column added to one of those statements must also be declared in that store's `MIGRATIONS`.

Wiping the volume signs every app out and invalidates every device token, whatever `RC_SECRET` is
set to, because a token is only honoured while its record is present here.

## Test and lint

```sh
cd gateway
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

The tests exercise the real ASGI app through Starlette's `TestClient`, including WebSockets. They
never reach the network: the STT backend, Web Push and APNs senders are injected fakes.

## Layout

| Path | Contents |
| --- | --- |
| `rc_gateway/config.py` | environment configuration, generated secrets, fail-fast validation |
| `rc_gateway/auth.py`, `origins.py`, `ratelimit.py`, `security.py` | authentication and CSRF surface |
| `rc_gateway/auth_store.py`, `session_registry.py` | issued logins, durable in `auth.sqlite3` |
| `rc_gateway/devices.py` | pairing codes and device tokens (hashed, single use) |
| `rc_gateway/index.py` | SQLite session index |
| `rc_gateway/unseen.py` | when a session carries the A47 mark: set on working to waiting, and what clears it |
| `rc_gateway/migrations.py` | additive column migrations applied when a store opens |
| `rc_gateway/hub.py`, `connections.py`, `replay.py` | WebSocket routing core |
| `rc_gateway/routes/` | HTTP endpoints |
| `rc_gateway/ws/` | `/ws/device`, `/ws/app`, `/ws/stt` |
| `rc_gateway/uploads.py` | authenticates and bounds an upload before its body is read |
| `rc_gateway/push*.py`, `apns.py` | Web Push and APNs |
| `rc_gateway/badge.py` | the A47 badge-only APNs push, sent once an account's count has been still for 3 s |
| `rc_gateway/stt.py` | OpenAI-compatible transcription client |

## Docker

`gateway/Dockerfile` builds from the **repository root** as context, because the image carries three
artefacts from sibling directories: the built web app, the `rc_client` wheel the installer
downloads, and `client/install.sh`. The root `.dockerignore` keeps that context small.

| Target | Contents |
| --- | --- |
| `gateway` | the Python service alone; useful for testing the image without the other components |
| `release` | the default: adds `web/dist`, the client wheel and the installer |

```sh
docker build -f gateway/Dockerfile --target gateway -t rc-gateway .   # from the repo root
docker compose up -d                                                   # the whole stack
```

Compose reads the repository root `.env` and publishes the gateway on `GATEWAY_BIND:GATEWAY_PORT`;
it ships no TLS terminator, so the operator's own reverse proxy owns the public hostname. The
gateway sets its security headers itself, in `rc_gateway/headers.py`. See `.env.example` for every
setting and `docs/DEPLOY.md` for the proxy configuration.

Code lifted from cc-remote is credited in `THIRD_PARTY_NOTICES.md`.
