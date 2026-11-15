/**
 * Background dictation: a dictation that has had Done tapped outlives the
 * composer it was spoken into. The job store takes it over at the moment the
 * composer goes away, writes the transcript into the session's draft before any
 * polish, merges the polish answer without ever overwriting newer typing, and
 * has no send path. `docs/DESIGN.md` § "Dictation keeps going when you leave".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';
import { claudeAgent } from '../mock/fixtures';
import { Composer } from '../src/features/chat/Composer';
import { DictationRowLabel } from '../src/features/voice/DictationRowLabel';
import type { DictationMode } from '../src/features/voice/dictationMode';
import type { SttSocketLike } from '../src/features/voice/sttSocket';
import { useVoice, type RecorderHandlers, type VoiceRecorder } from '../src/features/voice/useVoice';
import { strings } from '../src/strings';
import { useDictationJobs, type PolishPlan } from '../src/stores/dictationJobs';
import { useDrafts } from '../src/stores/drafts';
import type { Session } from '../src/protocol/types';

const KEY = 'dev-1/ses-1';
const plan: PolishPlan = { model: 'managed', strength: 'moderate', context: [] };

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

/** A voice hook whose composer hands over to the job store when it unmounts. */
function composerThatLeaves(options: {
  handsOver: boolean;
  plan: PolishPlan | null;
  mode?: DictationMode;
}) {
  const sockets: FakeSocket[] = [];
  const own: { text: string; isFinal: boolean }[] = [];
  const factory = (): SttSocketLike => {
    const socket = new FakeSocket();
    sockets.push(socket);
    void Promise.resolve().then(() => socket.onopen?.({}));
    return socket;
  };
  const recorder: VoiceRecorder = {
    start: () => Promise.resolve(true),
    stop: () => Promise.resolve(),
  };
  const recorderFactory = (_: RecorderHandlers): VoiceRecorder => recorder;
  const base = 'after lunch';
  let applied = base;
  useDrafts.getState().setText(KEY, base);
  const view = renderHook(() =>
    useVoice({
      enabled: true,
      factory,
      recorderFactory,
      onTranscript: (text, isFinal) => {
        applied = `${base} ${text}`;
        useDrafts.getState().setText(KEY, applied);
        own.push({ text, isFinal });
      },
      onDetach: () =>
        options.handsOver
          ? useDictationJobs
              .getState()
              .handoff(KEY, {
                base,
                applied,
                plan: options.plan,
                mode: options.mode ?? 'live',
                editTs: null,
              })
          : null,
    }),
  );
  return { ...view, sockets, own };
}

const settle = async (ms = 0): Promise<void> => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

interface PolishCall {
  body: { text: string };
  signal: AbortSignal | null | undefined;
  answer: (text: string) => void;
  refuse: () => void;
}
const polishCalls: PolishCall[] = [];

const reply = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

const draft = (): string => useDrafts.getState().drafts[KEY]?.text ?? '';
const job = () => useDictationJobs.getState().jobs[KEY];

