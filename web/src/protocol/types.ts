/**
 * Wire protocol v1 types. Mirrors PROTOCOL-FROZEN.md exactly.
 * Unknown fields must be ignored, so every object type is open at the edges
 * only where the contract allows extension (agent ids, tool kinds, capabilities).
 */

export const PROTOCOL_VERSION = 1;

export type AgentId = 'claude' | 'codex' | (string & {});

export type Platform = 'macos' | 'linux';

export type ErrorCode =
  | 'bad_request'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'device_offline'
  | 'agent_unavailable'
  | 'conflict'
  | 'timeout'
  | 'internal'
  | 'unsupported'
  | 'too_large';

export interface WireError {
  code: ErrorCode | string;
  message: string;
}

export interface Choice {
  id: string;
  label: string;
}

export type Capability =
  | 'worktree'
  | 'takeover'
  | 'interrupt'
  | 'queue'
  | 'steer'
  | 'attachments'
  | 'effort'
  | 'history'
  /** A27: the agent's sessions can list and run slash commands from an app. */
  | 'commands'
  | (string & {});

/**
 * A27 (4.11): one slash command a session offers right now. `name` is what the
 * user types after the slash and what goes back on the wire, unchanged.
 */
export interface Command {
  name: string;
  description: string;
  /**
   * Placeholder for whatever may follow the name — `instructions`, `path`.
   * Absent when the command takes nothing, which is also how an app decides
   * whether taking the row leaves a trailing space behind.
   */
  argument?: string;
  /**
   * Where the command comes from: `Built-in`, `Skills`, `Prompts`,
   * `Extensions`. The list is sectioned by it only when more than one is
   * present, because a single header says nothing.
   */
  group?: string;
}

/**
 * How a terminal-started session can be attached (amendment A10).
 * `channel` is the Claude channel shim, `daemon` the Codex shared app-server,
 * `extension` the device's own extension that pi loads into every one of its
 * sessions (A26), and `leader` Grok Build's leader process — one shared backend
 * per machine that its TUI joins and the device joins as another client (A28).
 */
export type AttachMode = 'channel' | 'daemon' | 'extension' | 'leader';

/**
 * A33: how an agent is signed in with a vendor. `account` is the vendor's own
 * subscription signed in with OAuth, `api_key` a key.
 */
export type AccountMethod = 'account' | 'api_key';

/** A33: one rate-limit window of an account, as a `device.agents` reply reads it. */
export interface AgentLimit {
  /** The window's length: 300 for five hours, 10080 for a week. */
  window_minutes: number;
  /** What the window is confined to when it is not everything, in the vendor's words. */
  scope?: string;
  used_percent: number;
  resets_at?: number | null;
}

/**
 * A33: one credential an agent holds on a device. `limits`, `limits_error` and
 * `limits_checked_at` reach an app in a `device.agents` reply only; `hello` and
 * `agents.updated` carry the rest, which comes from the agent's own files.
 */
export interface AgentAccount {
  /** The vendor as the agent names it: `anthropic`, `openai`, `xai`, another id. */
  provider: string;
  method: AccountMethod;
  /** The plan word the vendor records, lowercase as reported. */
  plan?: string | null;
  /** A finer tier when the vendor exposes one. */
  tier?: string;
  email?: string;
  /** For `api_key`: the third-party host the key is sent to. Host only. */
  endpoint?: string;
  /**
   * The windows the device read. Empty when the vendor reported none; absent
   * with no `limits_error` when the vendor exposes none the device can read.
   */
  limits?: AgentLimit[];
  /** Why `limits` is missing after the device tried, in the device's words. */
  limits_error?: string;
  limits_checked_at?: number;
}

/**
 * A40: the four settings `session.set` carries, which an attachment may drive
 * one by one rather than all or none.
 */
export type SharedSettingKey = 'model' | 'permission_mode' | 'effort' | 'speed';

