/**
 * Fixtures for the mock gateway and the unit tests. Every object follows
 * PROTOCOL-FROZEN.md §3 / §4 exactly.
 */
import type {
  AgentAccount,
  AgentId,
  AgentInfo,
  Command,
  Device,
  QueuedMessage,
  Session,
  SessionEvent,
} from '../src/protocol/types';

const now = Date.now();
const minutes = (n: number) => now - n * 60_000;
const inHours = (n: number) => now + n * 3_600_000;

export const claudeAgent: AgentInfo = {
  agent: 'claude',
  available: true,
  version: '2.1.266',
  path: '/Users/me/.local/bin/claude',
  models: [
    { id: 'claude-sonnet-4-5', label: 'Sonnet 4.5' },
    { id: 'claude-opus-4-1', label: 'Opus 4.1' },
    { id: 'claude-haiku-4-5', label: 'Haiku 4.5' },
  ],
  default_model: 'claude-sonnet-4-5',
  permission_modes: [
    { id: 'default', label: 'Ask before edits' },
    { id: 'acceptEdits', label: 'Auto-accept edits' },
    { id: 'plan', label: 'Plan mode' },
    { id: 'bypassPermissions', label: 'Bypass permissions' },
  ],
  default_permission_mode: 'acceptEdits',
  efforts: [
    { id: 'low', label: 'Low' },
    { id: 'medium', label: 'Medium' },
    { id: 'high', label: 'High' },
  ],
  default_effort: 'high',
  capabilities: [
    'worktree',
    'takeover',
    'interrupt',
    'queue',
    'attachments',
    'effort',
    'history',
    // A40: the shim's pseudo-terminal takes a typed `/compact`.
    'commands',
  ],
  // Amendment A10: this device installs the `claude` shim, so a terminal
  // session started through it can be attached instead of taken over.
  attach: 'channel',
  attach_ready: true,
  // A42: Stop is one Escape typed into the same pseudo-terminal, so the shim
  // carries the interrupt as well as the two settings below.
  shared_interrupt: true,
  // A40: the shim runs the CLI inside a pseudo-terminal the device owns, so
  // the device types `/model` and `/effort` into it as the person would. There
  // is no command to type for the permission mode, so it stays the terminal's.
  shared_settings: true,
  shared_settings_keys: ['model', 'effort'],
  // A49: the channel carries text alone, so the device writes each attachment
  // to disk and the words it injects name it.
  shared_attachments: true,
  // A33: the Max account this machine's Claude Code runs on, with the three
  // windows the OAuth usage endpoint reports — a session window, the week, and
  // the week confined to one model.
  accounts: [
    {
      provider: 'anthropic',
      method: 'account',
      plan: 'max',
      tier: 'Max 5x',
      email: 'me@example.com',
      limits: [
        { window_minutes: 300, used_percent: 16, resets_at: inHours(2.5) },
        { window_minutes: 10080, used_percent: 54, resets_at: inHours(72) },
        { window_minutes: 10080, scope: 'Fable', used_percent: 64, resets_at: inHours(72) },
      ],
      limits_checked_at: now,
    },
  ],
};

/** The same agent on a device where the shim is not installed yet (A10). */
export const claudeNoShim: AgentInfo = {
  ...claudeAgent,
  attach_ready: false,
  // A40: no shim means no pseudo-terminal to type into, so there is nothing
  // the device could change or run on a session this machine's terminal holds.
  capabilities: claudeAgent.capabilities.filter((capability) => capability !== 'commands'),
  // A42: and nothing to type an Escape into either.
  shared_interrupt: false,
  shared_settings: false,
  shared_settings_keys: undefined,
  // A49: and no channel to name an attachment's file in.
  shared_attachments: false,
  // A33: installed on this machine and signed in nowhere.
  accounts: [],
};

export const codexAgent: AgentInfo = {
  agent: 'codex',
  available: true,
  version: '0.154.0',
  path: '/opt/homebrew/bin/codex',
  models: [
    { id: 'gpt-5.4-codex', label: 'GPT-5.4 Codex' },
    { id: 'gpt-5.4', label: 'GPT-5.4' },
  ],
  default_model: 'gpt-5.4-codex',
  permission_modes: [
    { id: 'untrusted', label: 'Ask for everything' },
    { id: 'on-request', label: 'Ask when needed' },
    { id: 'never', label: 'Never ask' },
  ],
  default_permission_mode: 'on-request',
  efforts: [
    { id: 'low', label: 'Low' },
    { id: 'medium', label: 'Medium' },
    { id: 'high', label: 'High' },
  ],
  default_effort: 'medium',
  capabilities: [
    'worktree',
    'interrupt',
    'queue',
    'steer',
    'attachments',
    'effort',
    'history',
    // A27: the daemon has a method behind each command in `commandsFor`.
    'commands',
  ],
  // A21: Codex's own service tier. The apps draw the lightning only because
  // this list is non-empty; Claude lists none and draws nothing.
  speeds: [{ id: 'priority', label: 'Fast' }],
  // Amendment A11: every bare `codex` TUI on this device runs inside the shared
  // app-server daemon, so an attached session carries the interrupt, the
  // settings and the image inputs as well as prompts and approvals.
  attach: 'daemon',
  attach_ready: true,
  shared_interrupt: true,
  shared_settings: true,
  shared_attachments: true,
  // A33: the ChatGPT account, with the two windows the shared daemon answers
  // `account/rateLimits/read` with. The five-hour one is nearly spent, so the
  // meter is drawn in the warning colour.
  accounts: [
    {
      provider: 'openai',
      method: 'account',
      plan: 'pro',
      email: 'me@example.com',
      limits: [
        { window_minutes: 300, used_percent: 84, resets_at: inHours(1.2) },
        { window_minutes: 10080, used_percent: 12, resets_at: inHours(120) },
      ],
      limits_checked_at: now,
    },
  ],
};

/** A11: the same agent on a device where the daemon is not running yet. */
export const codexNoDaemon: AgentInfo = {
  ...codexAgent,
  attach_ready: false,
  shared_interrupt: false,
  shared_settings: false,
  shared_attachments: false,
  // A33: the account is read from a file, so it is known; the windows are read
  // through the daemon, which is not running here.
  accounts: [
    {
      provider: 'openai',
      method: 'account',
      plan: 'pro',
      email: 'me@example.com',
      limits_error: 'Codex shared daemon is not running',
    },
  ],
};

