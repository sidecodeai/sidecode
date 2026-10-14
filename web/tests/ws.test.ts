import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppSocket, RequestError, type AppSocketOptions, type SocketLike } from '../src/lib/ws';
import type { PushFrame } from '../src/protocol/frames';
import { fixturesAvailable, readFixture } from './fixtures';

class FakeSocket implements SocketLike {
  static instances: FakeSocket[] = [];
  sent: string[] = [];
  closed: { code?: number; reason?: string } | null = null;
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closed = { ...(code === undefined ? {} : { code }), ...(reason === undefined ? {} : { reason }) };
  }

  open(): void {
    this.onopen?.({});
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  fail(code = 1006): void {
    this.onclose?.({ code });
  }

  /** The parsed frames this socket sent, newest last. */
  frames(): Record<string, unknown>[] {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>);
  }
}

function makeSocket(overrides: Partial<AppSocketOptions> = {}) {
  const pushed: PushFrame[] = [];
  const statuses: string[] = [];
  const socket = new AppSocket({
    url: 'ws://test/ws/app',
    factory: (url) => new FakeSocket(url),
    onFrame: (frame) => pushed.push(frame),
    onStatus: (status) => statuses.push(status),
    ...overrides,
  });
  return { socket, pushed, statuses };
}

const latest = (): FakeSocket => {
  const socket = FakeSocket.instances.at(-1);
  if (!socket) throw new Error('no socket created');
  return socket;
};

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('AppSocket', () => {
  it('reports status and forwards push frames after hello', () => {
    const { socket, pushed, statuses } = makeSocket();
    socket.start();
    expect(statuses).toEqual(['connecting']);
    latest().open();
    expect(statuses).toEqual(['connecting', 'open']);

    latest().receive({ type: 'hello', protocol: 1, devices: [], sessions: [] });
    expect(pushed).toHaveLength(1);
    expect(pushed[0]?.type).toBe('hello');
    socket.stop();
  });

  it('answers ping with pong and never surfaces it as a push frame', () => {
    const { socket, pushed } = makeSocket();
    socket.start();
    latest().open();
    latest().receive({ type: 'ping' });
    expect(latest().frames()).toEqual([{ type: 'pong' }]);
    expect(pushed).toHaveLength(0);
    socket.stop();
  });

  it('resolves a request with the reply result', async () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();

    const promise = socket.request('session.stop', { session_id: 's1' }, { id: 'req-1' });
    expect(latest().frames()).toEqual([{ type: 'session.stop', id: 'req-1', session_id: 's1' }]);

    latest().receive({ type: 'reply', id: 'req-1', ok: true, result: { done: true } });
    await expect(promise).resolves.toEqual({ done: true });
    socket.stop();
  });

  // A47 §6.2: the one request the gateway answers itself for the red dot.
  it.runIf(fixturesAvailable())('sends session.seen exactly as the fixture spells it', async () => {
    const fixture = readFixture<{ type: string; id: string; session_id: string }>(
      'app/session.seen.json',
    );
    const { socket } = makeSocket();
    socket.start();
    latest().open();

    const promise = socket.request(
      'session.seen',
      { session_id: fixture.session_id },
      { id: fixture.id },
    );
    expect(latest().frames()).toEqual([fixture]);

    latest().receive({ type: 'reply', id: fixture.id, ok: true, result: {} });
    await expect(promise).resolves.toEqual({});
    socket.stop();
  });

  it('rejects a request with the wire error code', async () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();
    const promise = socket.request('session.send', { session_id: 's1', text: 'x', mode: 'auto' }, { id: 'r2' });
    latest().receive({
      type: 'reply',
      id: 'r2',
      ok: false,
      error: { code: 'conflict', message: 'controlled by terminal' },
    });
    await expect(promise).rejects.toBeInstanceOf(RequestError);
    await promise.catch((err: RequestError) => expect(err.code).toBe('conflict'));
    socket.stop();
  });

  it('times a request out without leaking the pending entry', async () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();
    const promise = socket.request('session.stop', { session_id: 's1' }, { id: 'r3', timeoutMs: 500 });
    vi.advanceTimersByTime(600);
    await expect(promise).rejects.toMatchObject({ code: 'timeout' });
    socket.stop();
  });

  it('reconnects with exponential backoff capped at five seconds', () => {
    const { socket, statuses } = makeSocket();
    socket.start();
    latest().open();

    const delays: number[] = [];
    for (let i = 0; i < 8; i += 1) {
      const before = FakeSocket.instances.length;
      latest().fail();
      // Find the delay by stepping until a new socket appears.
      let waited = 0;
      while (FakeSocket.instances.length === before && waited < 20_000) {
        vi.advanceTimersByTime(10);
        waited += 10;
      }
      delays.push(waited);
      latest().open();
    }

    expect(delays[0]).toBeLessThanOrEqual(260);
    expect(Math.max(...delays)).toBeLessThanOrEqual(5_010);
    expect(delays.at(-1)).toBe(5_000);
    expect(statuses).toContain('reconnecting');
    socket.stop();
  });

  it('resets the backoff once a hello arrives', () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();
    latest().fail();
    vi.advanceTimersByTime(300);
    latest().open();
    latest().receive({ type: 'hello', protocol: 1, devices: [], sessions: [] });

    const before = FakeSocket.instances.length;
    latest().fail();
    vi.advanceTimersByTime(260);
    expect(FakeSocket.instances.length).toBe(before + 1);
    socket.stop();
  });

  it('treats sixty seconds of total silence as a half-open socket', () => {
    const { socket } = makeSocket();
    socket.start();
    const first = latest();
    first.open();

    vi.advanceTimersByTime(61_000);
    expect(first.closed?.code).toBe(4000);

    const before = FakeSocket.instances.length;
    vi.advanceTimersByTime(5_000);
    expect(FakeSocket.instances.length).toBeGreaterThan(before - 1);
    socket.stop();
  });

  it('re-issues subscriptions with the latest cursor after a reconnect', () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();

    socket.subscribe('s1', undefined, { onResult: () => {}, onError: () => {} });
    const first = latest()
      .frames()
      .find((f) => f.type === 'session.subscribe');
    expect(first).toMatchObject({ type: 'session.subscribe', session_id: 's1' });
    expect(first).not.toHaveProperty('since_seq');

    socket.updateCursor('s1', 42);
    socket.updateCursor('s1', 7); // cursors never go backwards

    latest().fail();
    vi.advanceTimersByTime(300);
    latest().open();

    const resubscribe = latest()
      .frames()
      .find((f) => f.type === 'session.subscribe');
    expect(resubscribe).toMatchObject({ session_id: 's1', since_seq: 42 });
    socket.stop();
  });

  it('delivers the subscribe reply to the registered handler', async () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();

    const results: unknown[] = [];
    socket.subscribe('s1', 10, { onResult: (r) => results.push(r), onError: () => {} });
    const frame = latest()
      .frames()
      .find((f) => f.type === 'session.subscribe');
    latest().receive({
      type: 'reply',
      id: frame?.id,
      ok: true,
      result: { session: { session_id: 's1' }, events: [], resync: true },
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(results).toHaveLength(1);
    expect(results[0]).toMatchObject({ resync: true });
    socket.stop();
  });

  it('stops reconnecting after an auth close and reports it once', () => {
    const onUnauthorized = vi.fn();
    const { socket } = makeSocket({ onUnauthorized });
    socket.start();
    latest().open();

    const before = FakeSocket.instances.length;
    latest().fail(4401);
    vi.advanceTimersByTime(10_000);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(FakeSocket.instances.length).toBe(before);
  });

  it('rejects requests made while disconnected', async () => {
    const { socket } = makeSocket();
    await expect(
      socket.request('session.stop', { session_id: 's1' }),
    ).rejects.toBeInstanceOf(RequestError);
  });

  it('fails in-flight requests when the connection drops', async () => {
    const { socket } = makeSocket();
    socket.start();
    latest().open();
    const promise = socket.request('session.stop', { session_id: 's1' }, { id: 'r9' });
    latest().fail();
    await expect(promise).rejects.toBeInstanceOf(RequestError);
    socket.stop();
  });
});