export interface AgentInfo {
  agent: AgentId;
  available: boolean;
  version: string | null;
  path: string | null;
  models: Choice[];
  default_model: string | null;
  permission_modes: Choice[];
  default_permission_mode: string | null;
  efforts: Choice[];
  default_effort: string | null;
  /**
   * A21: speed tiers the agent can run a session at beyond its standard speed,
   * for example Codex's `priority` ("Fast"). Absent or empty when it has none,
   * and an agent with none draws no control.
   */
  speeds?: Choice[];
  capabilities: Capability[];
  /** Amendment A10: how this agent's terminal sessions can be attached. */
  attach?: AttachMode | null;
  /** A10: whether the device is prepared to attach. Hints on `terminal` only. */
  attach_ready?: boolean;
  /** A10: whether `session.stop` works on `shared` sessions. Defaults to false. */
  shared_interrupt?: boolean;
  /**
   * A11: whether `session.set` for `model`, `permission_mode` and `effort`
   * works on `shared` sessions. Defaults to false.
   */
  shared_settings?: boolean;
  /**
   * A40: with `shared_settings` true, the subset of the four settings the
   * device changes on a `shared` session. The rest stay what the terminal set
   * and are drawn as values (A17). Absent means all four; never present when
   * `shared_settings` is false.
   */
  shared_settings_keys?: SharedSettingKey[];
  /**
   * A11: whether `session.send.attachments` are delivered on `shared`
   * sessions. Defaults to false.
   */
  shared_attachments?: boolean;
  /**
   * A33: one entry per vendor credential the agent holds on this device.
   * Absent when the device did not look, empty when the agent is installed but
   * signed in nowhere.
   */
  accounts?: AgentAccount[];
}

/** A24: what the account routes of 3.9 are gated on. */
export type UserRole = 'admin' | 'member';

/** A24: a disabled account cannot sign in and its devices are refused. */
export type UserState = 'active' | 'disabled';

/** A24 (4.10): the account an app signed in as. */
export interface User {
  username: string;
  role: UserRole;
}

/** A24 (4.10): one account as the admin lists it. */
export interface UserRecord extends User {
  state: UserState;
  created_at: number;
  last_login_at: number | null;
  devices: number;
}

/** A22: where an app-requested client update stands. Absent means `idle`. */
export type DeviceUpdateState = 'idle' | 'updating' | 'failed';

export interface Device {
  device_id: string;
  name: string;
  platform: Platform;
  hostname: string;
  arch: string;
  client_version: string;
  /** A22: SHA-256 of the wheel the client was installed from; null when unknown. */
  client_build?: string | null;
  /** A22: an app-requested update in flight or failed. */
  update_state?: DeviceUpdateState;
  /** A22: why the last update failed. */
  update_message?: string | null;
  online: boolean;
  last_seen: number;
  created_at: number;
  latency_ms: number | null;
  agents: AgentInfo[];
  /**
   * A38: true when the device offers a shell. Absent from a client older than
   * A38, which is the same as false: its row opens nothing and says so.
   */
  terminal?: boolean;
}

export interface GitInfo {
  branch: string;
  dirty: boolean;
  ahead: number;
  behind: number;
  worktree: boolean;
}

export type SessionState =
  | 'starting'
  | 'idle'
  | 'running'
  | 'needs_approval'
  | 'needs_input'
  | 'error'
  | 'stopped'
  | 'readonly';

/**
 * Amendment A10: `shared` is a live CLI process the device is attached to.
 * Apps treat it like `remote` for the composer, approvals and the queue.
 */
export type ControlOwner = 'remote' | 'terminal' | 'shared' | 'none';

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  /** Optional (amendment A2): Codex reports no cost and may omit context. */
  context_used?: number | null;
  context_window?: number | null;
  cost_usd?: number | null;
}

export interface TurnRef {
  turn_id: string;
  started_at: number;
}

export interface TodoCounts {
  total: number;
  done: number;
}

/**
 * A35 (4.4): why a turn ended when the vendor's usage window was used up. The
 * device reads it from the agent's own signal, never from its words, and
 * `resets_at` is null when the vendor named no time.
 */
export interface LimitStop {
  /** The window that was hit: 300 for five hours, 10080 for a week. */
  window_minutes?: number;
  resets_at: number | null;
}

/**
 * A35 (4.4): the one resume a session can have pending after a usage limit
 * stopped it. `estimated` says the device computed `at` from the window's
 * length because the vendor named no reset time.
 */
export interface SessionResume {
  at: number;
  estimated: boolean;
  /** How many resumes have already run into the limit again. */
  attempts: number;
  window_minutes?: number;
}

/**
 * A41 (3.2): the words the app speaks about itself. A wire value since the
 * Settings screen's choices became the account's.
 */
export type InterfaceLanguage = 'en' | 'zh-Hans';

/** A41 (3.2): how much of a transcript is drawn. */
export type TimelineDetail = 'simple' | 'detailed';

/**
 * A35, A41 (3.2): the Settings values that read the same on every app and
 * device of the account, so they live on the gateway rather than in this
 * browser. Every field but `resume_after_limit` is absent until somebody has
 * set it: an app then keeps the value it has and writes it up the first time
 * it connects, so an account that arrives at A41 keeps what its first app had.
 */