/**
 * A25/A26/A28 — the two agents the apps met last, copied from
 * `protocol/fixtures/objects/agent.grok.json` and `agent.pi.json`. Grok Build's
 * terminal sessions are attached through its leader, the one backend process a
 * machine runs when `[cli] use_leader` is on; pi's through the extension the
 * device installs into it.
 */
export const grokAgent: AgentInfo = {
  agent: 'grok',
  available: true,
  version: '1.0.30',
  path: '/Users/me/.grok/bin/agent',
  models: [
    { id: 'grok-4.6', label: 'Grok 4.6' },
    { id: 'grok-4.5', label: 'Grok 4.5' },
  ],
  default_model: 'grok-4.6',
  permission_modes: [
    { id: 'default', label: 'Ask when needed' },
    { id: 'acceptEdits', label: 'Auto-accept edits' },
    { id: 'auto', label: 'Auto mode' },
    { id: 'dontAsk', label: 'Deny unless allowed' },
    { id: 'plan', label: 'Plan mode' },
    { id: 'bypassPermissions', label: 'Bypass permissions' },
  ],
  default_permission_mode: 'default',
  efforts: [
    { id: 'low', label: 'Low' },
    { id: 'medium', label: 'Medium' },
    { id: 'high', label: 'High' },
    { id: 'xhigh', label: 'Extra high' },
  ],
  default_effort: 'high',
  // A27: Grok Build advertises its whole list over ACP when a session opens.
  // A49: its prompts take no images, so the device writes each attachment to
  // disk and the prompt names it.
  capabilities: ['worktree', 'interrupt', 'queue', 'attachments', 'effort', 'history', 'commands'],
  // A28: `[cli] use_leader` is on here, so every `grok` this machine starts
  // joins the leader the device is a client of. `session/cancel` and
  // `session/set_config_option` act for everyone in the session, and since A49
  // a prompt on it names the files the device wrote as well.
  attach: 'leader',
  attach_ready: true,
  shared_interrupt: true,
  shared_settings: true,
  shared_attachments: true,
  // A33: an xAI account with no plan word and no windows the leader exposes, so
  // the card is the sign-in line alone.
  accounts: [{ provider: 'xai', method: 'account', plan: null, email: 'me@example.com' }],
};

/** A28: the same agent on a device whose config has not turned the leader on. */
export const grokNoLeader: AgentInfo = {
  ...grokAgent,
  attach_ready: false,
  shared_interrupt: false,
  shared_settings: false,
  shared_attachments: false,
  // A33: this machine's client is older than the amendment, so it never looked
  // at how its agents are signed in and the page draws nothing for it.
  accounts: undefined,
};

/**
 * A26: the device's own extension gives pi three permission modes, images and a
 * terminal presence, so an app draws its permission picker exactly as Codex's.
 */
export const piAgent: AgentInfo = {
  agent: 'pi',
  available: true,
  version: '0.85.1',
  path: '/Users/me/.local/bin/pi',
  models: [
    { id: 'anthropic/claude-sonnet-4-5', label: 'Claude Sonnet 4.5' },
    { id: 'openai/gpt-5', label: 'GPT-5' },
  ],
  default_model: 'anthropic/claude-sonnet-4-5',
  permission_modes: [
    { id: 'untrusted', label: 'Ask for everything' },
    { id: 'on-request', label: 'Ask when needed' },
    { id: 'never', label: 'Never ask' },
  ],
  default_permission_mode: 'on-request',
  efforts: [
    { id: 'off', label: 'Off' },
    { id: 'low', label: 'Low' },
    { id: 'medium', label: 'Medium' },
    { id: 'high', label: 'High' },
  ],
  default_effort: 'medium',
  // A27: pi answers `get_commands` with its prompts, skills and extensions.
  capabilities: [
    'worktree',
    'interrupt',
    'queue',
    'steer',
    'attachments',
    'effort',
    'history',
    'commands',
  ],
  attach: 'extension',
  attach_ready: true,
  shared_interrupt: true,
  shared_settings: true,
  shared_attachments: true,
  // A33: pi signs in per provider, so it is the one agent with two accounts —
  // an Anthropic subscription with windows, and an OpenAI key through a relay,
  // which has no plan window to measure.
  accounts: [
    {
      provider: 'anthropic',
      method: 'account',
      plan: 'max',
      limits: [
        { window_minutes: 300, used_percent: 16, resets_at: 1789487040000 },
        { window_minutes: 10080, used_percent: 54, resets_at: 1789596000000 },
      ],
      limits_checked_at: 1789470000000,
    },
    { provider: 'openai', method: 'api_key', endpoint: 'api.relay.example' },
  ],
};

/**
 * A27 — what each agent offers when an app asks `session.commands`.
 *
 * The lists follow what the real agents advertise: Codex has one source, so it
 * draws no group headers; Grok Build pushes a flat list of shell-side commands
 * and skills over ACP, several of which take an argument; pi answers
 * `get_commands` with its prompt templates, skills and extension commands
 * beside its own built-in compaction, which is the one agent an app sections.
 * Claude has no command surface at all and is absent here on purpose.
 */
const codexCommands: Command[] = [
  { name: 'compact', description: 'Summarise the conversation to free up context' },
  {
    name: 'review',
    description: "Review the working tree's changes and report issues",
    argument: 'instructions',
  },
  { name: 'init', description: 'Write an AGENTS.md for this repository' },
  { name: 'status', description: "Show the session's model, settings and token use" },
  { name: 'usage', description: 'Show account usage and when the limits reset' },
  { name: 'skills', description: 'List the skills this session can use' },
  { name: 'hooks', description: 'List the lifecycle hooks that are installed' },
  { name: 'mcp', description: 'List the MCP servers and the tools they bring' },
];

const grokCommands: Command[] = [
  { name: 'compact', description: 'Compress the conversation history' },
  { name: 'context', description: 'Show what is filling the context window' },
  { name: 'session-info', description: 'Show this session’s stats' },
  { name: 'hooks-list', description: 'List the hooks installed for this project' },
  { name: 'hooks-add', description: 'Add a hook', argument: 'event command' },
  { name: 'hooks-trust', description: 'Trust the hooks this project ships' },
  { name: 'plugins', description: 'List the installed plugins' },
  { name: 'goal', description: 'Set the goal for a long task', argument: 'goal' },
  { name: 'loop', description: 'Repeat a task until it passes', argument: 'instructions' },
  { name: 'workflow', description: 'Run a saved workflow', argument: 'name' },
  { name: 'deep-research', description: 'Research a question first', argument: 'question' },
  { name: 'review', description: 'Review the changes on this branch', argument: 'path' },
];

