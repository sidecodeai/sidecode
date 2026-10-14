/**
 * The dictation state machine: the mic button starts it, only Cancel and Done
 * end it, nothing is ever sent, and a dictation longer than one gateway
 * utterance is chained across sockets without losing the seam.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useVoice, type RecorderHandlers, type VoiceRecorder } from '../src/features/voice/useVoice';
import { SttSocket, type SttSocketLike } from '../src/features/voice/sttSocket';

class FakeSocket implements SttSocketLike {
  binaryType = '';
  readyState = 1;
  sent: (string | ArrayBuffer)[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
  }

  /** One frame from the gateway. */
  deliver(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  private says(type: string): boolean {
    return this.sent.some((item) => typeof item === 'string' && item.includes(type));
  }

  get toldToTranscribe(): boolean {
    return this.says('stt.stop');
  }

  get toldToDrop(): boolean {
    return this.says('stt.cancel');
  }
}

interface Harness {
  sockets: FakeSocket[];
  transcripts: { text: string; isFinal: boolean }[];
  speakAt: (level: number) => void;
}

function harness() {
  const sockets: FakeSocket[] = [];
  const transcripts: { text: string; isFinal: boolean }[] = [];
  let handlers: RecorderHandlers | null = null;

  const factory = (): SttSocketLike => {
    const socket = new FakeSocket();
    sockets.push(socket);
    // The gateway accepts after the caller has installed its handlers.
    void Promise.resolve().then(() => socket.onopen?.({}));
    return socket;
  };
  const recorder: VoiceRecorder = { start: () => Promise.resolve(true), stop: () => Promise.resolve() };
  const recorderFactory = (incoming: RecorderHandlers): VoiceRecorder => {
    handlers = incoming;
    return recorder;
  };

  const view = renderHook(
    ({ enabled }: { enabled: boolean }) =>
      useVoice({
        enabled,
        onTranscript: (text, isFinal) => transcripts.push({ text, isFinal }),
        factory,
        recorderFactory,
      }),
    { initialProps: { enabled: true } },
  );

  const tools: Harness = {
    sockets,
    transcripts,
    speakAt: (level) => handlers?.onLevel(level),
  };
  return { ...tools, result: view.result, rerender: view.rerender, unmount: view.unmount };
}

/** Let the timers and the promises they release settle together. */
const settle = async (ms = 0): Promise<void> => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useVoice', () => {
  it('listens from the moment the mic button is pressed', async () => {
    const { result, sockets } = harness();

    act(() => result.current.start());
    await settle();

    expect(result.current.state).toBe('listening');
    expect(sockets).toHaveLength(1);
  });

  it('keeps the transcript on Done and never marks it sent', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start());
    await settle();

    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'run the auth suite' }));
    expect(transcripts.at(-1)).toEqual({ text: 'run the auth suite', isFinal: false });

    act(() => result.current.done());
    await settle();
    expect(sockets[0]!.toldToTranscribe).toBe(true);

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'run the auth suite on CI' }));
    await settle();

    expect(transcripts.at(-1)).toEqual({ text: 'run the auth suite on CI', isFinal: true });
    expect(result.current.state).toBe('idle');
  });

  it('publishes nothing on Cancel and drops the utterance', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start());
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'forget this' }));

    act(() => result.current.cancel());
    await settle();

    expect(result.current.state).toBe('idle');
    expect(sockets[0]!.toldToDrop).toBe(true);
    expect(transcripts.filter((entry) => entry.isFinal)).toHaveLength(0);
  });

  it('has no time limit: five minutes of speech is still listening', async () => {
    const { result, transcripts, speakAt } = harness();
    act(() => result.current.start());
    await settle();

    act(() => speakAt(0.5));
    await settle(5 * 60_000);

    expect(result.current.state).toBe('listening');
    expect(transcripts.filter((entry) => entry.isFinal)).toHaveLength(0);
  });

  it('chains segments across sockets and joins them in spoken order', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start());
    await settle();

    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'first half' }));
    await settle(30_000);

    // The replacement is taking audio before the outgoing one transcribes.
    expect(sockets).toHaveLength(2);
    expect(sockets[0]!.toldToTranscribe).toBe(true);

    act(() => sockets[1]!.deliver({ type: 'stt.partial', text: 'second half' }));
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'first half exactly' }));
    await settle();

    expect(transcripts.at(-1)?.text).toBe('first half exactly second half');
    expect(transcripts.at(-1)?.isFinal).toBe(false);

    act(() => result.current.done());
    await settle();
    act(() => sockets[1]!.deliver({ type: 'stt.final', text: 'second half exactly' }));
    await settle();

    expect(transcripts.at(-1)).toEqual({
      text: 'first half exactly second half exactly',
      isFinal: true,
    });
  });

  it('holds a cut until the speaker pauses', async () => {
    const { result, sockets, speakAt } = harness();
    act(() => result.current.start());
    await settle();

    act(() => speakAt(0.6));
    await settle(30_000);
    expect(sockets).toHaveLength(1);

    act(() => speakAt(0));
    await settle(400);
    expect(sockets).toHaveLength(2);
    expect(result.current.state).toBe('listening');
  });

  it('keeps what it recognised when the gateway fails mid-utterance', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start());
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'half a sentence' }));

    act(() => sockets[0]!.deliver({ type: 'stt.error', message: 'backend unavailable' }));
    await settle();

    expect(result.current.state).toBe('error');
    expect(result.current.error).toBe('backend unavailable');
    expect(transcripts.at(-1)).toEqual({ text: 'half a sentence', isFinal: true });
  });
});

