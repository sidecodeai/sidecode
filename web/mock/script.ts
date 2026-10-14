/**
 * The scripted live turn the mock gateway plays back: thinking, streamed text,
 * tool calls with live output, an approval and a question.
 */
import type { SessionEvent } from '../src/protocol/types';

export interface Step {
  after: number;
  event: (seq: number, ts: number) => SessionEvent;
}

const THINKING = [
  'The failure only shows up on CI, so the shared state is probably ',
  'a module-level clock. Let me reproduce with a repeat count first ',
  'and then look at how the session lock is acquired.',
];

const ANSWER = [
  'Reproducing first — the refresh test shares a module-level clock, ',
  'so a scheduled expiry can leak between tests.',
];

const CLOSING = [
  'Each test now gets its own frozen clock and refresh is guarded by ',
  'the session lock. Re-running the suite 100× to confirm.',
];

const LIVE_OUTPUT = [
  '.....................',
  '............... 42%\n',
  '............................. 72%\n',
  '72 passed in 38.02s\n',
];

/**
 * Amendment A12: the device echoes the `session.send` request id as the
 * `user_message` block id. `blockId` is that id; a turn the device started
 * itself, such as a `first_message`, still mints one and exercises the app's
 * fallback. The echo lags by ECHO_DELAY_MS, the way a real round trip does.
 */
export const ECHO_DELAY_MS = 400;

