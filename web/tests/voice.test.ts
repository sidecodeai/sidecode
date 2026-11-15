/**
 * The dictation state machine: the mic button starts it, only Cancel and Done
 * end it, nothing is ever sent, and a dictation longer than one gateway
 * utterance is chained across sockets without losing the seam.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useVoice, type RecorderHandlers, type VoiceRecorder } from '../src/features/voice/useVoice';
import { dictationMode } from '../src/features/voice/dictationMode';
import { SttSocket, type SttSocketLike } from '../src/features/voice/sttSocket';
import type { DictationNotice, TranscriptSink } from '../src/stores/dictationJobs';
import { strings } from '../src/strings';

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
  /** The address each socket was opened on. */
  urls: string[];
  transcripts: { text: string; isFinal: boolean }[];
  /** Every last call of a run, and whether it was cut short. */
  endings: { text: string; interrupted: boolean }[];
  speakAt: (level: number) => void;
  renders: () => number;
}

interface Heard {
  text: string;
  isFinal: boolean;
  interrupted: boolean;
  notice?: DictationNotice;
}

/** A job that takes over a run: every call it gets, and how many times it was asked for. */
function jobSink() {
  const heard: Heard[] = [];
  const asked = { times: 0 };
  const take = (): TranscriptSink => {
    asked.times += 1;
    const sink = (text: string, isFinal: boolean, interrupted?: boolean, notice?: DictationNotice) =>
      void heard.push({
        text,
        isFinal,
        interrupted: interrupted === true,
        ...(notice ? { notice } : {}),
      });
    return Object.assign(sink, { bindCancel: () => undefined });
  };
  return { heard, asked, take };
}

/** Sockets, by the order they are opened, whose connect the test settles itself or refuses. */
interface Connects {
  held?: number[];
  failing?: number[];
}

function harness(onDetach?: () => TranscriptSink | null, connects: Connects = {}) {
  const sockets: FakeSocket[] = [];
  const urls: string[] = [];
  const transcripts: { text: string; isFinal: boolean }[] = [];
  const endings: { text: string; interrupted: boolean }[] = [];
  let handlers: RecorderHandlers | null = null;
  let renders = 0;

  const factory = (url: string): SttSocketLike => {
    urls.push(url);
    const socket = new FakeSocket();
    const index = sockets.length;
    sockets.push(socket);
    // The gateway answers after the caller has installed its handlers.
    if (!connects.held?.includes(index)) {
      void Promise.resolve().then(() => {
        if (connects.failing?.includes(index)) socket.onerror?.({});
        else socket.onopen?.({});
      });
    }
    return socket;
  };
  const recorder: VoiceRecorder = { start: () => Promise.resolve(true), stop: () => Promise.resolve() };
  const recorderFactory = (incoming: RecorderHandlers): VoiceRecorder => {
    handlers = incoming;
    return recorder;
  };

  const view = renderHook(
    ({ enabled }: { enabled: boolean }) => {
      renders += 1;
      return useVoice({
        enabled,
        onTranscript: (text, isFinal, interrupted) => {
          transcripts.push({ text, isFinal });
          if (isFinal) endings.push({ text, interrupted: interrupted === true });
        },
        ...(onDetach ? { onDetach } : {}),
        factory,
        recorderFactory,
      });
    },
    { initialProps: { enabled: true } },
  );

  const tools: Harness = {
    sockets,
    urls,
    transcripts,
    endings,
    // A 128-sample block at a steady level: its peak, and its energy.
    speakAt: (level) => handlers?.onLevel(level, level * level * 128, 128),
    renders: () => renders,
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

    act(() => result.current.start('live'));
    await settle();

    expect(result.current.state).toBe('listening');
    expect(sockets).toHaveLength(1);
  });

  it('keeps the transcript on Done and never marks it sent', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start('live'));
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

  it('publishes nothing when the field is taken back, and drops the utterance', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start('live'));
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'forget this' }));

    act(() => result.current.takeover());
    await settle();

    // The live words are in the field already; nothing more is coming.
    expect(result.current.state).toBe('idle');
    expect(sockets[0]!.toldToDrop).toBe(true);
    expect(transcripts.filter((entry) => entry.isFinal)).toHaveLength(0);
  });

  it('has no time limit: five minutes of speech is still listening', async () => {
    const { result, transcripts, speakAt } = harness();
    act(() => result.current.start('live'));
    await settle();

    act(() => speakAt(0.5));
    await settle(5 * 60_000);

    expect(result.current.state).toBe('listening');
    expect(transcripts.filter((entry) => entry.isFinal)).toHaveLength(0);
  });

  it('chains segments across sockets and joins them in spoken order', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start('live'));
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
    act(() => result.current.start('live'));
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
    act(() => result.current.start('live'));
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
    act(() => result.current.start('live'));
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

