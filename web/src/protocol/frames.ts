/** App <-> gateway WebSocket frames (PROTOCOL-FROZEN.md §5). */
import type {
  AgentId,
  AgentInfo,
  Command,
  Device,
  GitInfo,
  OutgoingAttachment,
  PolishInfo,
  Preferences,
  QuestionAnswers,
  QueuedMessage,
  Session,
  SessionEvent,
  User,
  WireError,
} from './types';

export interface ReplyOk<T> {
  type: 'reply';
  id: string;
  ok: true;
  result: T;
}
export interface ReplyErr {
  type: 'reply';
  id: string;
  ok: false;
  error: WireError;
}
export type Reply<T = unknown> = ReplyOk<T> | ReplyErr;

export interface HelloFrame {
  type: 'hello';
  protocol: number;
  gateway_version: string;
  user: User;
  devices: Device[];
  sessions: Session[];
  /** A44: `languages` is always `["auto"]`; the gateway's provider detects the language. */
  stt: { enabled: boolean; languages: string[] };
  /** A29: absent from a gateway too old to polish dictation, which is "off". */
  polish?: PolishInfo;
  /** A35: absent from a gateway too old to hold the account's preferences. */
  preferences?: Preferences;
  server_time: number;
}

export type PairingStep = 'waiting' | 'enrolled' | 'online' | 'agents';

export type PushFrame =
  | HelloFrame
  | { type: 'device.updated'; device: Device }
  | { type: 'device.removed'; device_id: string }
  | { type: 'session.updated'; session: Session }
  | { type: 'session.removed'; session_id: string; device_id: string }
  // Amendment A5: the gateway stamps `device_id` on forwarded session frames.
  // `session_id` stays the primary key; `device_id` is routing information.
  | { type: 'session.event'; session_id: string; device_id?: string; event: SessionEvent }
  | { type: 'pairing.progress'; code: string; step: PairingStep; device?: Device }
  // A35: the account's preferences changed, from this app or another one.
  | { type: 'preferences.updated'; preferences: Preferences }
  // A38: bytes one terminal produced, for the single app connection holding it.
  // The device's `to` never reaches an app; the gateway strips it and stamps
  // `device_id` instead. `seq` starts at 1 and rises by one per frame, so a
  // jump is a gap and not a pause.
  | { type: 'terminal.output'; terminal_id: string; device_id: string; seq: number; data: string }
  // A38: that shell ended and the id is free.
  | { type: 'terminal.exited'; terminal_id: string; device_id: string; code: number }
  | { type: 'ping' };

export type ServerFrame = PushFrame | Reply;

/* ------------------------------------------------------------- requests */

export interface SubscribeResult {
  session: Session;
  events: SessionEvent[];
  resync: boolean;
  /** Amendment A6: the latest queue snapshot, absent when the gateway saw none. */
  queue?: { pending: QueuedMessage[] };
}

export interface CreateSessionParams {
  device_id: string;
  agent: AgentId;
  cwd: string;
  model?: string;
  permission_mode?: string;
  effort?: string;
  /** A21: a tier id from `AgentInfo.speeds`; omitted for the standard speed. */
  speed?: string;
  worktree?: boolean;
  first_message?: string;
  title?: string;
}

export type SendMode = 'auto' | 'queue' | 'interrupt';

export interface SendParams {
  session_id: string;
  text: string;
  attachments?: OutgoingAttachment[];
  mode: SendMode;
  /**
   * A43: the `ts` a queued message had before it was taken out to be edited.
   * When the message queues, the device holds it under that `ts`, back in the
   * place it left.
   */
  queue_ts?: number;
}

export interface SendResult {
  accepted: 'sent' | 'queued' | 'steered';
  queued_id?: string;
}

export interface HistoryResult {
  events: SessionEvent[];
  has_more: boolean;
}

export interface DirEntry {
  name: string;
  path: string;
  is_git: boolean;
}

export interface DirsResult {
  path: string;
  parent: string | null;
  entries: DirEntry[];
  recent: { path: string; last_used: number }[];
}

export interface GitResult {
  is_repo: boolean;
  branch?: string;
  dirty?: boolean;
  ahead?: number;
  behind?: number;
}

export interface AgentsResult {
  agents: AgentInfo[];
}

export interface SessionResult {
  session: Session;
}

export interface BlockResult {
  event: SessionEvent;
}

/** A22: the device took the update and names the build it is leaving. */
export interface UpdateAcceptedResult {
  accepted: boolean;
  from?: string | null;
}

/** A27: what the session's agent offers now. May legitimately be empty. */
export interface CommandsResult {
  commands: Command[];
}