export function turnScript(prompt: string, blockId?: string): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  push(0, (seq, ts) => ({ seq, ts, kind: 'turn_started', turn_id: 'turn-live', trigger: 'remote' }));
  push(ECHO_DELAY_MS, (seq, ts) => ({
    seq,
    ts,
    kind: 'user_message',
    block_id: blockId ?? `u-${ts}`,
    source: 'remote',
    text: prompt,
  }));
  push(120, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));

  THINKING.forEach((delta, index) => {
    push(240, (seq, ts) => ({ seq, ts, kind: 'thinking', block_id: 'blk-think', delta, done: false }));
    void index;
  });
  push(200, (seq, ts) => ({
    seq,
    ts,
    kind: 'thinking',
    block_id: 'blk-think',
    text: THINKING.join(''),
    done: true,
    duration_ms: 12_000,
  }));

  ANSWER.forEach((delta) => {
    push(180, (seq, ts) => ({ seq, ts, kind: 'assistant_text', block_id: 'blk-text', delta, done: false }));
  });
  push(160, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: 'blk-text',
    text: ANSWER.join(''),
    done: true,
  }));

  push(200, (seq, ts) => ({
    seq,
    ts,
    kind: 'todos',
    items: [
      { id: 'td1', text: 'Reproduce the flake locally', status: 'completed' },
      { id: 'td2', text: 'Isolate the shared clock', status: 'in_progress' },
      { id: 'td3', text: 'Guard the refresh path with the session lock', status: 'pending' },
      { id: 'td4', text: 'Re-run the suite 100 times', status: 'pending' },
    ],
  }));

  push(300, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: 'blk-read',
    tool: 'Read',
    tool_kind: 'read',
    title: '4 files in tests/ and auth/',
    status: 'succeeded',
    started_at: ts - 1_200,
    ended_at: ts,
    duration_ms: 1_200,
    input: { paths: ['tests/test_auth.py', 'tests/conftest.py', 'auth/session.py', 'auth/clock.py'] },
    output: 'tests/test_auth.py (218 lines)\ntests/conftest.py (44 lines)\nauth/session.py (312 lines)\nauth/clock.py (27 lines)',
  }));

  push(320, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: 'blk-bash-1',
    tool: 'Bash',
    tool_kind: 'shell',
    title: 'pytest -k refresh --count 20',
    status: 'failed',
    started_at: ts - 6_400,
    ended_at: ts,
    duration_ms: 6_400,
    summary: '2 failed',
    input: { command: 'pytest -k refresh --count 20' },
    output:
      'FAILED tests/test_auth.py::test_refresh_flow - AssertionError: token expired\nFAILED tests/test_auth.py::test_refresh_flow - AssertionError: token expired\n2 failed, 18 passed in 6.40s',
  }));

  push(280, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: 'blk-edit-1',
    tool: 'Edit',
    tool_kind: 'edit',
    title: 'auth/session.py',
    status: 'succeeded',
    started_at: ts - 400,
    ended_at: ts,
    duration_ms: 400,
    diff: {
      path: 'auth/session.py',
      additions: 12,
      deletions: 4,
      patch: `--- a/auth/session.py
+++ b/auth/session.py
@@ -41,10 +41,18 @@ class SessionStore:
-    _clock = time.monotonic
-
-    def refresh(self, token: str) -> Token:
-        if self._expired(token):
-            raise TokenExpired(token)
+    def __init__(self, clock: Clock | None = None) -> None:
+        self._clock = clock or SystemClock()
+        self._lock = threading.RLock()
+
+    def refresh(self, token: str) -> Token:
+        with self._lock:
+            if self._expired(token):
+                raise TokenExpired(token)
+            return self._rotate(token)`,
    },
  }));

  push(240, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: 'blk-edit-2',
    tool: 'Edit',
    tool_kind: 'edit',
    title: 'tests/conftest.py',
    status: 'succeeded',
    started_at: ts - 300,
    ended_at: ts,
    duration_ms: 300,
    diff: {
      path: 'tests/conftest.py',
      additions: 8,
      deletions: 1,
      patch: `--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -1,5 +1,12 @@
-import pytest
+import pytest
+
+from auth.clock import FrozenClock
+
+
+@pytest.fixture
+def frozen_clock():
+    return FrozenClock(start=0.0)`,
    },
  }));

  CLOSING.forEach((delta) => {
    push(200, (seq, ts) => ({ seq, ts, kind: 'assistant_text', block_id: 'blk-text-2', delta, done: false }));
  });
  push(160, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: 'blk-text-2',
    text: CLOSING.join(''),
    done: true,
  }));

  push(220, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: 'blk-bash-live',
    tool: 'Bash',
    tool_kind: 'shell',
    title: 'pytest tests/test_auth.py --count 100 -q',
    status: 'running',
    started_at: ts,
    input: { command: 'pytest tests/test_auth.py --count 100 -q' },
    output: '',
  }));

  let live = '';
  LIVE_OUTPUT.forEach((chunk) => {
    push(700, (seq, ts) => {
      live += chunk;
      return {
        seq,
        ts,
        kind: 'tool_call',
        block_id: 'blk-bash-live',
        tool: 'Bash',
        tool_kind: 'shell',
        title: 'pytest tests/test_auth.py --count 100 -q',
        status: 'running',
        started_at: ts - 2_000,
        input: { command: 'pytest tests/test_auth.py --count 100 -q' },
        output: live,
      };
    });
  });

  push(600, (seq, ts) => ({
    seq,
    ts,
    kind: 'approval',
    block_id: 'blk-approval',
    request_id: 'req-live-1',
    tool: 'Bash',
    tool_kind: 'shell',
    title: 'git commit -am "fix: isolate the auth clock"',
    status: 'pending',
    input: { command: 'git commit -am "fix: isolate the auth clock"' },
    options: [
      { id: 'allow', label: 'Allow once', style: 'primary' },
      { id: 'allow_session', label: 'Allow for session', style: 'secondary' },
      { id: 'deny', label: 'Deny', style: 'danger' },
    ],
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'needs_approval' }));

  return steps;
}

/** Emitted once the approval is answered. */
export function afterApproval(): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  push(100, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
  push(400, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: 'blk-bash-live',
    tool: 'Bash',
    tool_kind: 'shell',
    title: 'pytest tests/test_auth.py --count 100 -q',
    status: 'succeeded',
    started_at: ts - 40_000,
    ended_at: ts,
    duration_ms: 40_000,
    summary: '100 passed',
    output: '100 passed in 41.10s\n',
  }));
  push(400, (seq, ts) => ({
    seq,
    ts,
    kind: 'question',
    block_id: 'blk-question',
    request_id: 'req-live-2',
    status: 'pending',
    questions: [
      {
        id: 'q1',
        prompt: 'Should I also backport the fix to the release branch?',
        options: [
          { id: 'yes', label: 'Yes, open a cherry-pick', description: 'Creates a branch from release/1.4' },
          { id: 'no', label: 'No, main only' },
        ],
        multi: false,
        allow_text: true,
      },
    ],
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'needs_input' }));

  return steps;
}