const piCommands: Command[] = [
  {
    name: 'compact',
    description: 'Summarise the conversation to free up context',
    argument: 'instructions',
    group: 'Built-in',
  },
  {
    name: 'release-notes',
    description: 'Draft release notes from the commits since the last tag',
    argument: 'tag',
    group: 'Prompts',
  },
  {
    name: 'refactor-plan',
    description: 'Plan a refactor before touching the code',
    argument: 'area',
    group: 'Prompts',
  },
  { name: 'skill:pdf-tables', description: 'Extract tables from a PDF into CSV', group: 'Skills' },
  {
    name: 'skill:sql-review',
    description: 'Review a migration for locks and index use',
    group: 'Skills',
  },
  {
    name: 'rc-status',
    description: 'Show what the remote-control extension is attached to',
    group: 'Extensions',
  },
  {
    name: 'web-search',
    description: 'Search the web and summarise the results',
    argument: 'query',
    group: 'Extensions',
  },
];

/**
 * A40: the one command the device can type into a Claude terminal. Everything
 * else the CLI offers behind `/` either opens a dialog the device has no way
 * to answer or writes the person's settings, so the list is this alone.
 */
const claudeCommands: Command[] = [
  {
    name: 'compact',
    description: 'Summarise the conversation so far to free context',
    group: 'Built-in',
  },
];

/** A27: the list for one agent. Empty for an agent with no command surface. */
export function commandsFor(agent: AgentId): Command[] {
  if (agent === 'claude') return claudeCommands;
  if (agent === 'codex') return codexCommands;
  if (agent === 'grok') return grokCommands;
  if (agent === 'pi') return piCommands;
  return [];
}

/**
 * A22: the build the mock gateway serves as `/api/config` `client.build`.
 * `dev-mac` runs it and `dev-ci` runs the one before, on an update the gateway
 * already tried and gave up on (A36), so the device list shows both a row with
 * nothing to say and a row whose failure can be retried.
 */
export const CLIENT_VERSION = '0.1.0';
export const CLIENT_BUILD = '3f2b4a9c1d8e7f60a5b4c3d2e1f0918273645a5b6c7d8e9f0a1b2c3d4e5f6a7b';
export const OLD_CLIENT_BUILD =
  '9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c2b1a0f9e8d';

/**
 * A33 — `hello` and `agents.updated` carry an account without its windows: they
 * come from local files, so a device that re-detects its agents every quarter
 * hour republishes nothing new. Only the reply to `device.agents` carries
 * `limits`, `limits_error` and `limits_checked_at`, which is what the lists
 * below and the handler in `server.ts` divide between them.
 */
export function withoutLimits(agent: AgentInfo): AgentInfo {
  if (!agent.accounts) return agent;
  return {
    ...agent,
    accounts: agent.accounts.map((account) => {
      const stored: AgentAccount = { provider: account.provider, method: account.method };
      if ('plan' in account) stored.plan = account.plan;
      if (account.tier !== undefined) stored.tier = account.tier;
      if (account.email !== undefined) stored.email = account.email;
      if (account.endpoint !== undefined) stored.endpoint = account.endpoint;
      return stored;
    }),
  };
}

const macAgents: AgentInfo[] = [claudeAgent, codexAgent, grokAgent, piAgent];
const ciAgents: AgentInfo[] = [claudeNoShim, codexNoDaemon, grokNoLeader];

/** A33: what a `device.agents` reply answers with, per device. */
export const deviceAgents: Record<string, AgentInfo[]> = {
  'dev-mac': macAgents,
  'dev-ci': ciAgents,
};

export const devices: Device[] = [
  {
    device_id: 'dev-mac',
    name: 'mac-studio-office',
    platform: 'macos',
    hostname: 'mac-studio.local',
    arch: 'arm64',
    client_version: '0.1.0',
    client_build: CLIENT_BUILD,
    online: true,
    last_seen: now,
    created_at: minutes(60 * 24 * 9),
    latency_ms: 18,
    // A38: the device with a shell, so the row's tap and the terminal page can
    // both be driven here.
    terminal: true,
    // A25: the one device that knows all four, so the picker, the card and a
    // session of each can be seen in development.
    agents: macAgents.map(withoutLimits),
  },
  {
    device_id: 'dev-ci',
    name: 'ci-runner-01',
    platform: 'linux',
    hostname: 'ci-runner-01',
    arch: 'x86_64',
    client_version: '0.0.9',
    client_build: OLD_CLIENT_BUILD,
    // A36: the gateway's own attempt did not come back, so the row says so and
    // offers Retry update until a person takes it or a newer build arrives.
    update_state: 'failed',
    update_message: 'the device did not come back',
    online: true,
    last_seen: minutes(2),
    created_at: minutes(60 * 24 * 30),
    latency_ms: 42,
    // A38: the capability is off in this machine's configuration, so its row
    // says so instead of opening anything.
    terminal: false,
    // A28: Grok is installed here too, but this machine's config never turned
    // the leader on, so its terminal sessions are watched rather than attached.
    agents: ciAgents.map(withoutLimits),
  },
];

export const HOME: Record<string, string> = {
  'dev-mac': '/Users/me',
  'dev-ci': '/home/ci',
};

function session(partial: Partial<Session> & Pick<Session, 'session_id' | 'device_id' | 'title' | 'cwd'>): Session {
  return {
    agent: 'claude',
    git: { branch: 'main', dirty: false, ahead: 0, behind: 0, worktree: false },
    state: 'idle',
    state_detail: null,
    origin: 'remote',
    control: 'remote',
    model: 'claude-sonnet-4-5',
    permission_mode: 'acceptEdits',
    effort: 'high',
    created_at: minutes(120),
    updated_at: minutes(4),
    last_seq: 0,
    archived: false,
    turn: null,
    todos: null,
    usage: null,
    queued: 0,
    ...partial,
  };
}

