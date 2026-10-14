/**
 * App socket (`WS /ws/app`, PROTOCOL-FROZEN.md §5).
 *
 * Responsibilities: one connection with exponential backoff capped at 5 s,
 * application-level liveness (reply to `ping`, treat 60 s of total silence as a
 * half-open socket), request/reply correlation with timeouts, and subscription
 * bookkeeping so `session.subscribe` is replayed with the right `since_seq`
 * after every reconnect.
 */
import type { PushFrame, Reply, RequestParams, RequestResult, RequestType, ServerFrame } from '../protocol/frames';
import type { WireError } from '../protocol/types';
import { requestId } from './ids';

export type SocketStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

/**
 * A failed request, wire or local. The `code` is what the app reads: every
 * sentence a person sees comes from `lib/errors.ts` and the string tables, so
 * a failure minted here carries an empty message rather than English nobody
 * could translate.
 */
export class RequestError extends Error {
  readonly code: string;
  constructor(error: WireError) {
    super(error.message);
    this.name = 'RequestError';
    this.code = error.code;
  }
}

export interface SocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
}

export interface AppSocketOptions {
  url: string;
  /** Injected for tests. */
  factory?: (url: string) => SocketLike;
  now?: () => number;
  setTimeout?: (fn: () => void, ms: number) => number;
  clearTimeout?: (handle: number) => void;
  onFrame: (frame: PushFrame) => void;
  onStatus: (status: SocketStatus) => void;
  /** Called when the socket closes with an auth failure so the app can log out. */
  onUnauthorized?: () => void;
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
  timer: number;
}

interface Subscription {
  sessionId: string;
  sinceSeq: number | undefined;
  /** Resolved with the subscribe reply each time the subscription is (re)established. */
  onResult: (result: RequestResult<'session.subscribe'>) => void;
  onError: (error: unknown) => void;
}

const PING_TIMEOUT_MS = 60_000;
const LIVENESS_CHECK_MS = 5_000;
const REQUEST_TIMEOUT_MS = 65_000;
const MAX_BACKOFF_MS = 5_000;
const AUTH_CLOSE_CODES = new Set([4401, 4403]);

const defaultFactory = (url: string): SocketLike => new WebSocket(url) as unknown as SocketLike;

export class AppSocket {
  private readonly opts: Required<Omit<AppSocketOptions, 'onUnauthorized'>> &
    Pick<AppSocketOptions, 'onUnauthorized'>;
  private socket: SocketLike | null = null;
  private status: SocketStatus = 'idle';
  private pending = new Map<string, Pending>();
  private subscriptions = new Map<string, Subscription>();
  private backoffMs = 250;
  private reconnectTimer: number | null = null;
  private livenessTimer: number | null = null;
  private lastFrameAt = 0;
  private stopped = false;