beforeEach(() => {
  vi.useFakeTimers();
  polishCalls.length = 0;
  useDrafts.getState().reset();
  useDictationJobs.getState().reset();
  vi.stubGlobal(
    'fetch',
    vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      return new Promise<Response>((resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        polishCalls.push({
          body: JSON.parse(String(init?.body ?? '{}')) as { text: string },
          signal: init?.signal,
          answer: (text) => resolve(reply(200, { text })),
          refuse: () =>
            resolve(reply(402, { error: { code: 'quota_exceeded', message: 'quota' } })),
        });
      });
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('a dictation handed over when the composer goes away', () => {
  it('treats a dictation still listening as Done and lets its sockets finish', async () => {
    const { result, sockets, unmount, own } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'run the suite' }));
    expect(draft()).toBe('after lunch run the suite');

    unmount();
    await settle();

    // The live words left the draft and the socket was asked for its last word
    // instead of being dropped.
    expect(draft()).toBe('after lunch');
    expect(sockets[0]!.toldToTranscribe).toBe(true);
    expect(sockets[0]!.toldToDrop).toBe(false);
    expect(job()?.phase).toBe('finishing');

    const before = own.length;
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'run the suite on CI' }));
    await settle();
    // The unmounted composer hears nothing more; the store does.
    expect(own).toHaveLength(before);
    expect(draft()).toBe('after lunch run the suite on CI');
  });

  it('keeps a dictation that was already finishing', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    act(() => result.current.done());
    await settle();

    unmount();
    expect(sockets[0]!.toldToDrop).toBe(false);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'done talking' }));
    await settle();
    expect(draft()).toBe('after lunch done talking');
  });

  it('ends the run as before when nobody takes it over', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: false, plan: null });
    act(() => result.current.start('live'));
    await settle();

    unmount();
    expect(sockets[0]!.toldToDrop).toBe(true);
    expect(job()).toBeUndefined();
  });

  it('writes the plain transcript before polish, then polishes into the draft', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    unmount();
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'um run the the suite' }));
    await settle();

    // Persisted first: a polish that never answers still leaves the words.
    expect(draft()).toBe('after lunch um run the the suite');
    expect(job()?.phase).toBe('polishing');
    expect(polishCalls).toHaveLength(1);
    expect(polishCalls[0]!.body.text).toBe('um run the the suite');

    await act(async () => polishCalls[0]!.answer('Run the suite.'));
    expect(draft()).toBe('after lunch Run the suite.');
    expect(job()).toMatchObject({ phase: 'ready', unseen: true });
  });

  it('appends the transcript without polish when polish is off', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan: null });
    act(() => result.current.start('live'));
    await settle();
    unmount();
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'plain words' }));
    await settle();

    expect(draft()).toBe('after lunch plain words');
    expect(polishCalls).toHaveLength(0);
    expect(job()).toMatchObject({ phase: 'ready', unseen: true });
  });

  it('lets text typed meanwhile win: the result is appended, never overwrites', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    unmount();
    act(() => useDrafts.getState().setText(KEY, 'after lunch, and also'));
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'the transcript' }));
    await settle();
    expect(draft()).toBe('after lunch, and also the transcript');

    // Typing after the transcript landed wins over the polish answer too.
    act(() => useDrafts.getState().setText(KEY, 'after lunch, and also the transcript!!'));
    await act(async () => polishCalls[0]!.answer('Polished.'));
    expect(draft()).toBe('after lunch, and also the transcript!!');
    expect(job()?.phase).toBe('ready');
  });

  it('keeps the transcript and the failure note when polish fails', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    unmount();
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'keep me' }));
    await settle();
    await act(async () => polishCalls[0]!.refuse());

    expect(draft()).toBe('after lunch keep me');
    expect(job()).toMatchObject({
      phase: 'ready',
      unseen: true,
      failed: true,
    });
  });

  it('ends interrupted, partial words kept, when the gateway never answers', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    act(() => sockets[0]!.deliver({ type: 'stt.partial', text: 'half a sentence' }));
    unmount();
    await settle(30_000);

    expect(draft()).toBe('after lunch half a sentence');
    expect(job()).toMatchObject({ phase: 'interrupted', unseen: true });
    expect(polishCalls).toHaveLength(0);
  });

  it('runs several jobs in parallel, one per session', async () => {
    const store = useDictationJobs.getState();
    const fresh = { base: '', applied: '', plan, mode: 'live', editTs: null } as const;
    const sinkA = store.handoff('dev-1/a', fresh);
    const sinkB = store.handoff('dev-1/b', fresh);
    act(() => sinkA('first', true));
    act(() => sinkB('second', true));
    expect(polishCalls.map((call) => call.body.text)).toEqual(['first', 'second']);
    expect(useDictationJobs.getState().jobs['dev-1/a']?.phase).toBe('polishing');
    expect(useDictationJobs.getState().jobs['dev-1/b']?.phase).toBe('polishing');
    await act(async () => polishCalls[1]!.answer('Second.'));
    expect(useDrafts.getState().drafts['dev-1/b']?.text).toBe('Second.');
    expect(useDrafts.getState().drafts['dev-1/a']?.text).toBe('first');
  });
});

/**
 * A57 — a batch dictation has written nothing into the draft when it is handed
 * over, so there are no live words to take out; the segments it held and the
 * last one's final land together, in one write, under the same rules.
 */