export const sessions: Session[] = [
  session({
    session_id: 'ses-flaky',
    device_id: 'dev-mac',
    title: 'Fix flaky auth test',
    cwd: '/Users/me/dev/remote-control/gateway',
    state: 'running',
    updated_at: minutes(4),
    turn: { turn_id: 'turn-1', started_at: minutes(4) },
    todos: { total: 4, done: 1 },
    usage: {
      input_tokens: 39_400,
      output_tokens: 8_800,
      total_tokens: 48_200,
      context_used: 61_000,
      context_window: 200_000,
      cost_usd: 0.42,
    },
    last_seq: 0,
  }),
  session({
    session_id: 'ses-vite',
    device_id: 'dev-mac',
    title: 'Migrate web to Vite 6',
    cwd: '/Users/me/dev/remote-control/web',
    state: 'needs_approval',
    state_detail: 'Waiting on a shell command',
    updated_at: minutes(12),
    git: { branch: 'feat/vite', dirty: true, ahead: 2, behind: 0, worktree: false },
    // A47: its turn stopped to ask while nobody was looking, so the red dot is
    // on its row from the first sign-in until someone opens it.
    unseen: true,
  }),
  session({
    session_id: 'ses-push',
    device_id: 'dev-mac',
    title: 'iOS push tokens',
    cwd: '/Users/me/dev/remote-control/ios',
    state: 'idle',
    updated_at: minutes(180),
  }),
  session({
    session_id: 'ses-terminal',
    device_id: 'dev-mac',
    // Amendment A7: a mirrored session reports `running` while the terminal
    // drives the turn; `readonly` only once it is idle.
    title: 'Refactor relay routing',
    cwd: '/Users/me/dev/remote-control/client',
    state: 'running',
    origin: 'terminal',
    control: 'terminal',
    turn: { turn_id: 'terminal-turn', started_at: minutes(1) },
    updated_at: minutes(1),
  }),
  session({
    // Amendment A10: a terminal session the device is attached to. The composer
    // and approvals work; the model and permission mode belong to the terminal.
    session_id: 'ses-shared',
    device_id: 'dev-mac',
    title: 'Wire the channel shim',
    cwd: '/Users/me/dev/remote-control/protocol',
    state: 'idle',
    origin: 'terminal',
    control: 'shared',
    // A17: read from the transcript, and `auto` is a real Claude permission
    // mode the device does not advertise, so the app shows it by its id. A40
    // leaves that one setting the terminal's; the model and the effort are
    // the device's to type in, so they are ids the agent does list.
    permission_mode: 'auto',
    effort: 'high',
    updated_at: minutes(2),
  }),
  session({
    // Amendment A11: a bare `codex` TUI running inside the shared app-server
    // daemon. The attachment carries the interrupt, the settings and images,
    // so nothing in the composer is locked to the terminal.
    session_id: 'ses-codex-shared',
    device_id: 'dev-mac',
    title: 'Typecheck the web app',
    cwd: '/Users/me/dev/remote-control/web',
    agent: 'codex',
    model: 'gpt-5.4-codex',
    permission_mode: 'on-request',
    effort: 'medium',
    state: 'running',
    state_detail: 'Typed in the terminal',
    origin: 'terminal',
    control: 'shared',
    turn: { turn_id: 'codex-turn-live', started_at: minutes(1) },
    usage: {
      input_tokens: 14_980,
      output_tokens: 1_740,
      total_tokens: 16_720,
      context_used: 19_300,
      context_window: 272_000,
    },
    git: { branch: 'feat/settings-drawer', dirty: true, ahead: 2, behind: 0, worktree: false },
    updated_at: minutes(1),
  }),
  session({
    // A11: the daemon is not running on this device, so the hint asks for it.
    session_id: 'ses-codex-terminal',
    device_id: 'dev-ci',
    title: 'Bisect the ingest regression',
    cwd: '/home/ci/work/api',
    agent: 'codex',
    model: 'gpt-5.4-codex',
    permission_mode: 'on-request',
    effort: 'medium',
    // A21: `/fast` was typed in that terminal, and the chip says so.
    speed: 'priority',
    state: 'readonly',
    origin: 'terminal',
    control: 'terminal',
    updated_at: minutes(6),
  }),
  session({
    // A10: the shim is not installed on this device, so the hint asks for it.
    session_id: 'ses-attach',
    device_id: 'dev-ci',
    title: 'Nightly perf sweep',
    cwd: '/home/ci/work/api',
    state: 'readonly',
    origin: 'terminal',
    control: 'terminal',
    updated_at: minutes(8),
  }),
  session({
    // A question the agent is blocked on: the dot reads `waiting`, like the
    // pending approval above it, because both are the user's turn to answer.
    session_id: 'ses-answer',
    device_id: 'dev-ci',
    title: 'Split the ingest migration',
    cwd: '/home/ci/work/api',
    state: 'needs_input',
    state_detail: 'Waiting on an answer',
    updated_at: minutes(9),
    git: { branch: 'feat/ingest-split', dirty: true, ahead: 1, behind: 0, worktree: false },
  }),
  session({
    // A35: a session Claude Code stopped at the five-hour window, with the
    // resume the device scheduled a minute after the reset. The state is `idle`
    // and the dot says so; the notice above the transcript carries the pause.
    session_id: 'ses-limit',
    device_id: 'dev-mac',
    title: 'Port the ingest worker to asyncio',
    cwd: '/Users/me/dev/remote-control/client',
    state: 'idle',
    updated_at: minutes(7),
    resume: { at: inHours(1.5), estimated: false, attempts: 0, window_minutes: 300 },
    git: { branch: 'feat/async-ingest', dirty: true, ahead: 1, behind: 0, worktree: false },
  }),
  session({
    // The CLI failed but the session is still ours, so the row stays active and
    // its dot reads `failed` rather than `off`.
    session_id: 'ses-crash',
    device_id: 'dev-mac',
    title: 'Regenerate the API client',
    cwd: '/Users/me/dev/remote-control/gateway',
    agent: 'codex',
    model: 'gpt-5.4-codex',
    permission_mode: 'on-request',
    effort: 'medium',
    state: 'error',
    state_detail: 'the CLI exited with 1',
    updated_at: minutes(35),
  }),
  session({
    // The CLI exited, so nothing owns this session any more: it belongs to its
    // device's Archive rather than to its active rows.
    session_id: 'ses-exited',
    device_id: 'dev-mac',
    title: 'Rewrite the pairing docs',
    cwd: '/Users/me/dev/remote-control/docs',
    state: 'stopped',
    control: 'none',
    updated_at: minutes(50),
  }),
  session({
    // Archived by hand, so it sits in its device's Archive with an "Archived"
    // mark rather than in the active rows.
    session_id: 'ses-archived',
    device_id: 'dev-ci',
    title: 'Drop the legacy ingest path',
    cwd: '/home/ci/work/api',
    agent: 'codex',
    model: 'gpt-5.4-codex',
    permission_mode: 'on-request',
    effort: 'medium',
    state: 'idle',
    archived: true,
    updated_at: minutes(60 * 26),
  }),
  session({
    session_id: 'ses-otlp',
    device_id: 'dev-ci',
    title: 'Add OTLP traces',
    cwd: '/home/ci/work/api',
    agent: 'codex',
    model: 'gpt-5.4-codex',
    permission_mode: 'on-request',
    effort: null,
    state: 'idle',
    updated_at: minutes(60),
    git: { branch: 'main', dirty: false, ahead: 0, behind: 1, worktree: false },
  }),
  session({
    // A28: this `grok` was started before the leader flag went on, so it runs
    // its own agent and the device can only mirror it. The device is ready, so
    // the hint asks for a restart rather than for the setup command.
    session_id: 'ses-grok-terminal',
    device_id: 'dev-mac',
    title: 'Trim the ACP update log',
    cwd: '/Users/me/dev/remote-control/client',
    agent: 'grok',
    model: 'grok-4.6',
    permission_mode: 'acceptEdits',
    effort: 'xhigh',
    state: 'readonly',
    origin: 'terminal',
    control: 'terminal',
    updated_at: minutes(7),
    git: { branch: 'feat/grok-mirror', dirty: true, ahead: 1, behind: 0, worktree: false },
  }),
  session({
    // A28: a `grok` TUI inside the machine's leader, which the device joined
    // with `session/load`. The leader takes the interrupt and the settings from
    // any of its clients, so Stop and the pickers are live; its prompts take no
    // images, so the composer draws no attachment button.
    session_id: 'ses-grok-shared',
    device_id: 'dev-mac',
    title: 'Rework the leader reconnect',
    cwd: '/Users/me/dev/remote-control/client',
    agent: 'grok',
    model: 'grok-4.6',
    permission_mode: 'auto',
    effort: 'high',
    state: 'running',
    state_detail: 'Typed in the terminal',
    origin: 'terminal',
    control: 'shared',
    turn: { turn_id: 'grok-turn-live', started_at: minutes(1) },
    usage: {
      input_tokens: 21_300,
      output_tokens: 2_450,
      total_tokens: 23_750,
      context_used: 26_100,
      context_window: 256_000,
    },
    git: { branch: 'feat/grok-leader', dirty: true, ahead: 3, behind: 0, worktree: false },
    updated_at: minutes(1),
  }),
  session({
    // A28: the leader was never turned on over here, so this TUI runs its own
    // agent and the hint asks for `rc-client grok setup`.
    session_id: 'ses-grok-no-leader',
    device_id: 'dev-ci',
    title: 'Profile the wheel build',
    cwd: '/home/ci/work/infra',
    agent: 'grok',
    model: 'grok-4.5',
    permission_mode: 'default',
    effort: 'medium',
    state: 'readonly',
    origin: 'terminal',
    control: 'terminal',
    updated_at: minutes(11),
  }),
  session({
    // A27: a Grok session an app started, so its 75-command list can actually
    // be opened — the mirrored terminal row above it never draws a composer.
    session_id: 'ses-grok',
    device_id: 'dev-mac',
    title: 'Port the hook allowlist',
    cwd: '/Users/me/dev/remote-control/client',
    agent: 'grok',
    model: 'grok-4.6',
    permission_mode: 'default',
    effort: 'high',
    state: 'idle',
    updated_at: minutes(9),
  }),
  session({
    // A26: pi asks through the device's extension, so it has the three
    // permission modes and the composer draws a picker for them.
    session_id: 'ses-pi',
    device_id: 'dev-mac',
    title: 'Sketch the RPC event assembler',
    cwd: '/Users/me/dev/remote-control/client',
    agent: 'pi',
    model: 'anthropic/claude-sonnet-4-5',
    permission_mode: 'on-request',
    effort: 'high',
    state: 'idle',
    updated_at: minutes(22),
    usage: {
      input_tokens: 8_120,
      output_tokens: 1_310,
      total_tokens: 9_430,
      context_used: 11_400,
      context_window: 200_000,
      cost_usd: 0.07,
    },
  }),
  session({
    // A second exited Codex session, so a device's Archive holds more than one
    // row and both halves of it are visible in the mock.
    session_id: 'ses-exited-codex',
    device_id: 'dev-mac',
    title: 'Port the popover placement',
    cwd: '/Users/me/dev/remote-control/web',
    agent: 'codex',
    model: 'gpt-5.4-codex',
    permission_mode: 'on-request',
    effort: 'medium',
    state: 'stopped',
    control: 'none',
    updated_at: minutes(200),
  }),
  session({
    session_id: 'ses-exited-ci',
    device_id: 'dev-ci',
    title: 'Cache the wheel build',
    cwd: '/home/ci/work/infra',
    state: 'stopped',
    control: 'none',
    updated_at: minutes(320),
  }),
  session({
    // Archived by hand on the other device, so both devices show the mark.
    session_id: 'ses-archived-mac',
    device_id: 'dev-mac',
    title: 'Sketch the pairing QR flow',
    cwd: '/Users/me/dev/remote-control/docs',
    state: 'idle',
    archived: true,
    updated_at: minutes(60 * 40),
  }),
];

