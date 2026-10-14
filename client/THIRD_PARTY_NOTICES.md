# Third-party notices

## cc-remote

Parts of this device daemon were written with reference to
[cc-remote](https://github.com/muggle-stack/cc-remote), which is MIT licensed.
No file was copied verbatim; the ideas and hard-won details below were adapted:

| Idea taken | Where it lives here | Upstream reference |
| --- | --- | --- |
| Atomic 0600 credential write | `rc_client/config.py` (`write_atomic`) | `cc_remote/device.py` |
| Outbound-only WebSocket with an item- and byte-bounded send queue | `rc_client/gateway.py` (`ByteQueue`, `GatewayLink`) | `cc_remote/wrapper/transport.py`, `cc_remote/relay/forward.py` |
| Environment tombstones so secrets never reach tool subprocesses | `rc_client/child_env.py` | `cc_remote/wrapper/child_env.py` |
| Process scanning for the CLI that owns a session, and `lsof` on macOS | `rc_client/procscan.py`, `rc_client/agents/claude/holders.py` | `cc_remote/wrapper/process_scan.py`, `cc_remote/wrapper/claude_external.py` |
| Claude binary resolution order | `rc_client/agents/claude/runtime.py` | `cc_remote/wrapper/claude_runtime.py` |
| Single-consumer SDK message pump and the interrupt/drain contract | `rc_client/agents/claude/adapter.py` | `cc_remote/wrapper/sdk.py`, `cc_remote/wrapper/machine.py` |
| SDK message to timeline translation | `rc_client/agents/claude/translate.py` | `cc_remote/wrapper/stream.py` |
| Watching transcript growth by `st_size` rather than `st_mtime` | `rc_client/tailing.py` | `cc_remote/wrapper/stream.py` |
| Bounded one-shot JSON-RPC against a throwaway app-server | `rc_client/agents/codex/rpc.py` | `cc_remote/wrapper/codex_rpc.py` |
| Codex model catalogue with per-model effort clamping | `rc_client/agents/codex/models.py` | `cc_remote/wrapper/codex_models.py` |
| Codex notification to timeline mapping | `rc_client/agents/codex/translate.py` | `cc_remote/wrapper/codex_stream.py` |
| Rollout discovery from `session_meta` | `rc_client/agents/codex/rollouts.py` | `cc_remote/wrapper/codex_sessions.py` |
| launchd and systemd service templates | `rc_client/service/` | `deploy/com.muggle.cc-remote.wrapper.plist.in`, `deploy/cc-remote-wrapper.service` |
| Installer bootstrap shape | `install.sh` | `deploy/install-wrapper.sh` |

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

`claude-agent-sdk` (MIT), `websockets` (BSD-3-Clause), `httpx` (BSD-3-Clause)
and `tomli-w` (MIT) are installed from PyPI and keep their own licences.
