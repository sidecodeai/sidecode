# Antigravity

Google's Antigravity CLI (`agy`) is SideCode's fifth coding agent, id `antigravity`, since protocol
amendment A64. The device drives it as an editor does, through `agy acp`, the Agent Client Protocol
server `agy` documents for editors, on the person's own sign-in and in the store `agy` itself keeps.
This document says what a device and the web app offer for it, what they do not, where the
integration lives and how to take it out again. The normative text is A64 in
[protocol/PROTOCOL.md](../protocol/PROTOCOL.md); where this document and PROTOCOL.md disagree,
PROTOCOL.md wins.

## How the device drives it

- **`agy acp` only.** The device runs one `agy acp` for each session it drives, started with no
  other argument in the session's directory, and one list process of its own that only calls
  `session/list`. That list is bounded by the `[mirror]` settings ([CLIENT.md](CLIENT.md)).
- **The person's own sign-in and store.** SideCode signs nobody in. A computer where `agy` is not
  signed in refuses a new session with "Antigravity is not signed in on this computer. Run agy in a
  terminal there and sign in, then try again." Conversations live in `agy`'s own store, so one
  started from an app goes on in a terminal, and an idle terminal conversation goes on from an app.
- **Quota on demand.** A `device.agents` reply runs `agy -p "/usage" --output-format json` and
  reports one `google` account with four windows, two for each of `agy`'s model groups. The result
  is held for a minute, the last good read stands in for up to 30 minutes, and the command never
  runs on a timer.
- **The usage-limit resume (A35).** Words that read like `agy`'s quota message make the device run
  the usage query afresh; only a window it reports used up ends the turn with `limit`, and the
  resume then goes as it does for Claude Code and Codex, off until the person turns the account's
  switch on.
- **No terminal takeover.** `agy` offers a client no way into a terminal and tells it nothing about
  another holder, so the device errs towards holding: a conversation that an `agy` it did not start
  names, or whose directory such an `agy` runs in, is read-only until that `agy` quits. One known
  only from `agy`'s list sits in the Archive and continues when a message is sent to it. Nothing
  watches a terminal conversation live.
- **Version.** `agy` 1.5.13 or later; an older one is reported and refused at session start.
  `RC_ANTIGRAVITY_BIN` points the device at an `agy` other than the one on `PATH` or in
  `~/.local/bin`.

The device relies only on what `agy` documents. It introduces itself as the client `sidecode` and
declares form questions and nothing else; it sends no `_meta` and no MCP server; it does not read
`agy`'s credentials or files, call Google's endpoints or drive `agy`'s sign-in (`authenticate` is
never called); and it adds no words of its own to what the person sends, apart from A35's fixed
resume sentence once the person has turned that switch on. Which conversations a terminal may have
open is judged from `agy`'s processes, never from its files. The gateway has no agent-specific
code, so A64 changes nothing in it.

## What is offered

Nothing on the wire is new beyond the id: a client draws Antigravity from its `capabilities` and
`control` as it draws any agent, and a client from before A64 shows the id `antigravity` with the
letter mark. The web app names it "Antigravity" in both languages and its account's vendor "Google",
and marks it with the letter A until Google's official vector and its licence are available.

| Offered | Not offered |
| --- | --- |
| New sessions in a chosen directory, with a worktree | Take over, and any live view of a terminal conversation |
| Send, Up next, editing a queued message (A43) | Rename: `/title` and `/rename` reach the model as text |
| Steering a running turn, except while one of its approvals or questions is open, when the message is queued | Close for an `agy` running in a terminal (`close_terminal`) |
| Attachments: images, text files and any other file | Plan cards: plan mode is a permission mode, and `agy` writes its plan as a file |
| Approvals with Allow and Deny only; Deny ends the turn | Todos: `agy` sends no plan updates |
| Questions: one card of up to four, each answered by its options or by words | Speed tiers |
| Stop; model, effort (set per model) and permission mode, as `agy` offered them to the newest session the device opened | |
| `agy`'s slash commands, with name and description | |
| Titles `agy` writes, history, tokens and context, quota meters on the device page, the A35 resume | |
| Close for a session the device drives, unread marks and pushes, the task board, dictation | |