describe('a batch dictation handed over', () => {
  /** Every value the session's draft takes from here on. */
  const draftWrites = () => {
    const writes: string[] = [];
    const stop = useDrafts.subscribe((state) => writes.push(state.drafts[KEY]?.text ?? ''));
    return { writes, stop };
  };

  it('has no live words to take out, and lands every segment in one step', async () => {
    const { result, sockets, unmount, own } = composerThatLeaves({ handsOver: true, plan: null, mode: 'batch' });
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'first half' }));
    await settle();
    expect(own).toEqual([]);

    unmount();
    await settle();
    expect(draft()).toBe('after lunch');
    expect(sockets[1]!.toldToTranscribe).toBe(true);
    expect(sockets[1]!.toldToDrop).toBe(false);
    expect(job()?.phase).toBe('finishing');

    const { writes, stop } = draftWrites();
    act(() => sockets[1]!.deliver({ type: 'stt.final', text: 'second half' }));
    await settle();
    stop();

    expect(writes).toEqual(['after lunch first half second half']);
    expect(job()).toMatchObject({ phase: 'ready', unseen: true });
  });

  it('appends after words typed meanwhile, then polishes', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan, mode: 'batch' });
    act(() => result.current.start('batch'));
    await settle();
    unmount();
    act(() => useDrafts.getState().setText(KEY, 'after lunch, and also'));

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'um the transcript' }));
    await settle();

    expect(draft()).toBe('after lunch, and also um the transcript');
    expect(polishCalls[0]?.body.text).toBe('um the transcript');
  });

  it('ends interrupted with the held words when the gateway never answers', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan, mode: 'batch' });
    act(() => result.current.start('batch'));
    await settle();
    await settle(30_000);
    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'held words' }));
    unmount();

    await settle(30_000);

    expect(draft()).toBe('after lunch held words');
    expect(job()).toMatchObject({ phase: 'interrupted', unseen: true });
    expect(polishCalls).toHaveLength(0);
  });

  it('writes nothing when it held nothing and the gateway never answers', async () => {
    const { result, unmount } = composerThatLeaves({ handsOver: true, plan, mode: 'batch' });
    act(() => result.current.start('batch'));
    await settle();
    unmount();
    const { writes, stop } = draftWrites();

    await settle(30_000);
    stop();

    expect(writes).toEqual([]);
    expect(draft()).toBe('after lunch');
    expect(job()).toMatchObject({ phase: 'interrupted', unseen: true });
  });
});

/**
 * A57 — typing while a transcript is on its way. A batch dictation's words are
 * all still to come, so the person's typing never waits for them: Send's slot
 * is theirs again, and the words are appended after what they typed. A live
 * dictation's words are in the field already, and its job keeps the slot as
 * it always has.
 */
describe('typing while a transcript is on its way', () => {
  const handOver = (mode: DictationMode) =>
    useDictationJobs
      .getState()
      .handoff(KEY, {
        base: 'after lunch',
        applied: 'after lunch',
        plan: null,
        mode,
        editTs: null,
      });

  it("gives a batch job's slot back to the person, and still appends the transcript", () => {
    useDrafts.getState().setText(KEY, 'after lunch');
    const sink = handOver('batch');
    expect(job()).toMatchObject({ phase: 'finishing', typedOver: false });

    act(() => useDrafts.getState().setText(KEY, 'after lunch, typed'));
    act(() => useDictationJobs.getState().drop(KEY));
    expect(job()).toMatchObject({ phase: 'finishing', typedOver: true });

    act(() => sink('the transcript', true));
    expect(draft()).toBe('after lunch, typed the transcript');
    expect(job()).toMatchObject({ phase: 'ready', typedOver: false });
  });

  it('appends the held words, interrupted, when a typed-over transcript never comes', () => {
    useDrafts.getState().setText(KEY, 'after lunch');
    const sink = handOver('batch');
    act(() => useDrafts.getState().setText(KEY, 'after lunch, typed'));
    act(() => useDictationJobs.getState().drop(KEY));

    act(() => sink('held words', true, true));

    expect(draft()).toBe('after lunch, typed held words');
    expect(job()?.phase).toBe('interrupted');
  });

  it('keeps a live job holding the slot, as it always has', () => {
    useDrafts.getState().setText(KEY, 'after lunch');
    const sink = handOver('live');

    act(() => useDictationJobs.getState().drop(KEY));
    expect(job()).toMatchObject({ phase: 'finishing', typedOver: false });

    act(() => sink('the transcript', true));
    expect(job()?.phase).toBe('ready');
  });
});

describe('sign-out while a handed-over dictation is finishing', () => {
  it('closes its sockets and lets a late transcript write nothing', async () => {
    const { result, sockets, unmount } = composerThatLeaves({ handsOver: true, plan });
    act(() => result.current.start('live'));
    await settle();
    unmount();

    act(() => useDictationJobs.getState().reset());
    useDrafts.getState().reset();
    expect(sockets[0]!.toldToDrop).toBe(true);

    act(() => sockets[0]!.deliver({ type: 'stt.final', text: 'late words' }));
    await settle();
    expect(draft()).toBe('');
    expect(polishCalls).toHaveLength(0);
    expect(job()).toBeUndefined();
  });

  it('ignores a sink the store handed out before it was emptied', () => {
    const sink = useDictationJobs
      .getState()
      .handoff(KEY, { base: '', applied: '', plan, mode: 'live', editTs: null });
    useDictationJobs.getState().reset();
    act(() => sink('late', true));
    expect(draft()).toBe('');
    expect(polishCalls).toHaveLength(0);
  });
});