  constructor(options: AppSocketOptions) {
    this.opts = {
      factory: defaultFactory,
      now: () => Date.now(),
      setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms) as unknown as number,
      clearTimeout: (h) => globalThis.clearTimeout(h),
      ...options,
    };
  }

  get state(): SocketStatus {
    return this.status;
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimer('reconnectTimer');
    this.clearTimer('livenessTimer');
    this.failAllPending(new RequestError({ code: 'internal', message: '' }));
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000, 'client stop');
    this.setStatus('closed');
  }

  /** Send a request and resolve with its `result`. Rejects with RequestError. */
  request<T extends RequestType>(
    type: T,
    params: RequestParams<T>,
    options: { id?: string; timeoutMs?: number } = {},
  ): Promise<RequestResult<T>> {
    const id = options.id ?? requestId();
    const timeoutMs = options.timeoutMs ?? REQUEST_TIMEOUT_MS;
    return new Promise<RequestResult<T>>((resolve, reject) => {
      if (!this.socket || this.status !== 'open') {
        reject(new RequestError({ code: 'device_offline', message: '' }));
        return;
      }
      const timer = this.opts.setTimeout(() => {
        this.pending.delete(id);
        reject(new RequestError({ code: 'timeout', message: '' }));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      try {
        this.socket.send(JSON.stringify({ type, id, ...params }));
      } catch (err) {
        this.pending.delete(id);
        this.opts.clearTimeout(timer);
        reject(err);
      }
    });
  }

  /**
   * Register a session subscription. Re-issued automatically after reconnect
   * with the cursor supplied by `updateCursor`.
   */
  subscribe(
    sessionId: string,
    sinceSeq: number | undefined,
    handlers: { onResult: Subscription['onResult']; onError: Subscription['onError'] },
  ): void {
    this.subscriptions.set(sessionId, { sessionId, sinceSeq, ...handlers });
    if (this.status === 'open') void this.issueSubscribe(sessionId);
  }

  updateCursor(sessionId: string, seq: number): void {
    const sub = this.subscriptions.get(sessionId);
    if (sub && (sub.sinceSeq === undefined || seq > sub.sinceSeq)) sub.sinceSeq = seq;
  }

  unsubscribe(sessionId: string): void {
    if (!this.subscriptions.delete(sessionId)) return;
    if (this.status === 'open' && this.socket) {
      this.socket.send(JSON.stringify({ type: 'session.unsubscribe', session_id: sessionId }));
    }
  }

  /* ------------------------------------------------------------ internals */

  private open(): void {
    if (this.stopped) return;
    this.clearTimer('reconnectTimer');
    this.setStatus(this.backoffMs > 250 ? 'reconnecting' : 'connecting');
    let socket: SocketLike;
    try {
      socket = this.opts.factory(this.opts.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    this.lastFrameAt = this.opts.now();

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.lastFrameAt = this.opts.now();
      this.setStatus('open');
      this.startLiveness();
      for (const sessionId of this.subscriptions.keys()) void this.issueSubscribe(sessionId);
    };
    socket.onmessage = (ev) => {
      if (this.socket !== socket) return;
      this.lastFrameAt = this.opts.now();
      if (typeof ev.data !== 'string') return;
      this.handleText(ev.data);
    };
    socket.onerror = () => {
      /* close always follows; nothing to do here */
    };
    socket.onclose = (ev) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.clearTimer('livenessTimer');
      this.failAllPending(new RequestError({ code: 'internal', message: '' }));
      const code = (ev as { code?: number } | undefined)?.code;
      if (code !== undefined && AUTH_CLOSE_CODES.has(code)) {
        this.stopped = true;
        this.setStatus('closed');
        this.opts.onUnauthorized?.();
        return;
      }
      this.scheduleReconnect();
    };
  }

  private handleText(raw: string): void {
    let frame: ServerFrame;
    try {
      frame = JSON.parse(raw) as ServerFrame;
    } catch {
      return;
    }
    if (frame.type === 'reply') {
      this.resolveReply(frame);
      return;
    }
    if (frame.type === 'ping') {
      this.socket?.send(JSON.stringify({ type: 'pong' }));
      return;
    }
    if (frame.type === 'hello') this.backoffMs = 250;
    this.opts.onFrame(frame);
  }

  private resolveReply(reply: Reply): void {
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
    this.opts.clearTimeout(pending.timer);
    if (reply.ok) pending.resolve(reply.result);
    else pending.reject(new RequestError(reply.error));
  }

  private async issueSubscribe(sessionId: string): Promise<void> {
    const sub = this.subscriptions.get(sessionId);
    if (!sub) return;
    const params: RequestParams<'session.subscribe'> = { session_id: sessionId };
    if (sub.sinceSeq !== undefined) params.since_seq = sub.sinceSeq;
    try {
      const result = await this.request('session.subscribe', params);
      if (this.subscriptions.get(sessionId) === sub) sub.onResult(result);
    } catch (err) {
      if (this.subscriptions.get(sessionId) === sub) sub.onError(err);
    }
  }

  private startLiveness(): void {
    this.clearTimer('livenessTimer');
    const tick = () => {
      if (this.stopped) return;
      if (this.opts.now() - this.lastFrameAt >= PING_TIMEOUT_MS) {
        // Half-open: mobile NAT never delivers onclose. Force a reconnect.
        const socket = this.socket;
        this.socket = null;
        socket?.close(4000, 'half-open');
        this.clearTimer('livenessTimer');
        this.failAllPending(new RequestError({ code: 'timeout', message: '' }));
        this.scheduleReconnect();
        return;
      }
      this.livenessTimer = this.opts.setTimeout(tick, LIVENESS_CHECK_MS);
    };
    this.livenessTimer = this.opts.setTimeout(tick, LIVENESS_CHECK_MS);
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.setStatus('reconnecting');
    const delay = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, MAX_BACKOFF_MS);
    this.reconnectTimer = this.opts.setTimeout(() => this.open(), delay);
  }

  private failAllPending(error: unknown): void {
    for (const [, pending] of this.pending) {
      this.opts.clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  private clearTimer(field: 'reconnectTimer' | 'livenessTimer'): void {
    const handle = this[field];
    if (handle !== null) {
      this.opts.clearTimeout(handle);
      this[field] = null;
    }
  }

  private setStatus(status: SocketStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.opts.onStatus(status);
  }
}

/** Absolute ws:// or wss:// URL for a gateway path on the current origin. */
export function socketUrl(path: string): string {
  const { protocol, host } = globalThis.location;
  const scheme = protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${host}${path}`;
}