/**
 * A43: what a session holds behind its turn when the mock starts. The session
 * waiting on an approval keeps three messages, the middle one with two files —
 * which can be removed but not edited — so the Up next chip, its list and an
 * edit going back to its place can all be seen at once.
 */
export function queueFor(sessionId: string): QueuedMessage[] {
  if (sessionId !== 'ses-vite') return [];
  return [
    {
      id: '3f6c2a91-7d4e-4b1a-9c55-0e8b7d2f41a6',
      text: 'Then run the full test suite.',
      ts: minutes(9),
    },
    {
      id: '8b1d4e70-2c9a-4f63-a1e8-5d7c3b9f0a24',
      text: 'These two screenshots show the drawer before and after.',
      ts: minutes(8),
      attachments: 2,
    },
    {
      id: 'c52e9b18-6a3f-4d07-8e4c-1b9a7f3d6e85',
      text: 'After that, bump Vite in the lockfile and rebuild.',
      ts: minutes(7),
    },
  ];
}

/** Final-form history events (what `session.history` must return). */
export function historyFor(sessionId: string): SessionEvent[] {
  switch (sessionId) {
    case 'ses-flaky':
      return flakyHistory();
    case 'ses-vite':
      return viteHistory();
    case 'ses-terminal':
      return terminalHistory();
    case 'ses-shared':
      return sharedHistory();
    case 'ses-attach':
      return attachHistory();
    case 'ses-codex-shared':
      return codexSharedHistory();
    case 'ses-codex-terminal':
      return codexTerminalHistory();
    case 'ses-answer':
      return answerHistory();
    case 'ses-crash':
      return crashHistory();
    case 'ses-limit':
      return limitHistory();
    case 'ses-otlp':
      return codexHistory();
    case 'ses-grok-terminal':
      return grokTerminalHistory();
    case 'ses-grok-shared':
      return grokSharedHistory();
    case 'ses-grok-no-leader':
      return grokNoLeaderHistory();
    case 'ses-pi':
      return piHistory();
    default:
      return [];
  }
}