/** A38: the shell is running; every frame for it now names this connection. */
export interface TerminalOpenResult {
  terminal_id: string;
}

/**
 * A38: output comes to this connection from now on, and `scrollback` is base64
 * of the last 64 KiB the terminal produced — the screen as it was left.
 */
export interface TerminalAttachResult {
  terminal_id: string;
  cols: number;
  rows: number;
  scrollback: string;
}

/** Request type -> (params, result) mapping used by the typed socket client. */
export interface RequestMap {
  'session.subscribe': [{ session_id: string; since_seq?: number }, SubscribeResult];
  /**
   * A47 (6.2): the person has this conversation in front of them. The gateway
   * clears `unseen` and, when it was set, sends `session.updated` to every
   * socket of the account. Idempotent; another account's session is
   * `not_found`.
   */
  'session.seen': [{ session_id: string }, Record<string, never>];
  'session.create': [CreateSessionParams, SessionResult];
  'session.send': [SendParams, SendResult];
  'session.stop': [{ session_id: string }, Record<string, never>];
  'session.approve': [
    { session_id: string; request_id: string; option_id: string; message?: string },
    Record<string, never>,
  ];
  'session.answer': [
    { session_id: string; request_id: string; answers: QuestionAnswers },
    Record<string, never>,
  ];
  'session.set': [
    {
      session_id: string;
      model?: string;
      permission_mode?: string;
      effort?: string;
      /** A21: a tier id from `AgentInfo.speeds`, or null for the standard speed. */
      speed?: string | null;
      title?: string;
    },
    SessionResult,
  ];
  /** A27: the slash commands this session offers right now. */
  'session.commands': [{ session_id: string }, CommandsResult];
  /**
   * A27: run one. The result is `{}` — the echo and the outcome arrive as
   * events, the echo under this request's own id.
   */
  'session.command': [
    { session_id: string; name: string; argument?: string },
    Record<string, never>,
  ];
  'session.history': [
    { session_id: string; before_seq?: number; limit?: number },
    HistoryResult,
  ];
  'session.block': [{ session_id: string; block_id: string }, BlockResult];
  'session.queue_remove': [{ session_id: string; queued_id: string }, Record<string, never>];
  /**
   * A35: schedule the resume of §7.2 for `at`, or move the pending one there.
   * `bad_request` unless `at` is at least a minute ahead and within eight days;
   * `conflict` while a turn runs or while the terminal controls the session.
   */
  'session.resume_set': [{ session_id: string; at: number }, SessionResult];
  /** A35: remove the pending resume. Idempotent. */
  'session.resume_cancel': [{ session_id: string }, SessionResult];
  'session.takeover': [{ session_id: string }, SessionResult];
  'session.archive': [{ session_id: string; archived: boolean }, SessionResult];
  'session.delete': [{ session_id: string }, Record<string, never>];
  'device.dirs': [{ device_id: string; path?: string }, DirsResult];
  'device.git': [{ device_id: string; path: string }, GitResult];
  /**
   * A37: make one directory, `name`, inside `path`, and answer with the new
   * directory's listing — empty, so the picker can stand in it and choose it.
   */
  'device.mkdir': [{ device_id: string; path: string; name: string }, DirsResult];
  'device.agents': [{ device_id: string }, AgentsResult];
  /** A22: bring the device to the build the gateway serves. */
  'device.update': [{ device_id: string; build: string }, UpdateAcceptedResult];
  /**
   * A38: start the person's login shell in a pseudo-terminal of this size.
   * `unsupported` when the device offers no terminal, `conflict` at the fifth.
   */
  'terminal.open': [{ device_id: string; cols: number; rows: number }, TerminalOpenResult];
  /** A38: bytes as typed, base64, at most 64 KiB decoded. */
  'terminal.input': [{ device_id: string; terminal_id: string; data: string }, Record<string, never>];
  /** A38: 1-500 columns, 1-200 rows. */
  'terminal.resize': [
    { device_id: string; terminal_id: string; cols: number; rows: number },
    Record<string, never>,
  ];
  /** A38: take a detached terminal over, with the screen it was left on. */
  'terminal.attach': [{ device_id: string; terminal_id: string }, TerminalAttachResult];
  /** A38: end the shell. Idempotent. */
  'terminal.close': [{ device_id: string; terminal_id: string }, Record<string, never>];
}

export type RequestType = keyof RequestMap;
export type RequestParams<T extends RequestType> = RequestMap[T][0];
export type RequestResult<T extends RequestType> = RequestMap[T][1];

export interface GitProbe extends GitResult {
  path: string;
}
export type { GitInfo };