export interface Preferences {
  resume_after_limit: boolean;
  language?: InterfaceLanguage;
  /**
   * A44: the language a phone that recognises speech itself listens for,
   * Chinese when unset. The iPhone owns it; the web never reads or writes it.
   */
  stt_language?: string;
  polish_enabled?: boolean;
  /** Chosen from `GET /api/polish/models`; empty when none. */
  polish_model?: string;
  polish_strength?: PolishStrength;
  timeline_detail?: TimelineDetail;
}

export interface Session {
  session_id: string;
  device_id: string;
  agent: AgentId;
  title: string;
  cwd: string;
  git: GitInfo | null;
  state: SessionState;
  state_detail: string | null;
  origin: 'remote' | 'terminal';
  control: ControlOwner;
  model: string | null;
  permission_mode: string | null;
  effort: string | null;
  /** A21: the tier from `AgentInfo.speeds` this session runs at. Null is standard. */
  speed?: string | null;
  created_at: number;
  updated_at: number;
  last_seq: number;
  archived: boolean;
  turn: TurnRef | null;
  todos: TodoCounts | null;
  usage: Usage | null;
  queued: number;
  /** A35: the resume the device has scheduled after a usage limit, if any. */
  resume?: SessionResume | null;
  /**
   * A47 (4.4): the session stopped working and waits for the person, and
   * nobody on the account has opened it since. The gateway's alone — a device
   * never sends it — and absent is false, which is all an older gateway says.
   */
  unseen?: boolean;
}

/* ---------------------------------------------------------------- events */

export type ToolKind =
  | 'shell'
  | 'read'
  | 'edit'
  | 'write'
  | 'search'
  | 'web'
  | 'mcp'
  | 'subagent'
  | 'todo'
  | 'other'
  | (string & {});

export type ToolStatus = 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface DiffInfo {
  path: string;
  additions: number;
  deletions: number;
  patch?: string;
  patch_truncated?: boolean;
}

export interface Attachment {
  name: string;
  mime: string;
  size: number;
}

export interface OutgoingAttachment {
  name: string;
  mime: string;
  data_base64: string;
}

interface EventBase {
  seq: number;
  ts: number;
  parent_block_id?: string;
  /**
   * Amendment A8: the `seq` at which this block first appeared, set on block
   * events only (equal to `seq` on the first one). Apps order blocks by
   * `first_seq ?? seq` so a late-finishing tool call keeps its place.
   */
  first_seq?: number;
}

/**
 * Amendment A10, `shared` sessions only. `delivered`: injected into the CLI;
 * `absorbed`: the CLI read it as mid-turn data and the device will re-inject
 * it. A message the device is still holding is a queue entry rather than a
 * block, so it has no delivery state at all (amendment A19).
 */
export type MessageDelivery = 'delivered' | 'absorbed';

/**
 * Who caused a message or a turn — the schema's one `Trigger`, shared by
 * `user_message.source` and `turn_started.trigger`. Amendment A30 adds `agent`:
 * words the CLI filed as a user turn that no person typed, another agent's
 * message or a background task's notification. A35 adds `resume`: the fixed
 * sentence the device sent for the person once a usage limit reset, which reads
 * as a remote message and a remote turn.
 */
export type Trigger = 'remote' | 'terminal' | 'queue' | 'agent' | 'resume';

export interface UserMessageEvent extends EventBase {
  kind: 'user_message';
  block_id: string;
  text: string;
  attachments?: Attachment[];
  source: Trigger;
  delivery?: MessageDelivery;
}

export interface AssistantTextEvent extends EventBase {
  kind: 'assistant_text';
  block_id: string;
  delta?: string;
  text?: string;
  done: boolean;
}

export interface ThinkingEvent extends EventBase {
  kind: 'thinking';
  block_id: string;
  delta?: string;
  text?: string;
  done: boolean;
  duration_ms?: number;
}

export interface ToolCallEvent extends EventBase {
  kind: 'tool_call';
  block_id: string;
  tool: string;
  /** Tool category (amendment A1). Devices always send it. */
  tool_kind?: ToolKind;
  title: string;
  status: ToolStatus;
  input?: Record<string, unknown>;
  input_truncated?: boolean;
  output?: string;
  output_truncated?: boolean;
  summary?: string;
  diff?: DiffInfo;
  started_at: number;
  ended_at?: number;
  duration_ms?: number;
}

export interface TodoItem {
  id: string;
  text: string;
  status: 'pending' | 'in_progress' | 'completed';
}

export interface TodosEvent extends EventBase {
  kind: 'todos';
  items: TodoItem[];
}

export interface ApprovalOption {
  id: string;
  label: string;
  style: 'primary' | 'secondary' | 'danger';
}

