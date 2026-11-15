/**
 * A57 — a batch dictation whose words never all arrive. The held text
 * lands, and the composer then says so with the transcription-failure notice:
 * never a silent landing, since the person saw no words while they spoke. The
 * notice has one source wherever the run is: the controller for the run on
 * screen, its job for a run handed to the job store, so a session left and
 * opened again still says it. Dismissing it forgets the job. The real
 * controller and job store run here over a fake gateway, on fake time.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { claudeAgent } from '../mock/fixtures';
import { Composer } from '../src/features/chat/Composer';
import type { Session } from '../src/protocol/types';
import { useDictationJobs } from '../src/stores/dictationJobs';
import { useDrafts } from '../src/stores/drafts';
import { sessionKey } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';

const session: Session = {
  session_id: 'ses-t',
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
const KEY = sessionKey(session.device_id, session.session_id);

/** Every `/ws/stt` the page dials, which accepts at once and answers when told. */
const gateway: SpeechSocket[] = [];

class SpeechSocket {
  binaryType = '';
  readyState = 0;
  sent: (string | ArrayBuffer)[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;

  constructor(readonly url: string) {
    gateway.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.({});
    });
  }

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
  }

  deliver(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

/** Just enough of an audio graph for the recorder's ScriptProcessor path. */
class SilentAudioContext {
  sampleRate = 48_000;
  state = 'running';
  destination = {};
  resume = () => Promise.resolve();
  close = () => Promise.resolve();
  createMediaStreamSource = () => ({ connect: () => undefined, disconnect: () => undefined });
  createScriptProcessor = () => ({
    connect: () => undefined,
    disconnect: () => undefined,
    onaudioprocess: null,
  });
  createGain = () => ({ gain: { value: 1 }, connect: () => undefined });
}

beforeEach(() => {
  vi.useFakeTimers();
  gateway.length = 0;
  useDictationJobs.getState().reset();
  useSettings.setState({ language: 'en', polishEnabled: false });
  vi.stubGlobal('WebSocket', SpeechSocket);
  vi.stubGlobal('AudioContext', SilentAudioContext);
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getTracks: () => [] }) },
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

const composer = () => (
  <Composer
    session={session}
    agent={claudeAgent}
    deviceOnline
    queue={[]}
    question={null}
    sttEnabled
    sttStreaming={false}
    onSend={vi.fn().mockResolvedValue(undefined)}
    onAnswer={vi.fn().mockResolvedValue(undefined)}
    onSetOption={vi.fn()}
    onRemoveQueued={vi.fn()}
    onTakeover={vi.fn()}
  />
);

const run = async (ms = 0): Promise<void> => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

const draft = () => useDrafts.getState().drafts[KEY]?.text ?? '';
const notices = () => screen.queryAllByRole('alert');
const job = () => useDictationJobs.getState().jobs[KEY];

/** Listening, with a first segment cut off and its words held. */
async function holding() {
  useDrafts.getState().setText(KEY, 'typed before');
  const view = render(composer());
  fireEvent.click(screen.getByRole('button', { name: strings.composer.micStart }));
  await run(10);
  await run(30_000);
  expect(gateway).toHaveLength(2);
  act(() => gateway[0]!.deliver({ type: 'stt.final', text: 'held words' }));
  return view;
}

describe('a batch final that never comes', () => {
  it('lands the held text in the open composer, then says the transcription failed', async () => {
    await holding();
    fireEvent.click(screen.getByRole('button', { name: strings.voice.done }));
    await run(10);

    await run(30_000);

    expect(draft()).toBe('typed before held words');
    expect(notices()).toHaveLength(1);
    expect(notices()[0]).toHaveTextContent(strings.voice.failed);
  });

  it('says it once, through the job, after the field was taken over', async () => {
    await holding();
    fireEvent.pointerDown(screen.getByLabelText(strings.composer.placeholder));
    await run(10);
    expect(job()?.phase).toBe('finishing');

    await run(30_000);

    expect(draft()).toBe('typed before held words');
    expect(job()?.phase).toBe('interrupted');
    expect(notices()).toHaveLength(1);
    expect(notices()[0]).toHaveTextContent(strings.voice.failed);
  });

  it('says it in the session opened again after a switch, until it is dismissed', async () => {
    const first = await holding();
    first.unmount();
    await run(10);
    render(composer());

    await run(30_000);

    expect(draft()).toBe('typed before held words');
    expect(notices()).toHaveLength(1);
    expect(notices()[0]).toHaveTextContent(strings.voice.failed);

    fireEvent.click(within(notices()[0]!).getByRole('button', { name: strings.common.dismiss }));
    expect(notices()).toHaveLength(0);
    expect(job()).toBeUndefined();
  });

  it('says it when the last segment closes without its final after a switch', async () => {
    const first = await holding();
    first.unmount();
    await run(10);
    render(composer());

    act(() => gateway[1]!.onclose?.({}));
    await run(10);

    expect(draft()).toBe('typed before held words');
    expect(notices()).toHaveLength(1);
    expect(notices()[0]).toHaveTextContent(strings.voice.failed);
  });

  it("says the gateway's own message after a switch", async () => {
    const first = await holding();
    first.unmount();
    await run(10);
    render(composer());

    act(() => gateway[1]!.deliver({ type: 'stt.error', code: 'internal', message: 'backend down' }));
    await run(10);

    expect(draft()).toBe('typed before held words');
    expect(notices()).toHaveLength(1);
    expect(notices()[0]).toHaveTextContent('backend down');
  });
});