/** Emitted once the question is answered: the turn finishes. */
export function afterAnswer(): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  push(100, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
  push(300, (seq, ts) => ({
    seq,
    ts,
    kind: 'todos',
    items: [
      { id: 'td1', text: 'Reproduce the flake locally', status: 'completed' },
      { id: 'td2', text: 'Isolate the shared clock', status: 'completed' },
      { id: 'td3', text: 'Guard the refresh path with the session lock', status: 'completed' },
      { id: 'td4', text: 'Re-run the suite 100 times', status: 'completed' },
    ],
  }));
  push(300, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: 'blk-text-3',
    done: true,
    text: 'Done. `auth/session.py` now takes an injectable clock and the refresh path holds the session lock:\n\n```python\nwith self._lock:\n    if self._expired(token):\n        raise TokenExpired(token)\n    return self._rotate(token)\n```\n\n100 consecutive runs are green.',
  }));
  push(200, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_completed',
    turn_id: 'turn-live',
    stop_reason: 'completed',
    duration_ms: 68_000,
    usage: {
      input_tokens: 44_100,
      output_tokens: 11_300,
      total_tokens: 55_400,
      context_used: 66_000,
      context_window: 200_000,
      cost_usd: 0.51,
    },
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'idle' }));

  return steps;
}

/**
 * Amendment A10: a short turn inside a session shared with a live terminal.
 * The user message is emitted by the server, because only it knows whether the
 * device could inject the text or had to hold it.
 */
/** The first few words of a prompt, quoted, for the mock's own narration. */
function words(text: string, count: number): string {
  const parts = text.split(/\s+/).filter(Boolean);
  const head = parts.slice(0, count).join(' ');
  return `"${head}${parts.length > count ? '…' : ''}"`;
}

export function sharedTurn(prompt: string, nonce: string, withApproval: boolean): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  push(0, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
  push(60, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_started',
    turn_id: `shared-${nonce}`,
    trigger: 'remote',
  }));
  push(700, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: `sh-a-${nonce}`,
    done: false,
    delta: 'Reading the channel registration on the running CLI',
  }));
  push(600, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: `sh-a-${nonce}`,
    done: !withApproval,
    delta: ` — the session id matches, so ${words(prompt, 6)} arrived as a prompt.`,
  }));

  if (withApproval) {
    push(700, (seq, ts) => ({
      seq,
      ts,
      kind: 'approval',
      block_id: `sh-ap-${nonce}`,
      request_id: `req-shared-${nonce}`,
      tool: 'Bash',
      tool_kind: 'shell',
      title: 'rc-client channel --status',
      // A10 §5.7: the relay hands over exactly these three fields, no diff.
      input: {
        tool_name: 'Bash',
        description: 'Print the channel attachment status',
        input_preview: 'rc-client channel --status',
      },
      status: 'pending',
      options: [
        { id: 'allow', label: 'Allow', style: 'primary' },
        { id: 'deny', label: 'Deny', style: 'danger' },
      ],
    }));
    push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'needs_approval' }));
    return steps;
  }

  push(400, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_completed',
    turn_id: `shared-${nonce}`,
    stop_reason: 'completed',
    duration_ms: 1_800,
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'idle' }));
  return steps;
}

/** Emitted once a relayed permission request is answered from an app. */
export function sharedAfterApproval(nonce: string, allowed: boolean): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  push(120, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
  push(500, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: `sh-a2-${nonce}`,
    done: true,
    text: allowed
      ? 'The channel reports one attached session and no pending injections.'
      : 'Skipped the status command. Run `rc-client channel --status` in the terminal instead.',
  }));
  push(400, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_completed',
    turn_id: `shared-${nonce}`,
    stop_reason: 'completed',
    duration_ms: 3_400,
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'idle' }));
  return steps;
}

/* ------------------------------------------------------ A11 shared Codex */

/** The four decisions the Codex daemon offers for a command (A11 §5.7). */
const CODEX_DECISIONS = [
  { id: 'allow', label: 'Allow', style: 'primary' as const },
  { id: 'allow_session', label: 'Allow for this session', style: 'secondary' as const },
  { id: 'allow_always', label: 'Always allow commands like this', style: 'secondary' as const },
  { id: 'deny', label: 'Deny', style: 'danger' as const },
];

