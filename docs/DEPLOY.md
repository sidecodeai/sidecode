# Deploying the gateway

The gateway is the only thing you deploy. Devices dial out to it and apps dial in — the web app it
serves, and the SideCode apps for iPhone, Android, Mac and Windows, which are distributed separately
and connect to a self-hosted gateway as well as to the official one. Neither needs anything
installed on the VPS beyond this stack.

## Prerequisites

- A Linux host with Docker and the Compose plugin.
- A reverse proxy you run and configure yourself. The stack contains no TLS terminator: it only
  publishes the gateway on a host port. Nginx Proxy Manager, Traefik and plain nginx all work; see
  [Reverse proxy](#reverse-proxy).
- A domain whose A or AAAA record points at the host, so that proxy can get a certificate.
- About 512 MB of memory for the gateway, more if you enable the local speech-to-text profile,
  which downloads and runs a Whisper model.

## First deployment

```sh
git clone https://github.com/sidecodeai/sidecode.git && cd sidecode
cp .env.example .env
$EDITOR .env          # set PUBLIC_ORIGIN and RC_PASSWORD
docker compose up -d
docker compose logs -f gateway
```

The first run builds three stages: the web app with `npm ci && npm run build`, the `rc_client` wheel
with `uv build`, and the Python service itself. Expect several minutes. A healthy start logs
`gateway ready` with the origin, the speech-to-text provider and whether push is enabled.

Verify on the host first, straight against the published port:

```sh
curl http://127.0.0.1:8787/api/health
# {"ok":true,"version":"0.1.0","protocol":1,"auth":{"mode":"password","registration_open":false},"devices_online":0}
```

Then point your reverse proxy at that port, following [Reverse proxy](#reverse-proxy), and repeat
the check through the public hostname:

```sh
curl https://rc.example.com/api/health
```

Open the origin in a browser, sign in as `admin` with `RC_PASSWORD`, and add your first device
from **Devices → Add device**.

## Accounts

Everyone who uses the gateway has an account, and an account sees only what it owns: its devices,
the sessions running on them, its pairing codes and its own notifications. Nothing an app receives
crosses that line, and a request naming another account's device or session is answered as if it
did not exist.

`admin` is yours. It exists from the first start, its password is `RC_PASSWORD`, and it is the only
account that can reach **Settings → Users**. It is never stored in the account database, so
changing `RC_PASSWORD` in `.env` and running `docker compose up -d` changes how you sign in; the in-app password change
is refused for `admin` for the same reason. `admin` also cannot be disabled, demoted to a member or
deleted, so a gateway always has one operator.

Everyone else is a `member`. Two ways to make one:

- **Let people register.** **Settings → Users** has a switch that opens `POST /api/register`. A
  fresh gateway starts with it closed, and while it is closed the sign-in screen offers no "Create
  an account" link at all. Open it, let the people who should have an account sign up, close it
  again. Registrations are rate limited to five per minute per address, as logins are.
- **Add them yourself.** **Add user** on the same screen takes a username, a first password and a
  role.

A username is 3 to 32 characters from `a-z`, `0-9`, `.`, `-` and `_`, starting with a letter or a
digit, and is lower-cased, so `Alice` and `alice` are one account. A password is 8 to 128
characters and is stored as a scrypt hash; nothing on the gateway can read it back. Members change
their own password from **Settings → Change password**, and you can reset one from the Users screen
without knowing the old one.

**Disabling** an account signs it out of every browser and phone immediately, and closes its
devices' sockets with close code 4403. The devices stay enrolled and their tokens stay valid, so
enabling the account again brings everything back without re-pairing anything.

**Deleting** an account does what deleting each of its devices would do, and then removes the
account itself: every device token stops working, every session leaves the index, outstanding
pairing codes are dropped, push registrations are deleted and every login session is revoked. It
cannot be undone, and the confirmation names how many devices go with it.

Accounts live in `users.sqlite3` in `DATA_DIR`, next to the other databases, and are part of the
`rc-data` backup below. **After upgrading a gateway that ran before accounts existed**, everything
it already held — every enrolled device, every pairing code, every push registration — belongs to
`admin`, which is the account it effectively had. Nothing needs re-pairing.

## Pairing a device

Two flows reach the same enrolment, and both start at **Devices → Add device**.

**A typed code.** The app mints a pairing code, single use and valid for ten minutes, and shows the
command to run on the host:

```sh
curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair RC-XXXX-XXXX
```

**A scanned code.** The host runs the same installer with no code:

```sh
curl -fsSL https://rc.example.com/install.sh | sh
```

It asks the gateway for a claim token, prints `https://rc.example.com/pair#<token>` as a QR code and
waits. Scanning that code with a SideCode app, or opening the link in a signed-in browser, claims the
token for that user: the gateway mints the ordinary pairing code, hands it to the waiting host and
enrolment continues exactly as it does for a typed code. Claim tokens live in memory only, for ten
minutes. Restarting the gateway forgets them, the host's poll then reads `404` and the installer
says to run the command again. Neither minting a token nor polling for it needs a credential, so
both are bounded: twelve requests a minute per address, three outstanding tokens per address, fifty
for the whole gateway, and two polls parked on one token at a time. The gateway-wide fifty is not a
refusal — reaching it drops the oldest unclaimed token — so no caller can take QR pairing away from
everybody else for a whole ten minutes.

## `.env` reference

Only `PUBLIC_ORIGIN` and `RC_PASSWORD` are required; the gateway refuses to start without them and
names the missing one.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | — | **Required.** The exact origin apps use, `scheme://host[:port]`, no path and no trailing slash. Checked against the browser's `Origin` header on cookie-authenticated writes, and baked into the pairing command served at `/install.sh` |
| `RC_PASSWORD` | — | **Required.** The password of the `admin` account. Every other account keeps its own hashed password in `users.sqlite3`. Use a long random value |
| `GATEWAY_PORT` | `8787` | The host port compose publishes the gateway on. Your reverse proxy forwards here |
| `GATEWAY_BIND` | `0.0.0.0` | The host address that port binds to. A proxy running as a container reaches the host over the Docker bridge, so loopback only works when the proxy is on the host network |
| `RC_SECRET` | generated | Signs login tokens. Left empty, one is generated into `DATA_DIR` on first start. Set it explicitly to pin the signing key instead of depending on a file inside the volume |
| `DATA_DIR` | `/data` | Where SQLite databases and generated keys live. Backed by the `rc-data` volume |
| `STT_PROVIDER` | `none` | One of `none`, `openai`, `mimo`, `realtime`. `none` disables voice input, `openai` targets any OpenAI-compatible transcription server, `mimo` targets Xiaomi MiMo, `realtime` keeps a live session over the OpenAI Realtime transcription protocol (Alibaba Model Studio's `qwen3-asr-flash-realtime`) — the only one whose words appear as they are said. Any other value stops the gateway at startup |
| `STT_BASE_URL` | `https://api.openai.com/v1` | Base URL. `openai` posts to `{STT_BASE_URL}/audio/transcriptions`, `mimo` to `{STT_BASE_URL}/chat/completions`; `realtime` ignores it |
| `STT_REALTIME_URL` | empty | The `wss://` endpoint of the `realtime` provider, required with it. The model goes in its query; a bare URL gets `?model={STT_MODEL}` appended |
| `STT_API_KEY` | empty | Bearer token for that server. Not needed by most local servers |
| `STT_MODEL` | `whisper-1` | Model name the backend expects |
| `POLISH_BASE_URL` | empty | OpenAI-compatible base URL for dictation polish (A29); the gateway calls `{POLISH_BASE_URL}/models` and `{POLISH_BASE_URL}/chat/completions`. Empty leaves the feature off and the apps show the setting disabled |
| `POLISH_API_KEY` | empty | Bearer token for that provider. Both this and the base URL are needed to enable polish |
| `POLISH_MODELS` | empty | Optional comma-separated allowlist of model ids: it narrows and orders the list the apps offer, stands in for a provider that serves no `/models`, and makes `POST /api/polish` refuse any other model with `400` |
| `POLISH_TIMEOUT_SECONDS` | `20` | How long one polish request may take before the gateway answers `502` |
| `IOS_MIN_APP_VERSION` | the build's own constant | Overrides the oldest iOS app this gateway supports (`major.minor.patch`, A31). Normally left unset: the constant `IOS_MINIMUM_APP_VERSION` in `rc_gateway/compat.py` is raised in the release that breaks compatibility |
| `IOS_UPDATE_URL` | empty | Where the iPhone app's "Update required" screen sends people, such as the app's App Store page, `https://` only |
| `MACOS_MIN_APP_VERSION` | the build's own constant | Overrides the oldest Mac app this gateway supports (`major.minor.patch`, A45). Normally left unset: the constant `MACOS_MINIMUM_APP_VERSION` in `rc_gateway/compat.py` is raised in the release that breaks the Mac app's compatibility, apart from the iOS one |
| `MACOS_UPDATE_URL` | empty | Where the Mac app's "Update required" screen sends people, such as `https://sidecode.cc/download`, `https://` only |
| `ANDROID_MIN_APP_VERSION` | the build's own constant | Overrides the oldest Android app this gateway supports (`major.minor.patch`, A46). Normally left unset: the constant `ANDROID_MINIMUM_APP_VERSION` in `rc_gateway/compat.py` is raised in the release that breaks the Android app's compatibility |
| `ANDROID_UPDATE_URL` | empty | Where the Android app's "Update required" screen sends people, such as `https://sidecode.cc/download`, `https://` only |
| `WINDOWS_MIN_APP_VERSION` | the build's own constant | Overrides the oldest Windows app this gateway supports (`major.minor.patch`, A46). Normally left unset: the constant `WINDOWS_MINIMUM_APP_VERSION` in `rc_gateway/compat.py` is raised in the release that breaks the Windows app's compatibility |
| `WINDOWS_UPDATE_URL` | empty | Where the Windows app's "Update required" screen sends people, such as `https://sidecode.cc/download`, `https://` only |
| `APNS_TEAM_ID` | empty | Apple developer team id of the iPhone app's publisher; see [Push notifications](#push-notifications) |
| `APNS_KEY_ID` | empty | Key id of the APNs `.p8` signing key |
| `APNS_KEY_PATH` | empty | Path to that `.p8` **inside the container** |
| `APNS_TOPIC` | empty | The app's bundle id |
| `APNS_ENVIRONMENT` | `production` | `production` or `sandbox`. Debug builds register sandbox tokens |
| `WEB_PUSH_CONTACT` | `mailto:admin@example.com` | The VAPID `sub` claim. Set it to enable Web Push; the key pair is generated into `DATA_DIR` on first start. Empty disables Web Push |
| `TRUSTED_PROXIES` | `127.0.0.0/8,::1/128` | Comma-separated CIDR networks whose `X-Forwarded-For` the gateway believes. Must list your proxy's source address as the gateway sees it |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. Production runs `info`: at `debug` the WebSocket library logs a preview of every frame, which can carry fragments of messages or transcripts |

Five more variables exist for running outside compose and are set by the image: `RC_HOST`
(`0.0.0.0`), `RC_PORT` (`8787`), `WEB_DIST_DIR`, `CLIENT_DIST_DIR` and `CLIENT_INSTALL_SCRIPT`. From
a source checkout they default to the matching directories in the repository, so you rarely set
them; `RC_HOST` and `RC_PORT` are useful when another process already holds 8787.

All four APNs values must be present for APNs to be enabled. An unreadable key path is logged as
`APNs disabled: signing key not found` and the gateway starts anyway.

## Reverse proxy

The stack terminates nothing. It publishes the gateway on `GATEWAY_BIND:GATEWAY_PORT` and you put
your own proxy in front to own the hostname, the certificate and the HTTP to HTTPS redirect.

**Nginx Proxy Manager.** Create a Proxy Host:

| Field | Value |
| --- | --- |
| Domain Names | your public hostname, e.g. `rc.example.com` |
| Scheme | `http` |
| Forward Hostname / IP | the address the proxy can reach the VPS on: the host's IP, `host.docker.internal`, or the Docker bridge gateway (often `172.17.0.1`) when the proxy is itself a container |
| Forward Port | `GATEWAY_PORT`, `8787` by default |
| Websockets Support | **on** — without it `/ws/app`, `/ws/device` and `/ws/stt` never upgrade and nothing streams |
| Block Common Exploits | on, harmless here |
| SSL | request a certificate, then enable **Force SSL** and **HTTP/2 Support** |

In the host's **Advanced** tab add:

```nginx
client_max_body_size 80m;
```

nginx defaults to 1 MB, which is smaller than a single dictated audio upload: the gateway accepts
25 MiB on `/api/stt/transcribe`. 80 MB leaves headroom. Note that it does not bound `session.send`
attachments — the protocol's eight 6 MiB attachments travel as WebSocket frames after the upgrade,
which `client_max_body_size` does not apply to; the gateway caps those itself at 72 MiB.

The gateway pings every WebSocket every 25 seconds, so nginx's default 60 second
`proxy_read_timeout` never fires on an idle session.

**Connections.** Every online user holds two WebSockets, a device's `/ws/device` and an app's
`/ws/app`, and the proxy spends two connections on each, one to the client and one to the gateway.
A reverse proxy has its own connection limit per worker, 512 by default in nginx, and its own
open-file limit; raise both as the number of online users grows, then restart the proxy. In nginx:

```nginx
worker_rlimit_nofile 16384;
events {
    worker_connections 4096;
}
```

The gateway's own limit is in `docker-compose.yml`: `ulimits: nofile` 65536. Docker's default soft
limit of 1024 open files caps the connections the gateway can hold, because each online user takes
two sockets and every database call opens its SQLite files; past it, requests fail with
`unable to open database file`.

**Then, in `.env`:**

- Set `PUBLIC_ORIGIN` to the `https://` origin the browser will show, no path and no trailing slash.
  A mismatch down to the scheme or port makes the browser's login return 403.
- Put the proxy's source network in `TRUSTED_PROXIES`, as the gateway sees it. A proxy on the host
  network is loopback or the host address; a containerised proxy arrives from its Docker network
  subnet, which `docker network inspect <network>` prints. `172.16.0.0/12` covers the default bridge
  ranges. Make sure the proxy *replaces* `X-Forwarded-For` rather than appending: a proxy that
  appends lets a caller pick the first element and therefore its own rate-limit bucket. Nginx Proxy
  Manager replaces it.

**Security headers.** The gateway sets them itself on every HTTP response: a content security
policy, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
a permissions policy that keeps the microphone available for the composer, and HSTS whenever
`PUBLIC_ORIGIN` is `https://`. It sends no `Server` banner. Do not add a second, conflicting
content security policy in the proxy; the browser enforces the intersection of both and the app
will break in ways that are hard to read.

**Body limits and caching** are the gateway's job too. `/api/login` accepts 4 KB, the device and
push routes 16 KB, and an audio upload 25 MiB; the SPA shell, the service worker and the manifest
are served `no-store` so a deploy never leaves a stale `index.html` pointing at assets that no
longer exist.

**Any other proxy** needs the same five things: forward to the published port, forward WebSocket
upgrades, raise the body limit, replace `X-Forwarded-For`, and allow two connections per WebSocket
for every socket you expect.

**Plain HTTP** is only appropriate on a LAN, on loopback, or for a local test: the login password
and the bearer token cross the wire in the clear otherwise. Set `PUBLIC_ORIGIN` to the `http://`
address apps will actually use. The device installer refuses a plain-HTTP gateway that is not
loopback or an RFC 1918 address, so a publicly reachable host must be https.

## Terminals pass through (A38)

From 1.5.0 a device row in the apps opens a shell on that machine. The bytes travel inside the two
WebSocket connections the gateway already carries — the device's `/ws/device` and the app's
`/ws/app` — as `terminal.output` and `terminal.input` frames, so a reverse proxy that passes those
sockets passes terminals too: nothing new listens, no port is opened on the host or the VPS, and
the gateway relays the frames without reading or logging their content. Expect a busy shell to
push a few frames a second of up to 16 KiB each on each socket. A device can refuse the capability
with `[terminal] enabled = false` in its `~/.rc-client/config.toml`.

## Lifecycle

```sh
docker compose up -d                      # build if needed, then start
docker compose ps                         # gateway should be "healthy"
docker compose logs -f gateway            # structured JSON to stderr
docker compose restart gateway            # same container: a changed .env needs up -d
docker compose down                       # stop, keep the volumes
docker compose down -v                    # stop and DELETE the volumes
```

Compose rotates the gateway's container log with the `json-file` driver at 10 MB, keeping five
files, so older lines are gone once the files turn over.

**Upgrading.** Pull the new revision and rebuild:

```sh
git pull
docker compose build
docker compose up -d
```

Each of the six databases applies its own additive migrations when it is opened, so an existing
`DATA_DIR` is brought up to the new schema in place and an upgrade needs no manual step. A release
that adds a database — `preferences.sqlite3` in 1.4.0 — creates it on the first start and needs no
manual step either, and brings no new environment variable with it; a release that adds a column —
`devices.update_failed_build` in 1.4.7 (A36), `devices.terminal` in 1.5.0 (A38), the six Settings
columns of `preferences` in 1.7.0 (A41) — is applied the
same way. From 1.4.7 the gateway also
brings every connected device to the wheel it serves by itself (A36), so an image update is followed
by the devices updating themselves, one `Updating…` at a time on the device rows; a device whose
update fails stays on its old client and says so until someone retries. Signed-in apps stay signed in:
login sessions are recorded in `auth.sqlite3` rather than in memory, so a restart or an image update
no longer signs everyone out. Expired and revoked rows are pruned at startup, which is logged as
`pruned expired login sessions`.

**Updating the devices.** The gateway serves one client wheel at `/dist/rc_client-latest.whl` and
reports it to the apps as `client` in `GET /api/config`: its version, its SHA-256 and its URL. Every
device reports the build it was installed from, so a row in **Devices** whose build differs offers
**Update**, which asks that device to fetch exactly that build, verify its hash, install it and
restart its service. The row reads "Updating…" until the device reconnects on the new build, and
"Update failed · <reason>" if it reports a failure or does not come back within five minutes. The gateway needs nothing beyond the wheel it already serves:
rebuild after a `git pull` and every device can be brought forward from an app.

`GET /api/config` omits `client` in exactly one case, a gateway run from a source checkout where no
wheel has been built. The apps then offer no update, because there is no build to name. The image
`docker compose build` produces always carries one.

**Backing up.** Everything durable is in the `rc-data` volume: the accounts, login sessions, the
device registry, the session index, push subscriptions, the per-account preferences, the
token-signing secret and the VAPID private key.

```sh
docker run --rm -v sidecode_rc-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/rc-data-$(date +%F).tar.gz -C /data .
```

Check the volume's real name with `docker volume ls` first; Compose prefixes it with the project
directory name. Wiping the volume removes **every account except `admin`**, which is recreated from
`RC_PASSWORD` on the next start, and invalidates every login session **and every device token**:
each device has to be enrolled again with a fresh pairing code, and every Web Push subscription is
orphaned along with the VAPID key.

## Speech to text

Voice input is off until `STT_PROVIDER` names a backend. Four ways to provide one; only the last
streams words as they are said — the other three answer whole utterances, and the gateway fakes
partials by re-sending everything every two seconds. Whichever it is, the provider always detects
the language itself: there is no language setting, the apps offer no choice for dictation the
gateway transcribes, and a language an older app still sends is ignored (A44).

**A hosted OpenAI-compatible provider.** Any server implementing
`POST {STT_BASE_URL}/audio/transcriptions`:

```sh
STT_PROVIDER=openai
STT_BASE_URL=https://api.openai.com/v1
STT_API_KEY=sk-…
STT_MODEL=whisper-1
```

**MiMo.** Xiaomi MiMo has no transcription endpoint: the gateway sends the utterance as a base64
WAV data URL inside a chat completion and reads the transcript back out of the assistant's reply.

```sh
STT_PROVIDER=mimo
STT_BASE_URL=https://api.xiaomimimo.com/v1
STT_API_KEY=…
STT_MODEL=mimo-v2.5-asr
```

MiMo caps an utterance at 10 MB of base64; the gateway refuses anything larger before it sends the
request, which no recording under the protocol's 120 s limit reaches. The request shape has been
checked only against a fake local server, not against MiMo.

**Live, over the OpenAI Realtime protocol.** Alibaba Model Studio's
`qwen3-asr-flash-realtime` speaks it: the gateway holds one WebSocket to the vendor per utterance,
forwards each 100 ms frame as `input_audio_buffer.append`, turns every incremental
`conversation.item.input_audio_transcription.text` into `stt.partial` the moment it arrives, and on
`stt.stop` finishes the session, committing the last sentence only when one is still in progress —
server VAD (500 ms of silence ends a sentence) has usually committed it already, and Alibaba answers
a commit on an empty buffer with an error; anything the vendor says while the session is being
finished does not fail the utterance. Long dictations are segmented by that VAD; the sentences are
joined Chinese-style, without spaces between CJK characters. Chinese with English mixed in is what
the model is for.

```sh
STT_PROVIDER=realtime
STT_REALTIME_URL=wss://<workspace-id>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
STT_MODEL=qwen3-asr-flash-realtime
STT_API_KEY=sk-…          # the Model Studio API key
```

The Singapore region is `ap-southeast-1.maas.aliyuncs.com`; the workspace id and the key come from
the Model Studio console. `POST /api/stt/transcribe` works with this provider for WAV (16 kHz mono
PCM16) and raw PCM uploads only — the live protocol has no decoder behind it — and plays the file
into a session frame by frame. The wire shape follows the dialect Alibaba documents. OpenAI's own
Realtime transcription uses different `session.update` fields (24 kHz audio,
`gpt-live-transcribe`); this provider does not target it.

**On the VPS.** The compose file carries an optional `stt` service on the internal network, so audio
never leaves the host:

```sh
STT_PROVIDER=openai
STT_BASE_URL=http://stt:8000/v1
STT_MODEL=Systran/faster-distil-whisper-small.en
```

```sh
docker compose --profile local-stt up -d
```

The first request downloads the model into the `stt-models` volume, which takes a while and needs
disk. This profile has not been exercised end to end.

Transcription spends the operator's speech credit, so it is bounded the same way dictation polish
is: an address gets thirty speech requests a minute, counting each upload to `/api/stt/transcribe`
and each `/ws/stt` upgrade but never the partial transcripts inside a live stream, and one account
holds at most four live streams at once. Both are constants in `rc_gateway/app.py` and
`rc_gateway/ws/stt_ws.py` rather than settings, because a person dictating cannot reach either. A
stream that receives no audio for 90 s is closed, and signing out closes it at once rather than
letting it keep transcribing on a revoked session's behalf.

## Dictation polish

Off until both `POLISH_BASE_URL` and `POLISH_API_KEY` are set. The provider is anything
OpenAI-compatible — OpenAI itself, a proxy, or a local server on the compose network:

```
POLISH_BASE_URL=https://api.openai.com/v1
POLISH_API_KEY=sk-…
# POLISH_MODELS=gpt-4.1-mini,gpt-4.1     # optional: restrict the list the apps offer
```

With them set, `gateway ready` logs the polish provider, `GET /api/config` reports
`polish.enabled: true`, and each user turns the feature on in their own app's Settings — it is off by
default — choosing a model from the provider's list and a strength. What leaves the VPS when they do
is the dictated text plus up to twenty of that conversation's most recent messages as the app shows
them, sent to your provider with the operator's key; the gateway stores nothing from the call. A
provider that is down or slow answers the app with `502`, and the dictation stays as it was spoken;
more than thirty polish requests a minute from one address answer `429`.
Only the polish feature uses this key: the gateway still runs no agent and holds no agent credential.

## App compatibility

The web app is served by the gateway, so it can never be older than the gateway. The SideCode apps
are installed on their own, so the gateway says which is the oldest build of each it still works
with. For the iPhone app that is
(`apps.ios.minimum_version` in `GET /api/health`, `GET /api/config` and `hello`, A31), and an app
below that shows a full-screen "Update required" with its own version, the minimum, a button to
`IOS_UPDATE_URL` and Sign out — nothing else until it is updated. The minimum is a constant in the
gateway's source, `IOS_MINIMUM_APP_VERSION` in `rc_gateway/compat.py`, raised in the same release
that stops supporting older apps and left alone for additive changes; `IOS_MIN_APP_VERSION` overrides
it for one deployment when needed. A self-hosted gateway normally leaves all four minimums at the
release's constants, which follow the apps' public releases; set the `*_UPDATE_URL` variables only
if you want the "Update required" screen to offer a button. The Mac app is installed on its own too and reads its own entry, `apps.macos`
(A45), which works the same way with its own constant `MACOS_MINIMUM_APP_VERSION`, override
`MACOS_MIN_APP_VERSION` and button `MACOS_UPDATE_URL`, and so do the Android and Windows apps with
`apps.android` and `apps.windows` (A46): `ANDROID_MINIMUM_APP_VERSION` and
`WINDOWS_MINIMUM_APP_VERSION`, overrides `ANDROID_MIN_APP_VERSION` and `WINDOWS_MIN_APP_VERSION`,
buttons `ANDROID_UPDATE_URL` and `WINDOWS_UPDATE_URL`. The four minimums move separately, so a
release raises only the one whose older builds it stops supporting.

## Push notifications

**Web Push.** Set `WEB_PUSH_CONTACT` to a `mailto:` address. The VAPID key pair is generated into
`DATA_DIR` on first start, and `GET /api/push/web/vapid` serves the public key to the browser. With
the variable empty, that endpoint returns 503 and the web app hides the notification toggle. Keep
`vapid_private.pem` in your backups: replacing it invalidates every existing browser subscription.

**Which apps a self-hosted gateway can notify.** Web Push works for every browser that uses your
gateway, because the gateway signs it with its own VAPID key. The SideCode iPhone app is reached
only through APNs, which needs the app publisher's signing key for its bundle id; that key is not
distributed, so a self-hosted gateway leaves `APNS_*` empty, reports `apns_enabled: false`, and the
iPhone app alerts only while it is running and connected.
The Android, Mac and Windows apps have no push channel on any gateway: they post their own
notifications from their live connection. The APNs settings below are for an operator who holds
the key for the bundle id the app was built with.

**APNs.** Create an APNs auth key in the Apple developer portal, mount the `.p8` into the container
read-only, and set all four `APNS_*` values. The mount is commented out in `docker-compose.yml`:

```yaml
    volumes:
      - rc-data:/data
      - ./secrets/AuthKey.p8:/run/secrets/apns/AuthKey.p8:ro
```

with `APNS_KEY_PATH=/run/secrets/apns/AuthKey.p8`. The file must be readable by uid 10001, the
unprivileged user the image runs as. Use `APNS_ENVIRONMENT=sandbox` while testing a debug build.


## Health checks

`GET /api/health` is unauthenticated and returns the version, the protocol version, the auth mode,
whether registration is open and the number of online devices. The gateway container polls it every 30 seconds, so `docker
compose ps` reports `healthy` only when the service really answers. Note that every route is
declared `GET`-only: a `HEAD` request returns 405, which matters if your external monitor defaults
to `HEAD`.

## Troubleshooting

| Symptom | Log line or check | Cause and fix |
| --- | --- | --- |
| Container exits immediately | `rc-gateway: PUBLIC_ORIGIN is required…` | `.env` is missing, unreadable, or the variable is empty |
| Container exits immediately | `rc-gateway: DATA_DIR /data is not writable` | The volume is owned by the wrong uid; the image runs as 10001 |
| Login works in the app, fails in the browser | 403 on `/api/login` | `PUBLIC_ORIGIN` does not match the origin the browser actually used, down to scheme and port |
| Login returns 429 | `login rate limited` | Five attempts per minute per IP. Wait, and check `TRUSTED_PROXIES` if everyone shares one bucket |
| Login returns 400 with no message | none | The account is part of the credential: a body without a `username` is refused. Every app sends one |
| Login returns 403 for one person only | none | That account is disabled. Enable it from **Settings → Users** |
| Someone sees no "Create an account" link | `GET /api/health` shows `auth.registration_open: false` | Registration is closed. Open it from **Settings → Users**, or add the account yourself |
| A member's device keeps reconnecting and being closed | `device upgrade refused: the owning account is not active` | Its owner is disabled. The token is still valid; enabling the account restores the device |
| The hostname does not answer | `curl http://127.0.0.1:8787/api/health` on the host | If that works, the fault is in your reverse proxy: wrong forward host or port, or the container cannot reach `GATEWAY_BIND` |
| Devices or apps fail to connect once many users are online | `unable to open database file`, or nginx's `worker_connections are not enough` | The gateway reached its open-file limit or the proxy its connection limit; check `ulimits: nofile` in `docker-compose.yml` and `worker_connections` in [Reverse proxy](#reverse-proxy) |
| Everything loads but nothing streams | no `app connected` line | Websockets Support is off in the proxy host, so the `/ws/*` upgrade never reaches the gateway |
| Install one-liner 404s | `install script missing` | The image was built with `--target gateway` instead of `release`; rebuild with `docker compose build` |
| Device never appears | `device connected` absent | The daemon cannot reach the origin, or its token was revoked. Check `rc-client status` on the machine |
| A new device brings in old terminal sessions | none | Expected: a freshly enrolled machine mirrors its recent Claude and Codex transcripts, capped at 50 sessions from the last 14 days. Narrow it with `[mirror] max_sessions` and `max_age_days` in the device's `config.toml`, described in `docs/CLIENT.md` |
| Device flaps | `device disconnected` every few minutes | The proxy is timing out idle WebSockets; the gateway pings every 25 s, so allow at least 60 s. nginx's 60 s default is enough |
| Sessions listed but every action fails | `device_offline` replies | The daemon is not connected; the index still shows its last known summaries |
| Voice button missing | `GET /api/config` shows `stt.enabled: false` | `STT_PROVIDER` is `none` |
| Upload rejected with 413 | nginx returns it before the gateway logs anything | `client_max_body_size` is still at nginx's 1 MB default; set it to `80m` in the proxy host's Advanced tab |
| Dictation fails mid-utterance | `stt backend unreachable` or `stt backend rejected the request` | Wrong `STT_BASE_URL`, missing `STT_API_KEY`, or an unknown `STT_MODEL` |
| Dictation refused with `too_many_requests` | `stt upgrade rate limited` or `transcription rate limited` | Thirty speech requests a minute per address, or four live streams for one account. Check `TRUSTED_PROXIES` if everyone shares one bucket |
| A large attachment is refused with `too_large` | `large forward refused: no room in the in-flight budget` | Another maximal message is still being written to its device. One 64 MiB message at a time fits `mem_limit: 512m`; retry, or raise the limit and the budget together |
| An app socket closes with 4009 | `app socket replaced: account at the connection cap` | That account has more than eight `/ws/app` sockets open. Usually a tab left open on every device; the oldest is closed and the app reconnects |
| Subscribing to notifications returns 409 | `web push subscription refused: the endpoint belongs to another account` | The browser or phone was registered by a different account and never unsubscribed. Sign in as that account and unsubscribe, or wait for its login session to expire |
| Browser notifications never arrive | `web push delivery failed` | Subscriptions were created against a different VAPID key; unsubscribe and subscribe again |
| iPhone notifications never arrive | `GET /api/config` shows `push.apns_enabled: false` | Expected on a self-hosted gateway without the app publisher's APNs key (see [Push notifications](#push-notifications)). With a key, `apns delivery abandoned` means a wrong topic, wrong environment, or an expired key |
| Every app is signed out at once | check `docker volume ls` and `.env` | Sessions live in `auth.sqlite3` and survive a restart, so this means either `RC_SECRET` changed, which invalidates tokens but leaves devices enrolled, or the volume was replaced, which also drops every device token |

Gateway logs are structured JSON on stderr and never contain tokens, passwords, pairing codes,
prompt text or tool output at `info`. `LOG_LEVEL=debug` is for local diagnosis only: the
WebSocket library then logs a preview of every frame, which can include fragments of messages
or transcripts.