/**
 * The composer can be taken away mid-dictation — the device goes offline, or
 * the terminal takes the session back. `enabled` is what says so, and a run
 * already in flight used to keep the microphone open and keep streaming audio
 * to the gateway for as long as the tab lived.
 */
describe('a composer that stops accepting dictation', () => {
  it('ends a run already in flight and keeps what it recognised', async () => {
    const { result, sockets, transcripts, rerender } = harness();
    act(() => result.current.start());
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'half a sentence' }));
    expect(result.current.state).toBe('listening');

    act(() => rerender({ enabled: false }));
    await settle();

    expect(result.current.state).toBe('idle');
    expect(sockets[0]!.toldToDrop).toBe(true);
    // The words are in the field already; nothing publishes over them.
    expect(transcripts.at(-1)).toEqual({ text: 'half a sentence', isFinal: false });
  });

  it('leaves an idle controller alone', async () => {
    const { result, sockets, rerender } = harness();

    act(() => rerender({ enabled: false }));
    await settle();

    expect(result.current.state).toBe('idle');
    expect(sockets).toHaveLength(0);
  });
});

/**
 * One socket's own failures. A connect that never settles left the composer in
 * `starting` for ever — no waveform, Done disabled, nothing to press — and a
 * dictation cancelled while the first socket was still connecting retained the
 * promise nobody would ever resolve.
 */
describe('SttSocket connect', () => {
  const silent = (): { socket: SttSocketLike } => {
    const socket = new FakeSocket();
    return { socket };
  };

  it('gives up on a gateway that never accepts the socket', async () => {
    const { socket } = silent();
    const stt = new SttSocket({ onEvent: () => undefined, factory: () => socket });

    const connect = stt.start();
    const settled = vi.fn();
    void connect.then(settled, settled);
    await settle(4_000);
    expect(settled).not.toHaveBeenCalled();

    await settle(2_000);
    await expect(connect).rejects.toThrow(/timed out/);
  });

  it('rejects a connect the dictation cancelled under it', async () => {
    const { socket } = silent();
    const stt = new SttSocket({ onEvent: () => undefined, factory: () => socket });

    const connect = stt.start();
    stt.cancel();

    await expect(connect).rejects.toThrow(/cancelled/);
  });
});

/** A44: the gateway's provider detects the language, so the socket names none. */
describe('SttSocket address', () => {
  it('opens /ws/stt with no language', async () => {
    const socket = new FakeSocket();
    const urls: string[] = [];
    const stt = new SttSocket({
      onEvent: () => undefined,
      factory: (url) => {
        urls.push(url);
        return socket;
      },
    });

    const connect = stt.start();
    socket.onopen?.({});
    await connect;

    expect(urls).toHaveLength(1);
    const url = new URL(urls[0] ?? '');
    expect(url.pathname).toBe('/ws/stt');
    expect(url.search).toBe('');
  });
});