/**
 * A57 — a gateway whose speech backend transcribes the recording afterwards
 * says so with `stt.streaming: false`, and a dictation through it is a batch
 * one: no partials are asked for, nothing reaches the field while the person
 * speaks, each segment's final is held, and the whole transcript lands once,
 * after Done. Every way of ending it early is Done, so the words still land.
 */
describe('the dictation mode', () => {
  it('is batch only when the gateway says its words do not stream', () => {
    expect(dictationMode(false)).toBe('batch');
    expect(dictationMode(true)).toBe('live');
    // A gateway too old to say keeps today's live words.
    expect(dictationMode(undefined)).toBe('live');
  });
});

describe('a batch dictation', () => {
  it('asks the gateway for no partials, on every segment', async () => {
    const { result, urls } = harness();
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);

    expect(result.current.mode).toBe('batch');
    expect(urls).toHaveLength(2);
    for (const url of urls) expect(new URL(url).search).toBe('?partials=0');
  });

  it('shows nothing while listening, and lands every segment at once after Done', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start('batch'));
    await settle();
    // A partial the gateway should not have sent is not shown either.
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'never shown' }));
    await settle(30_000);
    expect(sockets).toHaveLength(2);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'first half' }));
    await settle();

    expect(transcripts).toEqual([]);
    expect(result.current.state).toBe('listening');

    act(() => result.current.done());
    await settle();
    expect(sockets[1]!.toldToTranscribe).toBe(true);
    act(() => sockets[1]!.deliver({ type: 'stt.final', text: 'second half' }));
    await settle();

    expect(transcripts).toEqual([{ text: 'first half second half', isFinal: true }]);
    expect(result.current.state).toBe('idle');
  });

  it('hands itself to the job store when the field is reached for, freeing the composer', async () => {
    const job = jobSink();
    const { result, sockets, transcripts } = harness(job.take);
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'first half' }));

    act(() => result.current.takeover());
    await settle();

    // Done, not dropped, and the composer is free at once.
    expect(job.asked.times).toBe(1);
    expect(sockets[1]!.toldToTranscribe).toBe(true);
    expect(sockets[1]!.toldToDrop).toBe(false);
    expect(result.current.state).toBe('idle');

    act(() => sockets[1]!.deliver({ type: 'stt.final', text: 'second half' }));
    await settle();
    expect(job.heard).toEqual([
      { text: 'first half second half', isFinal: true, interrupted: false },
    ]);
    expect(transcripts).toEqual([]);
  });

  it('hands over while finishing too, and can start again at once', async () => {
    const job = jobSink();
    const { result, sockets } = harness(job.take);
    act(() => result.current.start('batch'));
    await settle();
    act(() => result.current.done());
    await settle();

    act(() => result.current.takeover());
    expect(result.current.state).toBe('idle');
    act(() => result.current.start('batch'));
    await settle();
    expect(result.current.state).toBe('listening');

    // The first run still lands where it was handed, and the second is untouched.
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'the first' }));
    await settle();
    expect(job.heard).toEqual([{ text: 'the first', isFinal: true, interrupted: false }]);
    expect(sockets[1]!.toldToDrop).toBe(false);
    expect(result.current.state).toBe('listening');
  });

  it('is Done here when nobody takes it, so its words still land', async () => {
    const { result, sockets, transcripts } = harness();
    act(() => result.current.start('batch'));
    await settle();

    act(() => result.current.takeover());
    await settle();
    expect(sockets[0]!.toldToTranscribe).toBe(true);
    expect(sockets[0]!.toldToDrop).toBe(false);
    expect(result.current.state).toBe('finishing');

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'every word' }));
    await settle();
    expect(transcripts).toEqual([{ text: 'every word', isFinal: true }]);
  });

  it('keeps the held words and says the rest was lost when the gateway never answers', async () => {
    const { result, sockets, endings } = harness();
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'held words' }));
    act(() => result.current.done());

    await settle(30_000);

    expect(endings).toEqual([{ text: 'held words', interrupted: true }]);
    expect(result.current.state).toBe('error');
    expect(result.current.error).toBe(strings.voice.failed);
  });

  it('ends with the held words when a segment closes without its final', async () => {
    const { result, sockets, endings } = harness();
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'held words' }));
    act(() => result.current.done());
    await settle();

    act(() => sockets[1]!.onclose?.({}));
    await settle();

    expect(endings).toEqual([{ text: 'held words', interrupted: true }]);
    expect(result.current.state).toBe('error');
  });

  it('gives a run it handed over its notice to carry, saying nothing itself', async () => {
    const job = jobSink();
    const { result, sockets } = harness(job.take);
    act(() => result.current.start('batch'));
    await settle();
    act(() => result.current.takeover());
    await settle();

    act(() => sockets[0]!.deliver({ type: 'stt.error', code: 'internal', message: 'backend down' }));
    await settle();

    // The job says it wherever the session is opened: one source.
    expect(job.heard).toEqual([
      { text: '', isFinal: true, interrupted: true, notice: { message: 'backend down' } },
    ]);
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('keeps the segment still listening when a cut one fails, and ends with its words', async () => {
    const { result, sockets, endings } = harness();
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'first' }));
    await settle(30_000);
    expect(sockets).toHaveLength(3);

    // That segment's words are gone, the others are not.
    act(() => sockets[1]!.deliver({ type: 'stt.error', code: 'upstream', message: 'backend busy' }));
    await settle();
    expect(sockets[2]!.toldToTranscribe).toBe(true);
    expect(sockets[2]!.toldToDrop).toBe(false);
    expect(result.current.state).toBe('finishing');
    expect(endings).toEqual([]);

    act(() => sockets[2]!.deliver({ type: 'stt.final', text: 'third' }));
    await settle();

    expect(endings).toEqual([{ text: 'first third', interrupted: true }]);
    expect(result.current.state).toBe('error');
    expect(result.current.error).toBe('backend busy');
  });

  it('keeps the listening segment when a cut one closes without its final', async () => {
    const { result, sockets, endings } = harness();
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);

    act(() => sockets[0]!.onclose?.({}));
    await settle();
    expect(sockets[1]!.toldToTranscribe).toBe(true);
    expect(sockets[1]!.toldToDrop).toBe(false);

    act(() => sockets[1]!.deliver({ type: 'stt.final', text: 'second' }));
    await settle();
    expect(endings).toEqual([{ text: 'second', interrupted: true }]);
    expect(result.current.error).toBe(strings.voice.failed);
  });

  it('keeps what the listening segment heard when a cut cannot connect', async () => {
    const { result, sockets, endings } = harness(undefined, { failing: [1] });
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);

    expect(sockets[0]!.toldToTranscribe).toBe(true);
    expect(sockets[0]!.toldToDrop).toBe(false);
    expect(result.current.state).toBe('finishing');

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'first segment words' }));
    await settle();
    expect(endings).toEqual([{ text: 'first segment words', interrupted: true }]);
    expect(result.current.error).toBe(strings.voice.failed);
  });

  it('still lands the segment told to stop when a cut connecting at Done fails', async () => {
    const { result, sockets, endings } = harness(undefined, { held: [1] });
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    expect(sockets).toHaveLength(2);
    act(() => result.current.done());
    await settle();
    expect(sockets[0]!.toldToTranscribe).toBe(true);

    act(() => sockets[1]!.onerror?.({}));
    await settle();
    expect(sockets[0]!.toldToDrop).toBe(false);

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'all of it' }));
    await settle();
    expect(endings).toEqual([{ text: 'all of it', interrupted: false }]);
    expect(result.current.state).toBe('idle');
  });

  it('ends as soon as a cut connecting at Done comes, once the rest has spoken', async () => {
    const { result, sockets, endings } = harness(undefined, { held: [1] });
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => result.current.done());
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'all of it' }));
    await settle();
    expect(endings).toEqual([]);

    act(() => sockets[1]!.onopen?.({}));
    await settle();

    expect(endings).toEqual([{ text: 'all of it', interrupted: false }]);
    expect(sockets[1]!.toldToDrop).toBe(true);
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('never turns a stray partial into held words', async () => {
    const { result, sockets, endings } = harness();
    act(() => result.current.start('batch'));
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'a partial it was not asked for' }));
    act(() => result.current.done());

    await settle(30_000);

    expect(endings).toEqual([{ text: '', interrupted: true }]);
  });

  it('applies the held words before it says the gateway failed', async () => {
    const { result, sockets, endings } = harness();
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'held words' }));

    act(() => sockets[1]!.deliver({ type: 'stt.error', message: 'backend unavailable' }));
    await settle();

    expect(endings).toEqual([{ text: 'held words', interrupted: true }]);
    expect(result.current.state).toBe('error');
    expect(result.current.error).toBe('backend unavailable');
  });

  it('is Done, not dropped, when the composer stops taking dictation', async () => {
    const { result, sockets, transcripts, rerender } = harness();
    act(() => result.current.start('batch'));
    await settle();

    act(() => rerender({ enabled: false }));
    await settle();
    expect(sockets[0]!.toldToTranscribe).toBe(true);
    expect(sockets[0]!.toldToDrop).toBe(false);

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'kept' }));
    await settle();
    expect(transcripts).toEqual([{ text: 'kept', isFinal: true }]);
  });
});