export interface CodexTurnOptions {
  /** The turn the events belong to: the live one when steering, a new one otherwise. */
  turnId: string;
  /** True when the prompt reached a turn that was already running. */
  steered: boolean;
  /** Whether this turn ends on a permission request rather than completing. */
  withApproval: boolean;
}

/**
 * Amendment A11: a turn on a Codex thread the device shares with a live TUI
 * through the app-server daemon. Steering joins the running turn instead of
 * starting one, and a command approval offers all four daemon decisions.
 */
export function codexSharedTurn(prompt: string, nonce: string, options: CodexTurnOptions): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  if (!options.steered) {
    push(0, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
    push(60, (seq, ts) => ({
      seq,
      ts,
      kind: 'turn_started',
      turn_id: options.turnId,
      trigger: 'remote',
    }));
  }

  const block = `cx-a-${nonce}`;
  push(600, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: block,
    done: false,
    delta: options.steered
      ? 'Folding that into the running turn'
      : 'Resuming the thread on the daemon',
  }));
  push(500, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: block,
    done: !options.withApproval,
    delta: ` — the TUI sees the same stream, so ${words(prompt, 6)} is on the transcript there too.`,
  }));

  if (options.withApproval) {
    push(700, (seq, ts) => ({
      seq,
      ts,
      kind: 'approval',
      block_id: `cx-ap-${nonce}`,
      request_id: `req-codex-${nonce}`,
      tool: 'shell',
      tool_kind: 'shell',
      title: 'npm run typecheck',
      // A11 §5.7: commands carry the daemon's own command payload.
      input: {
        command: "/bin/zsh -lc 'npm run typecheck'",
        cwd: '/Users/me/dev/remote-control/web',
        command_actions: [{ type: 'unknown', command: 'npm run typecheck' }],
      },
      status: 'pending',
      options: CODEX_DECISIONS,
    }));
    push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'needs_approval' }));
    return steps;
  }

  push(400, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_completed',
    turn_id: options.turnId,
    stop_reason: 'completed',
    duration_ms: 2_100,
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'idle' }));
  return steps;
}

/** Emitted once a Codex permission request is answered from an app. */
export function codexSharedAfterApproval(
  nonce: string,
  turnId: string,
  allowed: boolean,
): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };

  push(120, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
  push(500, (seq, ts) => ({
    seq,
    ts,
    kind: 'tool_call',
    block_id: `cx-t-${nonce}`,
    tool: 'shell',
    tool_kind: 'shell',
    title: 'npm run typecheck',
    status: allowed ? 'succeeded' : 'cancelled',
    started_at: ts - 4_200,
    ended_at: ts,
    duration_ms: 4_200,
    input: { command: 'npm run typecheck' },
    output: allowed ? '> tsc --noEmit\n\nFound 0 errors.\n' : '',
  }));
  push(500, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: `cx-a2-${nonce}`,
    done: true,
    text: allowed
      ? '`tsc --noEmit` is clean. The same output scrolled past in the terminal.'
      : 'Skipped the typecheck. Run `npm run typecheck` in the terminal when you are ready.',
  }));
  push(400, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_completed',
    turn_id: turnId,
    stop_reason: 'completed',
    duration_ms: 7_200,
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'idle' }));
  return steps;
}

/* ------------------------------------------------------- slash commands (A27) */

/**
 * What a terminal would have printed for the read-only commands. The device
 * turns each into a `tool_call` block — `tool_kind: "other"`, `tool` and
 * `title` the command itself — because that is where long output belongs
 * (PROTOCOL.md §6.3, `fixtures/events/tool_call.command.json`).
 */