export interface ApprovalEvent extends EventBase {
  kind: 'approval';
  block_id: string;
  request_id: string;
  tool: string;
  /** Tool category (amendment A1). */
  tool_kind?: ToolKind;
  title: string;
  input?: Record<string, unknown>;
  diff?: DiffInfo;
  options: ApprovalOption[];
  status: 'pending' | 'resolved' | 'expired';
  decision?: { option_id: string; by: 'remote' | 'terminal' | 'policy' };
}

export interface QuestionOption {
  id: string;
  label: string;
  description?: string;
}

export interface QuestionSpec {
  id: string;
  prompt: string;
  options: QuestionOption[];
  multi: boolean;
  allow_text: boolean;
  secret?: boolean;
}

export type QuestionAnswers = Record<string, string[] | string>;

export interface QuestionEvent extends EventBase {
  kind: 'question';
  block_id: string;
  request_id: string;
  questions: QuestionSpec[];
  status: 'pending' | 'resolved' | 'expired';
  answers?: QuestionAnswers;
  /** A20: who answered a resolved question, when the device knows. */
  by?: 'remote' | 'terminal';
}

export interface TurnStartedEvent extends EventBase {
  kind: 'turn_started';
  turn_id: string;
  /** A30: `agent` is a turn another agent's message started; read as `terminal`. */
  trigger: Trigger;
}

export interface TurnCompletedEvent extends EventBase {
  kind: 'turn_completed';
  turn_id: string;
  stop_reason: 'completed' | 'interrupted' | 'error';
  duration_ms: number;
  usage?: Usage;
  /** A35: present when the vendor's usage limit ended the turn. */
  limit?: LimitStop;
}

/** A35 (5.15): what the device did about a session the usage limit stopped. */
export type ResumeStatus = 'scheduled' | 'rescheduled' | 'fired' | 'cancelled' | 'dropped';

export interface ResumeEvent extends EventBase {
  kind: 'resume';
  status: ResumeStatus;
  /** For `scheduled` and `rescheduled`: when the prompt will be sent. */
  at?: number;
  estimated?: boolean;
  attempts?: number;
  /** For `cancelled` and `dropped`: why, in the device's words. */
  reason?: string;
}

export interface StatusEvent extends EventBase {
  kind: 'status';
  state: SessionState;
  detail?: string;
}

export interface MetaEvent extends EventBase {
  kind: 'meta';
  title?: string;
  model?: string;
  permission_mode?: string;
  effort?: string;
  /** A21: the tier the session now runs at; null is the standard speed. */
  speed?: string | null;
  cwd?: string;
  git?: GitInfo | null;
  control?: ControlOwner;
  agent_version?: string;
}

export interface QueuedMessage {
  id: string;
  text: string;
  ts: number;
  /**
   * A43: how many files the held message carries, absent when it carries none.
   * The files stay on the device and no frame brings them back, so such an
   * entry can be removed but not edited.
   */
  attachments?: number;
}

export interface QueueEvent extends EventBase {
  kind: 'queue';
  pending: QueuedMessage[];
}

export interface NoticeEvent extends EventBase {
  kind: 'notice';
  level: 'info' | 'warn' | 'error';
  text: string;
}

export interface ErrorEvent extends EventBase {
  kind: 'error';
  message: string;
  code?: string;
  block_id?: string;
}

export type SessionEvent =
  | UserMessageEvent
  | AssistantTextEvent
  | ThinkingEvent
  | ToolCallEvent
  | TodosEvent
  | ApprovalEvent
  | QuestionEvent
  | TurnStartedEvent
  | TurnCompletedEvent
  | StatusEvent
  | MetaEvent
  | QueueEvent
  | NoticeEvent
  | ErrorEvent
  | ResumeEvent;

export type SessionEventKind = SessionEvent['kind'];

/* ------------------------------------------- dictation polish (A29, §3.5) */

/** Whether the gateway can polish a dictation through the model its operator configured. */
export interface PolishInfo {
  enabled: boolean;
}

/** One model of the operator's OpenAI-compatible provider. */
export interface PolishModel {
  id: string;
  label: string;
}

export interface PolishModelsResponse {
  models: PolishModel[];
}

/**
 * How far the model may go. `moderate` removes fillers, false starts and
 * repetitions, corrects plain mishearings and punctuates; `strong` also
 * restructures for clarity and resolves vague references from the conversation.
 */
export type PolishStrength = 'moderate' | 'strong';

/** One message of the conversation the app already shows, as the model sees it. */
export interface PolishContextItem {
  role: 'user' | 'assistant';
  text: string;
}

export interface PolishRequest {
  text: string;
  model: string;
  strength: PolishStrength;
  /**
   * A hint, not a rule (A44): `auto` for words the gateway transcribed, the
   * language a phone's own recogniser listened for otherwise.
   */
  language?: string;
  context: PolishContextItem[];
}

export interface PolishResponse {
  text: string;
}