/** What the listening row reads: the levels, and the time. */
describe('the listening row', () => {
  it("gets every block's energy without a render", async () => {
    const { result, speakAt, renders } = harness();
    act(() => result.current.start('live'));
    await settle();
    const heard: [number, number][] = [];
    result.current.levels.subscribe((energy, samples) => heard.push([energy, samples]));
    const before = renders();

    act(() => {
      for (let block = 0; block < 300; block += 1) speakAt(0.5);
    });

    expect(heard).toHaveLength(300);
    expect(heard[0]).toEqual([32, 128]);
    expect(renders()).toBe(before);
  });

  it('counts whole seconds, and stops the clock at Done', async () => {
    const { result } = harness();
    act(() => result.current.start('live'));
    await settle();

    await settle(2_500);
    expect(result.current.elapsedMs).toBe(2_000);

    act(() => result.current.done());
    await settle(5_000);
    expect(result.current.elapsedMs).toBe(2_000);
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

  it('asks for no partials, A57, only when told to', async () => {
    const opened = async (partials: boolean): Promise<URL> => {
      const socket = new FakeSocket();
      let address = '';
      const stt = new SttSocket({
        onEvent: () => undefined,
        partials,
        factory: (url) => {
          address = url;
          return socket;
        },
      });
      const connect = stt.start();
      socket.onopen?.({});
      await connect;
      return new URL(address);
    };

    expect((await opened(false)).search).toBe('?partials=0');
    expect((await opened(false)).pathname).toBe('/ws/stt');
    expect((await opened(true)).search).toBe('');
  });
});
