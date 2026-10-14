# Third-party notices

## cc-remote

Parts of this gateway are derived from **cc-remote** (<https://github.com/muggle-stack/cc-remote>),
which is distributed under the MIT License. The derived work has been renamed to this project's
vocabulary (device, app, session, gateway) and reduced to what this protocol needs, but the design
and, in places, the code are the upstream project's.

Files in `rc_gateway/` that contain adapted upstream code, with their origin:

| This file | Upstream file | What was taken |
| --- | --- | --- |
| `connections.py` | `cc_remote/relay/forward.py` | Bounded per-connection send queue with both an item and a byte limit, and the rule that exceeding either drops the whole connection rather than shedding frames |
| `hub.py` | `cc_remote/relay/pairing.py` | One connection slot per device with replacement, the send lock that linearises forwarding against slot changes, and the broadcast / drop-slow-peer structure |
| `auth.py` | `cc_remote/relay/auth.py` | HMAC-signed session token format and constant-time login |
| `session_registry.py` | `cc_remote/relay/server.py` (`SessionRegistry`) | Process-local revocation registry for signed tokens |
| `ratelimit.py` | `cc_remote/relay/server.py` (`LoginRateLimiter`) | Per-IP attempt limiter with global stale-entry cleanup |
| `origins.py` | `cc_remote/relay/origins.py` | Origin canonicalisation used for the CSRF check |
| `devices.py` | `cc_remote/relay/devices.py` | Pairing and enrollment store: hashed codes, hashed tokens, `BEGIN IMMEDIATE`, single use, device cap |
| `push_store.py` | `cc_remote/relay/push.py`, `cc_remote/relay/native_push.py` | Subscription and delivery-journal schemas, session-scoped rows |
| `push.py` | `cc_remote/relay/push.py`, `cc_remote/relay/native_push_router.py` | Web Push dispatch through pywebpush and the observed-event notification trigger |
| `apns.py` | `cc_remote/relay/native_push.py` | ES256 provider-token cache, the documented APNs failure reasons, and the logging filters that keep device tokens and HPACK header blocks out of the log |
| `logging.py` | `cc_remote/relay/log_safety.py` | Fail-closed credential redaction applied before any handler formats a record |

`gateway/Dockerfile` and the root `docker-compose.yml` follow the structure of upstream's
`deploy/Dockerfile` and `deploy/docker-compose.yml`.

### MIT License

```
MIT License

Copyright (c) 2026 muggle

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Runtime dependencies

The gateway depends on FastAPI, Starlette, uvicorn, websockets, httpx, pywebpush, py-vapid,
cryptography, python-dotenv and python-multipart. Their licences are recorded in the resolved
dependency set (`gateway/uv.lock`) and in each distribution's own metadata.