/**
 * A35: a turn Claude Code ended at the five-hour window. The vendor's sentence
 * goes out as an `error`, never as the agent's words; the turn closes with
 * `limit`, and the device's `resume` row says when it will continue.
 */
function limitHistory(): SessionEvent[] {
  const base = minutes(9);
  const resetsAt = inHours(1.5) - 60_000;
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'lm-u1',
      source: 'remote',
      text: 'port the ingest worker to asyncio and keep the retry semantics',
    },
    {
      seq: 2,
      ts: base + 2_400,
      kind: 'assistant_text',
      block_id: 'lm-a1',
      done: true,
      text: 'Starting with the queue reader: it is the only place that blocks, so the rest follows once it awaits.',
    },
    {
      seq: 3,
      ts: base + 9_000,
      kind: 'tool_call',
      block_id: 'lm-t1',
      tool: 'edit_file',
      tool_kind: 'edit',
      title: 'ingest/worker.py',
      status: 'succeeded',
      started_at: base + 4_000,
      ended_at: base + 9_000,
      duration_ms: 5_000,
      diff: { path: 'ingest/worker.py', additions: 34, deletions: 21 },
    },
    {
      seq: 4,
      ts: base + 120_000,
      kind: 'error',
      message: "You've hit your session limit · resets 2:20pm (UTC)",
      code: 'rate_limit',
    },
    {
      seq: 5,
      ts: base + 120_100,
      kind: 'turn_completed',
      turn_id: 'limit-turn',
      stop_reason: 'error',
      duration_ms: 120_100,
      limit: { window_minutes: 300, resets_at: resetsAt },
    },
    {
      seq: 6,
      ts: base + 120_200,
      kind: 'resume',
      status: 'scheduled',
      at: resetsAt + 60_000,
      estimated: false,
    },
  ];
}

/**
 * A25: a Grok Build session someone started in a terminal. The device read it
 * from the update log Grok keeps, so the transcript is there and the composer
 * is not — nothing here can write to it.
 */
function grokTerminalHistory(): SessionEvent[] {
  const base = minutes(9);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'gk-u1',
      source: 'terminal',
      text: 'trim the update log to the last 500 events on load',
    },
    {
      seq: 2,
      ts: base + 1_400,
      kind: 'thinking',
      block_id: 'gk-th1',
      done: true,
      text: 'The log is append-only and the cursor is the event id, so a tail read is enough — nothing has to rewrite the file.',
      duration_ms: 4_200,
    },
    {
      seq: 3,
      ts: base + 5_000,
      kind: 'tool_call',
      block_id: 'gk-t1',
      tool: 'read_file',
      tool_kind: 'read',
      title: 'updates.jsonl (tail)',
      status: 'succeeded',
      started_at: base + 4_200,
      ended_at: base + 5_000,
      duration_ms: 800,
      input: { path: '~/.grok/sessions/-Users-me-dev/9d1c/updates.jsonl' },
      output: '500 lines, last event id 9d1c-4821',
    },
    {
      seq: 4,
      ts: base + 7_200,
      kind: 'assistant_text',
      block_id: 'gk-a1',
      done: true,
      text: 'The reader now seeks to the last 500 event ids and resumes from `9d1c-4821`, so a restart replays nothing it has already shown.',
    },
    {
      seq: 5,
      ts: base + 7_400,
      kind: 'turn_completed',
      turn_id: 'grok-turn-1',
      stop_reason: 'completed',
      duration_ms: 7_300,
    },
  ];
}

/**
 * A28: a Grok Build session a TUI holds inside the leader. The prompt was typed
 * in the terminal, the leader fanned every row out to the device as well, and
 * the turn is still running, so the chat opens on a live tool row.
 */
function grokSharedHistory(): SessionEvent[] {
  const base = minutes(3);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'gs-u1',
      source: 'terminal',
      text: 'reconnect to the leader when the child dies and load every session we held',
    },
    {
      seq: 2,
      ts: base + 1_600,
      kind: 'assistant_text',
      block_id: 'gs-a1',
      done: true,
      text: 'Reconnecting re-runs `initialize` and then `session/load` for each held id, so the replay is bounded by the cursor rather than by the whole log.',
    },
    { seq: 3, ts: base + 2_000, kind: 'turn_started', turn_id: 'grok-turn-live', trigger: 'terminal' },
    {
      seq: 4,
      ts: base + 2_400,
      kind: 'user_message',
      block_id: 'gs-u2',
      source: 'terminal',
      text: 'and cover the case where the leader came back on a newer build',
    },
    {
      seq: 5,
      ts: base + 5_800,
      kind: 'thinking',
      block_id: 'gs-th1',
      done: true,
      text: '`initialize` answers with the leader\'s own version, so comparing it against the installed binary is enough to spot the drift.',
      duration_ms: 3_200,
    },
    {
      seq: 6,
      ts: base + 9_000,
      kind: 'tool_call',
      block_id: 'gs-t1',
      tool: 'run_command',
      tool_kind: 'shell',
      title: 'pytest tests/test_grok_leader.py',
      status: 'running',
      started_at: base + 8_400,
      input: { command: 'uv run pytest -q tests/test_grok_leader.py' },
      output: 'collected 14 items\n',
    },
  ];
}

