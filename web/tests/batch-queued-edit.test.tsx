/**
 * A57 — where batch words land while a queued message is being edited.
 * They belong to the draft the mic was pressed on. A dictation begun on the
 * draft, whose words arrive while the field edits a queued message, or while
 * that edit is on its way back into the line, puts them in the draft set
 * aside, unpolished, and they come back with it. A dictation begun inside an
 * edit lands in that edit while it is open, and in the draft it gave back to
 * once it has ended. The real controller and job store run here, over a fake
 * gateway and a silent microphone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { claudeAgent } from '../mock/fixtures';
import { Composer } from '../src/features/chat/Composer';
import type { Send } from '../src/features/chat/useQueuedEdit';
import type { QueuedMessage, Session } from '../src/protocol/types';
import { useDictationJobs } from '../src/stores/dictationJobs';
import { useDrafts } from '../src/stores/drafts';
import { sessionKey } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';

const session: Session = {
  session_id: 'ses-q',
  device_id: 'dev-1',
  agent: 'claude',
  title: 'Fix flaky auth test',
  cwd: '/Users/me/dev/gateway',
  git: null,
  state: 'running',
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
  queued: 1,
};
const KEY = sessionKey(session.device_id, session.session_id);
const QUEUED: QueuedMessage = { id: 'q-1', text: 'Queued message.', ts: 1_000 };

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

  said(type: string): boolean {
    return this.sent.some((item) => typeof item === 'string' && item.includes(type));
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

/** Every `POST /api/polish`; none may concern words set aside. */
const polished: unknown[] = [];

beforeEach(() => {
  gateway.length = 0;
  polished.length = 0;
  useDictationJobs.getState().reset();
  useSettings.setState({ language: 'en', polishEnabled: true, polishModel: 'gpt-4.1-mini' });
  vi.stubGlobal('WebSocket', SpeechSocket);
  vi.stubGlobal('AudioContext', SilentAudioContext);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      polished.push(JSON.parse(String(init?.body ?? '{}')));
      return new Promise<Response>(() => undefined);
    }),
  );
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getTracks: () => [] }) },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, 'mediaDevices');
  useSettings.setState({ polishEnabled: false, polishModel: '' });
});

/** A composer whose sends wait until the test lets them through. */
function setup() {
  let release: () => void = () => undefined;
  const onSend = vi.fn<Send>(
    () =>
      new Promise<void>((resolve) => {
        release = resolve;
      }),
  );
  render(
    <Composer
      session={session}
      agent={claudeAgent}
      deviceOnline
      queue={[QUEUED]}
      question={null}
      sttEnabled
      sttStreaming={false}
      polishEnabled
      polishContext={() => []}
      onSend={onSend}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeQueued={vi.fn<(id: string) => Promise<void>>(() => Promise.resolve())}
      onTakeover={vi.fn()}
    />,
  );
  return {
    user: userEvent.setup(),
    /** The gateway accepts the edited message back into the line. */
    accept: async () => {
      await act(async () => {
        release();
        await Promise.resolve();
      });
      await waitFor(() => expect(editing()).toBeNull());
    },
  };
}

const field = () => screen.getByLabelText(strings.composer.placeholder) as HTMLTextAreaElement;
const editing = () => useDrafts.getState().drafts[KEY]?.editing ?? null;

async function type(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.click(field());
  await user.keyboard(text);
}

async function listen(user: ReturnType<typeof userEvent.setup>): Promise<SpeechSocket> {
  const before = gateway.length;
  await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
  await waitFor(() => expect(screen.getByRole('button', { name: strings.voice.done })).toBeEnabled());
  return gateway[before]!;
}

async function editQueued(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: strings.composer.upNextCount(1) }));
  await user.click(screen.getByRole('button', { name: QUEUED.text }));
  await waitFor(() => expect(field()).toHaveValue(QUEUED.text));
}

async function done(user: ReturnType<typeof userEvent.setup>, socket: SpeechSocket) {
  await user.click(screen.getByRole('button', { name: strings.voice.done }));
  await waitFor(() => expect(socket.said('stt.stop')).toBe(true));
}

describe('a dictation begun on the draft', () => {
  it('puts its words in the draft set aside while a queued message is edited', async () => {
    const { user, accept } = setup();
    await type(user, 'my draft');
    const socket = await listen(user);
    await editQueued(user);
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));

    act(() => socket.deliver({ type: 'stt.final', text: 'dictated words' }));

    expect(field()).toHaveValue(QUEUED.text);
    expect(editing()?.aside.text).toBe('my draft dictated words');
    expect(polished).toEqual([]);

    await user.click(screen.getByRole('button', { name: strings.common.cancel }));
    await accept();
    expect(field()).toHaveValue('my draft dictated words');
  });

  it('puts them there while the edit is on its way back into the line', async () => {
    const { user, accept } = setup();
    await type(user, 'my draft');
    const socket = await listen(user);
    await editQueued(user);
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));
    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    expect(editing()?.sending).toBe(true);

    act(() => socket.deliver({ type: 'stt.final', text: 'dictated words' }));
    await accept();

    expect(field()).toHaveValue('my draft dictated words');
    expect(polished).toEqual([]);
  });
});

describe('a dictation begun inside an edit', () => {
  it('lands in that edit while it is open, and is polished there', async () => {
    const { user } = setup();
    await type(user, 'my draft');
    await editQueued(user);
    const socket = await listen(user);
    await done(user, socket);

    act(() => socket.deliver({ type: 'stt.final', text: 'dictated words' }));

    expect(field()).toHaveValue(`${QUEUED.text} dictated words`);
    expect(editing()?.aside.text).toBe('my draft');
    await waitFor(() => expect(polished).toHaveLength(1));
  });

  it('lands in the draft that came back once the edit has ended', async () => {
    const { user, accept } = setup();
    await type(user, 'my draft');
    await editQueued(user);
    const socket = await listen(user);
    await done(user, socket);
    await user.click(screen.getByRole('button', { name: strings.common.cancel }));
    await accept();
    expect(field()).toHaveValue('my draft');

    act(() => socket.deliver({ type: 'stt.final', text: 'dictated words' }));

    expect(field()).toHaveValue('my draft dictated words');
  });

  it('waits aside while that edit is on its way back, then comes back with the draft', async () => {
    const { user, accept } = setup();
    await type(user, 'my draft');
    await editQueued(user);
    const socket = await listen(user);
    await done(user, socket);
    await user.click(screen.getByRole('button', { name: strings.common.cancel }));
    expect(editing()?.sending).toBe(true);

    act(() => socket.deliver({ type: 'stt.final', text: 'dictated words' }));
    expect(field()).toHaveValue(QUEUED.text);
    await accept();

    expect(field()).toHaveValue('my draft dictated words');
  });
});