## Testing against a real `agy`

The device's tests run a fake `agy` (`client/tests/fake_agy.py`), and the test guard refuses the
real one and any touch of `~/.gemini`. A run against a real, signed-in `agy` needs a computer, or
an operating-system user, with no other agents' conversations: `RC_CLIENT_HOME` does not isolate a
second client, which mirrors Claude Code's transcripts from the fixed `~/.claude` to whatever
gateway it enrols with. A test account on the gateway does not protect those conversations.

## Where it lives

- **Device.** `client/rc_client/agents/antigravity/`; `"antigravity"`, last in `AGENT_IDS` in
  `client/rc_client/agents/registry.py`; the client README and the package description; the tests
  `client/tests/fake_agy.py`, `client/tests/agy_helpers.py` and `client/tests/test_antigravity_*.py`;
  and in `client/tests/agent_guard.py` the `agy` entry and the `~/.gemini` audit.
- **Generic hooks only Antigravity uses today.** In `registry.py` the `AgentService` protocol with
  `service_for()` and `terminal_conflict()`; in `daemon.py` the start and stop of a plugin's service
  and `_refresh_agent`; in `sessions/hub.py` the `terminal_conflict()` lookup.
- **Generic and used by others**, so they stay on removal: `private_process`, which Claude Code's and
  pi's runners set and `own_process` in `sessions/hub.py` reads, and the hub's rule that keeps a
  session's queue while a terminal holds it, accepts edits to it and drains it once the device
  holds the session again (`sessions/hub.py`, `sessions/idle.py`,
  `client/tests/test_queue_while_held.py`).
- **Protocol.** A64 in `protocol/PROTOCOL.md` and the sections it names;
  `protocol/fixtures/objects/agent.antigravity.json` and
  `protocol/fixtures/app/reply.error.antigravity-signed-out.json`; the `antigravity` and `google`
  examples in `protocol/schema/objects.json`; the fixture's entry in
  `protocol/scripts/validate_fixtures.py`.
- **Web.** `agentLabels.antigravity`, `vendorLabels.google` and the A35 switch's sentence in
  `web/src/strings.ts` and `web/src/strings.zh-Hans.ts`; `antigravityAgent` and `antigravityHello`
  in `web/mock/fixtures.ts`; the tests `agents.test.tsx`, `DevicePage.test.tsx`,
  `accounts.test.ts`, `mock-close.test.ts` and `SettingsPage.test.tsx`.
- **Docs.** This file, the `[mirror]` rows in [CLIENT.md](CLIENT.md), and the Antigravity passages
  in [DESIGN.md](DESIGN.md) and [WEB.md](WEB.md).

## Removing it

1. **Device.** Delete the adapter package, its `AGENT_IDS` entry and its tests, take `agy` and the
   `~/.gemini` audit out of the test guard and the fifth agent out of the test lists. Then look for
   other users of the generic hooks
   (`rg -n 'service_for|AgentService|terminal_conflict|TERMINAL_CONFLICT' client/rc_client`) and
   delete each one nobody else uses, with the daemon's and the hub's calls into it. The release that
   does so withdraws, with `session.removed`, the Antigravity conversations a device still has, so
   no client keeps a row nothing can open.
2. **Protocol.** Withdraw the id in a new amendment, as A26 withdrew Cursor, and delete the two
   fixtures with their schema and validator entries.
3. **Web.** Delete the name, the vendor label, the demo entries and their tests.
4. **Docs.** The passages above, this file last. `rg -il 'antigravity|\bagy\b'` should then find
   only the amendment history in `protocol/PROTOCOL.md`.
5. **Release.** Devices update themselves with the next rc-client (A36), and a client that still
   meets the id renders it generically.