/** A28: a Grok TUI on a machine whose config never turned the leader on. */
function grokNoLeaderHistory(): SessionEvent[] {
  const base = minutes(11);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'gn-u1',
      source: 'terminal',
      text: 'where does the wheel build spend its time on this runner?',
    },
    {
      seq: 2,
      ts: base + 5_200,
      kind: 'assistant_text',
      block_id: 'gn-a1',
      done: true,
      text: 'Almost all of it is the native extension: 94 s of the 112 s wall time, and none of it is cached between runs.',
    },
  ];
}

/**
 * A26: a pi turn that read and wrote nothing, so `on-request` had nothing to
 * ask about. The picker for its three modes is in the composer all the same.
 */
function piHistory(): SessionEvent[] {
  const base = minutes(24);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'pi-u1',
      source: 'remote',
      text: 'Assemble the streamed message deltas by content index.',
    },
    {
      seq: 2,
      ts: base + 1_100,
      kind: 'thinking',
      block_id: 'pi-th1',
      done: true,
      text: 'Deltas arrive per content index and never repeat, so one buffer per index and a join at `message_end` is all it takes.',
      duration_ms: 3_100,
    },
    {
      seq: 3,
      ts: base + 5_200,
      kind: 'assistant_text',
      block_id: 'pi-a1',
      done: true,
      text: 'Each `contentIndex` now owns a buffer, and `text_delta` and `thinking_delta` land in their own blocks. Records split on LF only, so a U+2028 inside a string never ends one.',
    },
    {
      seq: 4,
      ts: base + 5_400,
      kind: 'turn_completed',
      turn_id: 'pi-turn-1',
      stop_reason: 'completed',
      duration_ms: 5_300,
      usage: {
        input_tokens: 8_120,
        output_tokens: 1_310,
        total_tokens: 9_430,
        context_used: 11_400,
        context_window: 200_000,
        cost_usd: 0.07,
      },
    },
  ];
}

function flakyHistory(): SessionEvent[] {
  const base = minutes(30);
  return [
    { seq: 1, ts: base, kind: 'turn_started', turn_id: 'turn-0', trigger: 'remote' },
    {
      seq: 2,
      ts: base + 100,
      kind: 'user_message',
      block_id: 'blk-u0',
      source: 'remote',
      text: 'Set up the repro harness for the auth suite first.',
    },
    {
      seq: 3,
      ts: base + 4_000,
      kind: 'assistant_text',
      block_id: 'blk-a0',
      done: true,
      text: 'Added a `--count` harness to the auth suite so the flake reproduces locally.\n\n| run | failures |\n| --- | --- |\n| 20 | 1 |\n| 100 | 6 |',
    },
    {
      seq: 4,
      ts: base + 5_000,
      kind: 'turn_completed',
      turn_id: 'turn-0',
      stop_reason: 'completed',
      duration_ms: 4_900,
    },
  ];
}

function viteHistory(): SessionEvent[] {
  const base = minutes(20);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'v-u1',
      source: 'remote',
      text: 'Bump the web app to Vite 6 and keep the build green.',
    },
    {
      seq: 2,
      ts: base + 2_000,
      kind: 'assistant_text',
      block_id: 'v-a1',
      done: true,
      text: 'Upgrading the toolchain. I need to run the install before I can verify the build.',
    },
    {
      seq: 3,
      ts: base + 3_000,
      kind: 'approval',
      block_id: 'v-ap1',
      request_id: 'req-vite-1',
      tool: 'Bash',
      tool_kind: 'shell',
      title: 'npm install --no-audit',
      input: { command: 'npm install --no-audit', cwd: '/Users/me/dev/remote-control/web' },
      status: 'pending',
      options: [
        { id: 'allow', label: 'Allow once', style: 'primary' },
        { id: 'allow_session', label: 'Allow for session', style: 'secondary' },
        { id: 'deny', label: 'Deny', style: 'danger' },
      ],
    },
  ];
}

function terminalHistory(): SessionEvent[] {
  const base = minutes(15);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 't-u1',
      source: 'terminal',
      text: 'split the relay router into per-domain handlers',
    },
    {
      seq: 2,
      ts: base + 1_500,
      kind: 'assistant_text',
      block_id: 't-a1',
      done: true,
      text: 'Splitting `router.py` into `router/devices.py` and `router/sessions.py`.',
    },
    // A30: a teammate reported back and the CLI filed it as a user turn. The
    // device reduced the envelope to who reported and what they said.
    {
      seq: 3,
      ts: base + 2_000,
      kind: 'user_message',
      block_id: 't-u2',
      source: 'agent',
      text: 'recon-ios: Recon complete. The session list already pages; three call sites still read the old cursor.',
    },
    {
      seq: 4,
      ts: base + 3_600,
      kind: 'assistant_text',
      block_id: 't-a2',
      done: true,
      text: 'Taking the three call sites onto the new cursor before the split lands.',
    },
    {
      seq: 5,
      ts: base + 4_000,
      kind: 'notice',
      level: 'info',
      text: 'This session is driven from a terminal on mac-studio-office.',
    },
  ];
}

/**
 * A10: a terminal-typed prompt on a session the device is attached to. A20:
 * the CLI asked a question while it ran, the app saw the card at the same
 * moment as the terminal saw its dialog, and the terminal answered first.
 */
function sharedHistory(): SessionEvent[] {
  const base = minutes(9);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'sh-u0',
      source: 'terminal',
      text: 'add the channel handshake to the protocol doc',
    },
    {
      seq: 2,
      ts: base + 1_200,
      kind: 'question',
      block_id: 'sh-q0',
      request_id: 'req-shared-1',
      status: 'resolved',
      by: 'terminal',
      answers: { placement: ['section11'] },
      questions: [
        {
          id: 'placement',
          prompt: 'Where should the handshake go?',
          options: [
            { id: 'section11', label: 'Section 11, beside the amendments' },
            { id: 'section4', label: 'Section 4, with the device socket' },
          ],
          multi: false,
          allow_text: true,
        },
      ],
    },
    {
      seq: 3,
      ts: base + 2_400,
      kind: 'assistant_text',
      block_id: 'sh-a0',
      done: true,
      text: 'Documented the handshake in §11. Anything typed here also reaches the terminal.',
    },
    {
      seq: 4,
      ts: base + 2_600,
      kind: 'turn_completed',
      turn_id: 'shared-turn-0',
      stop_reason: 'completed',
      duration_ms: 2_500,
    },
  ];
}