const COMMAND_OUTPUT: Record<string, { output: string; summary?: string }> = {
  status: {
    output: [
      'Model: gpt-5.4-codex · reasoning high · speed Fast',
      'Approval: on-request · sandbox workspace-write',
      'Directory: /Users/me/dev/remote-control/web',
      'Tokens: 48,200 used of 200,000 (24%)',
    ].join('\n'),
    summary: '24% of the context used',
  },
  usage: {
    output: [
      'Plan: Pro',
      '5-hour window: 38% used, resets 14:20',
      'Weekly window: 12% used, resets Thu 09:00',
    ].join('\n'),
    summary: '38% of the 5-hour window used',
  },
  skills: {
    output: ['pdf-tables — extract tables from a PDF', 'sql-review — review a migration'].join('\n'),
    summary: '2 skills',
  },
  hooks: {
    output: ['SessionStart — scripts/announce.sh', 'PreToolUse — scripts/guard.py'].join('\n'),
    summary: '2 hooks',
  },
  mcp: {
    output: ['playwright — 14 tools, connected', 'sqlite — 6 tools, connected'].join('\n'),
    summary: '2 servers',
  },
};

/** What the agents answer in prose, by command. */
const COMMAND_ANSWER: Record<string, (argument?: string) => string> = {
  review: (argument) =>
    argument
      ? `Reviewed the working tree with your instructions in mind (${argument}). One finding: the retry loop in \`gateway/link.py\` re-enters before the backoff timer is cleared.`
      : 'Reviewed the working tree. One finding: `gateway/link.py` re-enters the retry loop before the backoff timer is cleared.',
  init: () =>
    'Wrote `AGENTS.md`: the layout, how to run the tests, and the two rules this repository enforces on every change.',
};

/**
 * A27 — running one slash command. The echo comes first, under the request's
 * own id, and then the outcome the command calls for: a `notice` for a state
 * change, a `tool_call` block for what a terminal would have printed, and an
 * ordinary turn for a command the agent answers in prose. A Grok command may
 * legitimately answer nothing at all — `/context` renders in its own pager —
 * so the echo is the whole of it.
 */
export function commandScript(
  agent: string,
  name: string,
  argument: string | undefined,
  blockId: string,
): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const push = (delay: number, event: Step['event']) => {
    at += delay;
    steps.push({ after: at, event });
  };
  const nonce = String(Date.now()).slice(-6);

  push(0, (seq, ts) => ({
    seq,
    ts,
    kind: 'user_message',
    block_id: blockId,
    source: 'remote',
    text: argument ? `/${name} ${argument}` : `/${name}`,
  }));

  if (name === 'compact') {
    push(900, (seq, ts) => ({
      seq,
      ts,
      kind: 'notice',
      level: 'info',
      text: 'Context was compacted; earlier turns are summarised.',
    }));
    return steps;
  }

  const printed = agent === 'codex' ? COMMAND_OUTPUT[name] : undefined;
  if (printed) {
    push(500, (seq, ts) => ({
      seq,
      ts,
      kind: 'tool_call',
      block_id: `cmd-${nonce}`,
      tool: `/${name}`,
      tool_kind: 'other',
      title: `/${name}`,
      status: 'succeeded',
      started_at: ts - 200,
      ended_at: ts,
      duration_ms: 200,
      output: printed.output,
      ...(printed.summary ? { summary: printed.summary } : {}),
    }));
    return steps;
  }

  // Grok renders `/context` in its own pager, so nothing reaches the transport.
  if (agent === 'grok' && name === 'context') return steps;

  const answer =
    COMMAND_ANSWER[name]?.(argument) ??
    (argument
      ? `Ran \`/${name}\` with "${argument}".`
      : `Ran \`/${name}\`. Nothing needed changing.`);
  const turnId = `cmd-turn-${nonce}`;
  push(200, (seq, ts) => ({ seq, ts, kind: 'turn_started', turn_id: turnId, trigger: 'remote' }));
  push(60, (seq, ts) => ({ seq, ts, kind: 'status', state: 'running' }));
  push(500, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: `cmd-a-${nonce}`,
    delta: answer,
    done: false,
  }));
  push(300, (seq, ts) => ({
    seq,
    ts,
    kind: 'assistant_text',
    block_id: `cmd-a-${nonce}`,
    text: answer,
    done: true,
  }));
  push(200, (seq, ts) => ({
    seq,
    ts,
    kind: 'turn_completed',
    turn_id: turnId,
    stop_reason: 'completed',
    duration_ms: 1_060,
  }));
  push(20, (seq, ts) => ({ seq, ts, kind: 'status', state: 'idle' }));
  return steps;
}