describe('a polish answer that arrives after its composer is gone', () => {
  const span = { base: 'hello', dictated: 'um world' };

  it('never overwrites text typed since, and lands through the store', async () => {
    useDrafts.getState().setText(KEY, 'hello um world');
    const view = render(
      <Composer
        session={session}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled
        question={null}
        onSend={vi.fn()}
        onAnswer={vi.fn()}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    act(() => useDictationJobs.getState().polish(KEY, span, plan, 'live'));
    view.unmount();

    act(() => useDrafts.getState().setText(KEY, 'hello um world, newer words'));
    await act(async () => polishCalls[0]!.answer('World.'));

    expect(draft()).toBe('hello um world, newer words');
  });

  it('still replaces the dictated span when nothing was typed', async () => {
    useDrafts.getState().setText(KEY, 'hello um world');
    act(() => useDictationJobs.getState().polish(KEY, span, plan, 'live'));
    await act(async () => polishCalls[0]!.answer('World.'));
    expect(draft()).toBe('hello World.');
    expect(job()?.note).toEqual({ span, text: 'World.' });
  });

  it('is aborted when the field moves on, and its answer is ignored', async () => {
    useDrafts.getState().setText(KEY, 'hello um world');
    act(() => useDictationJobs.getState().polish(KEY, span, plan, 'live'));
    act(() => useDictationJobs.getState().drop(KEY));

    expect(polishCalls[0]!.signal?.aborted).toBe(true);
    expect(job()).toBeUndefined();
    expect(draft()).toBe('hello um world');
  });

  it('is aborted by sign-out', async () => {
    act(() => useDictationJobs.getState().polish(KEY, span, plan, 'live'));
    act(() => useDictationJobs.getState().reset());
    expect(polishCalls[0]!.signal?.aborted).toBe(true);
    expect(job()).toBeUndefined();
  });
});

describe('the session row label', () => {
  const label = () => {
    const view = render(<DictationRowLabel sessionKey={KEY} />);
    return view.container.textContent;
  };

  it('names the running state, then the unseen result, and clears on open', () => {
    expect(label()).toBe('');
    const sink = useDictationJobs
      .getState()
      .handoff(KEY, { base: '', applied: '', plan: null, mode: 'live', editTs: null });

    const finishing = render(<DictationRowLabel sessionKey={KEY} />);
    expect(finishing.container.textContent).toBe('Transcribing…');
    finishing.unmount();

    act(() => sink('words', true));
    const ready = render(<DictationRowLabel sessionKey={KEY} />);
    expect(ready.container.textContent).toBe(strings.voice.rowReady);
    expect(strings.voice.rowReady).toBe('Draft ready');

    act(() => useDictationJobs.getState().open(KEY));
    expect(ready.container.textContent).toBe('');
    useDictationJobs.getState().leave(KEY);
  });

  it('says Dictation interrupted until the session is opened', () => {
    const sink = useDictationJobs
      .getState()
      .handoff(KEY, { base: '', applied: '', plan, mode: 'live', editTs: null });
    act(() => sink('half', true, true));
    expect(label()).toBe('Dictation interrupted');
  });
});

describe('reopening the session', () => {
  it('shows the running job in the status line and the finished note under the field', async () => {
    useDrafts.getState().setText(KEY, 'hello um world');
    act(() =>
      useDictationJobs.getState().polish(KEY, { base: 'hello', dictated: 'um world' }, plan, 'live'),
    );
    render(
      <Composer
        session={session}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled
        question={null}
        onSend={vi.fn()}
        onAnswer={vi.fn()}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    expect(screen.getByText(strings.voice.polishing)).toBeInTheDocument();

    await act(async () => polishCalls[0]!.answer('World.'));
    expect(screen.getByText(strings.voice.polished)).toBeInTheDocument();
    expect(screen.getByLabelText(strings.composer.placeholder)).toHaveValue('hello World.');
  });
});

const session: Session = {
  session_id: 'ses-1',
  device_id: 'dev-1',
  agent: 'claude',
  title: 'Fix flaky auth test',
  cwd: '/Users/me/dev/gateway',
  git: null,
  state: 'idle',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'claude-sonnet-4-5',
  permission_mode: 'default',
  effort: 'high',
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
};
