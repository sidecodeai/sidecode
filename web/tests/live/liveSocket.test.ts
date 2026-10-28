import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  CODE_TIMEOUT,
  CONNECT_TIMEOUT_MS,
  LiveSocket,
  MAX_BUFFERED_BYTES,
  SILENCE_TIMEOUT_MS,
  type CloseInfo,
  type ServerMessage,
} from '../../src/features/live/liveSocket';
import { FakeWebSocket, READY } from './audioFakes';

function setup() {
  const messages: ServerMessage[] = [];
  const audio: ArrayBuffer[] = [];
  const closes: CloseInfo[] = [];
  const sockets: FakeWebSocket[] = [];
  const socket = new LiveSocket(
    {
      onMessage: (message) => messages.push(message),
      onAudio: (frame) => audio.push(frame),
      onClose: (info) => closes.push(info),
    },
    (url) => {
      const created = new FakeWebSocket(url);
      sockets.push(created);
      return created;
    },
  );
  socket.open();
  const ws = sockets[0] as FakeWebSocket;
  return { socket, ws, messages, audio, closes };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('LiveSocket', () => {
  it('dials the gateway on /ws/live and takes binary frames as ArrayBuffers', () => {
    const { ws } = setup();
    expect(ws.url).toMatch(/^wss?:\/\/[^/]+\/ws\/live$/);
    expect(ws.binaryType).toBe('arraybuffer');
  });

  it('hands binary frames on as audio and JSON as messages', () => {
    const { ws, messages, audio } = setup();
    ws.open();
    ws.serverSend(READY);
    ws.serverAudio(1920);
    ws.serverSend({ type: 'state', speaker: 'assistant' });
    expect(messages.map((m) => m.type)).toEqual(['ready', 'state']);
    expect(audio).toHaveLength(1);
    expect(audio[0]?.byteLength).toBe(1920);
  });

  it('ignores what is not a message', () => {
    const { ws, messages } = setup();
    ws.open();
    ws.serverSend('not json');
    ws.serverSend('[1,2]');
    ws.serverSend('{"nope":1}');
    ws.serverSend('"text"');
    ws.serverSend('null');
    expect(messages).toEqual([]);
  });

  it('sends microphone frames and commands once the socket is open', () => {
    const { socket, ws } = setup();
    const frame = new ArrayBuffer(960);
    expect(socket.sendAudio(frame)).toBe(false);
    socket.send({ type: 'hangup' });
    expect(ws.sent).toEqual([]);

    ws.open();
    expect(socket.sendAudio(frame)).toBe(true);
    socket.send({ type: 'captions', enabled: true });
    expect(ws.frames()).toHaveLength(1);
    expect(ws.commands()).toEqual([{ type: 'captions', enabled: true }]);
  });

  it('drops microphone frames rather than queue them behind a slow connection', () => {
    const { socket, ws } = setup();
    ws.open();
    ws.bufferedAmount = MAX_BUFFERED_BYTES + 1;
    expect(socket.sendAudio(new ArrayBuffer(960))).toBe(false);
    expect(ws.frames()).toHaveLength(0);
    ws.bufferedAmount = 0;
    expect(socket.sendAudio(new ArrayBuffer(960))).toBe(true);
  });

  it('reports why the gateway ended the call when the socket closes', () => {
    const { ws, closes } = setup();
    ws.open();
    ws.serverSend({ type: 'ended', reason: 'idle' });
    ws.serverClose(1000);
    expect(closes).toEqual([{ ended: 'idle', code: 1000 }]);
  });

  it('reports a close with no reason as one', () => {
    const { ws, closes } = setup();
    ws.open();
    ws.serverClose(1006);
    expect(closes).toEqual([{ ended: null, code: 1006 }]);
  });

  it('reports a close only once', () => {
    const { ws, closes } = setup();
    ws.open();
    ws.serverClose(1000);
    ws.serverClose(1000);
    vi.advanceTimersByTime(CONNECT_TIMEOUT_MS * 2);
    expect(closes).toHaveLength(1);
  });

  it('closes from this end without reporting, and hears nothing more', () => {
    const { socket, ws, messages, closes } = setup();
    ws.open();
    socket.close();
    expect(ws.closedWith).toBe(1000);
    ws.serverSend(READY);
    ws.serverClose(1000);
    expect(messages).toEqual([]);
    expect(closes).toEqual([]);
    expect(socket.sendAudio(new ArrayBuffer(960))).toBe(false);
  });

  it('gives up on a gateway that never says ready', () => {
    const { ws, closes } = setup();
    vi.advanceTimersByTime(CONNECT_TIMEOUT_MS - 1);
    expect(closes).toEqual([]);
    vi.advanceTimersByTime(2);
    expect(closes).toEqual([{ ended: null, code: CODE_TIMEOUT }]);
    expect(ws.closedWith).toBe(1000);
  });

  it('gives up on a gateway that goes quiet, and any message keeps it alive', () => {
    const { ws, closes } = setup();
    ws.open();
    ws.serverSend(READY);
    for (let i = 0; i < 5; i += 1) {
      vi.advanceTimersByTime(SILENCE_TIMEOUT_MS - 1000);
      ws.serverSend({ type: 'time', seconds_left: 100 });
    }
    expect(closes).toEqual([]);
    vi.advanceTimersByTime(SILENCE_TIMEOUT_MS + 1);
    expect(closes).toEqual([{ ended: null, code: CODE_TIMEOUT }]);
  });

  it('counts audio as the gateway being there', () => {
    const { ws, closes } = setup();
    ws.open();
    ws.serverSend(READY);
    vi.advanceTimersByTime(SILENCE_TIMEOUT_MS - 1000);
    ws.serverAudio(1920);
    vi.advanceTimersByTime(SILENCE_TIMEOUT_MS - 1000);
    expect(closes).toEqual([]);
  });
});