/** A10: a terminal session that could be attached but was not. */
function attachHistory(): SessionEvent[] {
  const base = minutes(8);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'at-u1',
      source: 'terminal',
      text: 'run the perf sweep and summarise the regressions',
    },
    {
      seq: 2,
      ts: base + 3_000,
      kind: 'assistant_text',
      block_id: 'at-a1',
      done: true,
      text: 'Sweep finished. Two endpoints regressed by more than 5%.',
    },
  ];
}

/**
 * A11: a Codex thread typed in the terminal that the device is attached to
 * through the daemon. The last approval was answered in the TUI, so it resolved
 * with the reserved `elsewhere` option id.
 */
function codexSharedHistory(): SessionEvent[] {
  const base = minutes(4);
  const options = [
    { id: 'allow', label: 'Allow', style: 'primary' as const },
    { id: 'allow_session', label: 'Allow for this session', style: 'secondary' as const },
    { id: 'allow_always', label: 'Always allow commands like this', style: 'secondary' as const },
    { id: 'deny', label: 'Deny', style: 'danger' as const },
  ];
  const approval = {
    kind: 'approval' as const,
    block_id: 'cs-ap0',
    request_id: 'req-codex-history',
    tool: 'shell',
    tool_kind: 'shell' as const,
    title: 'npm run lint',
    input: {
      command: "/bin/zsh -lc 'npm run lint'",
      cwd: '/Users/me/dev/remote-control/web',
      command_actions: [{ type: 'unknown', command: 'npm run lint' }],
    },
    options,
  };
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'cs-u0',
      source: 'terminal',
      text: 'run the typecheck and fix whatever it reports',
    },
    { ...approval, seq: 2, ts: base + 1_200, status: 'pending' },
    {
      ...approval,
      seq: 3,
      ts: base + 6_800,
      first_seq: 2,
      status: 'resolved',
      // A11 §5.7: the TUI answered first, so the device never decided.
      decision: { option_id: 'elsewhere', by: 'terminal' },
    },
    {
      seq: 4,
      ts: base + 9_000,
      kind: 'assistant_text',
      block_id: 'cs-a0',
      done: true,
      text: 'Lint is clean. Running `tsc --noEmit` next — it is slower, so I will report the first batch of errors as they come.',
    },
    { seq: 5, ts: base + 9_200, kind: 'turn_started', turn_id: 'codex-turn-live', trigger: 'terminal' },
    {
      seq: 6,
      ts: base + 12_000,
      kind: 'tool_call',
      block_id: 'cs-t0',
      tool: 'shell',
      tool_kind: 'shell',
      title: 'npm run typecheck',
      status: 'running',
      started_at: base + 11_000,
      input: { command: 'npm run typecheck' },
      output: '> tsc --noEmit\n',
    },
  ];
}

/** A11: a Codex TUI on a device where the daemon socket is absent. */
function codexTerminalHistory(): SessionEvent[] {
  const base = minutes(6);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'ct-u1',
      source: 'terminal',
      text: 'bisect the ingest latency regression between v1.3 and main',
    },
    {
      seq: 2,
      ts: base + 4_000,
      kind: 'assistant_text',
      block_id: 'ct-a1',
      done: true,
      text: 'The regression lands on `feat/batch-ingest`. I need the collector logs to narrow it further.',
    },
  ];
}

/** A session blocked on a question, so the list shows the `waiting` tone. */
function answerHistory(): SessionEvent[] {
  const base = minutes(10);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'an-u1',
      source: 'remote',
      text: 'Split the ingest migration into two steps.',
    },
    {
      seq: 2,
      ts: base + 2_000,
      kind: 'assistant_text',
      block_id: 'an-a1',
      done: true,
      text: 'Both halves are ready. I need to know which one runs first.',
    },
    {
      seq: 3,
      ts: base + 2_400,
      kind: 'question',
      block_id: 'an-q1',
      request_id: 'req-answer-1',
      status: 'pending',
      questions: [
        {
          id: 'order',
          prompt: 'Which migration should run first?',
          options: [
            { id: 'backfill', label: 'Backfill the new columns' },
            { id: 'swap', label: 'Swap the read path' },
          ],
          multi: false,
          allow_text: true,
        },
      ],
    },
  ];
}

/** A session whose CLI failed, so the list shows the `failed` tone. */
function crashHistory(): SessionEvent[] {
  const base = minutes(36);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'cr-u1',
      source: 'remote',
      text: 'Regenerate the API client from the OpenAPI document.',
    },
    {
      seq: 2,
      ts: base + 1_800,
      kind: 'notice',
      level: 'error',
      text: 'The generator exited with 1: openapi.yaml is not valid YAML.',
    },
  ];
}

function codexHistory(): SessionEvent[] {
  const base = minutes(70);
  return [
    {
      seq: 1,
      ts: base,
      kind: 'user_message',
      block_id: 'c-u1',
      source: 'remote',
      text: 'Add OTLP span exporters to the ingest path.',
    },
    {
      seq: 2,
      ts: base + 3_000,
      kind: 'tool_call',
      block_id: 'c-t1',
      tool: 'shell',
      tool_kind: 'shell',
      title: 'rg -n "tracer" src/',
      status: 'succeeded',
      started_at: base + 2_000,
      ended_at: base + 3_000,
      duration_ms: 1_000,
      output: 'src/ingest.py:12:tracer = trace.get_tracer(__name__)\nsrc/api.py:8:tracer = trace.get_tracer(__name__)',
    },
    {
      seq: 3,
      ts: base + 6_000,
      kind: 'assistant_text',
      block_id: 'c-a1',
      done: true,
      text: 'Both entry points already build a tracer. I wired the OTLP exporter in `src/telemetry.py` and pointed the collector at `$OTEL_EXPORTER_OTLP_ENDPOINT`.',
    },
    {
      seq: 4,
      ts: base + 6_500,
      kind: 'turn_completed',
      turn_id: 'c-turn-1',
      stop_reason: 'completed',
      duration_ms: 6_400,
    },
  ];
}

export const directories: Record<string, { path: string; parent: string | null; entries: string[] }[]> = {};

export const recentDirs = [
  { path: '/Users/me/dev/remote-control/gateway', last_used: minutes(30) },
  { path: '/Users/me/dev/remote-control/web', last_used: minutes(120) },
  { path: '/Users/me/work/api', last_used: minutes(60 * 26) },
];
