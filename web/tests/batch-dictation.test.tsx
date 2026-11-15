/**
 * A57 — dictation through a gateway whose speech backend transcribes the
 * recording afterwards (`stt.streaming: false`). The composer asks for no
 * partials, says it is recording, leaves the field exactly as it was while the
 * person speaks, and lands the whole transcript in one step after Done. A hand
 * on the field hands the dictation to the job store, as leaving the session
 * does: typing gets Send back at once, and the words follow what was typed.
 * A gateway that streams, or is too old to say, dictates live as
 * before. The real dictation controller and job store run here, over a fake
 * gateway and a silent microphone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { claudeAgent } from '../mock/fixtures';
import { Composer } from '../src/features/chat/Composer';
import type { Session } from '../src/protocol/types';
import { useDictationJobs } from '../src/stores/dictationJobs';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';

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

beforeEach(() => {
  gateway.length = 0;
  // A test that ends while listening hands its dictation to the job store.
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
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

function setup(sttStreaming: boolean | undefined, polishEnabled = false) {
  render(
    <Composer
      session={session}
      agent={claudeAgent}
      deviceOnline
      queue={[]}
      question={null}
      sttEnabled
      polishEnabled={polishEnabled}
      polishContext={() => []}
      {...(sttStreaming === undefined ? {} : { sttStreaming })}
      onSend={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />,
  );
  return {
    user: userEvent.setup(),
    field: () => screen.getByLabelText(strings.composer.placeholder) as HTMLTextAreaElement,
  };
}

const mic = () => screen.getByRole('button', { name: strings.composer.micStart });
const send = () => screen.queryByRole('button', { name: strings.composer.send });
/** The spinner in Send's slot while the words are on their way. */
const waiting = () => screen.queryByRole('status', { name: strings.voice.finishing });

/** The socket of the dictation under way, once it is listening. */
async function listening(user: ReturnType<typeof userEvent.setup>): Promise<SpeechSocket> {
  await user.click(mic());
  await waitFor(() => expect(screen.getByRole('button', { name: strings.voice.done })).toBeEnabled());
  return gateway[0]!;
}

describe('a batch dictation in the composer', () => {
  it('asks for no partials and says it records, to be transcribed at Done', async () => {
    const { user } = setup(false);
    const socket = await listening(user);

    expect(new URL(socket.url).search).toBe('?partials=0');
    expect(screen.getByText(strings.voice.recording)).toBeInTheDocument();
    expect(screen.queryByText(strings.voice.transcribing)).toBeNull();
  });

  it('leaves the draft as it was while listening, then lands the transcript whole', async () => {
    const { user, field } = setup(false);
    await user.click(field());
    await user.keyboard('after lunch');
    const socket = await listening(user);

    // Nothing is pretended while the person speaks.
    act(() => socket.deliver({ type: 'stt.partial', text: 'not yet' }));
    expect(field()).toHaveValue('after lunch');

    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));
    expect(field()).toHaveValue('after lunch');
    expect(screen.getByText(strings.voice.finishing)).toBeInTheDocument();

    act(() => socket.deliver({ type: 'stt.final', text: 'run the auth suite' }));

    expect(field()).toHaveValue('after lunch run the auth suite');
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();
  });

  it('hands the dictation on when the field is reached for, and gives Send back to typing', async () => {
    const { user, field } = setup(false);
    const socket = await listening(user);

    // The tap is Done: the words are on their way, and the slot waits for them.
    await user.click(field());
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));
    expect(socket.said('stt.cancel')).toBe(false);
    expect(screen.queryByRole('button', { name: strings.voice.done })).toBeNull();
    expect(waiting()).not.toBeNull();
    expect(screen.getByText(strings.voice.finishing)).toBeInTheDocument();

    // Typing does not wait for them.
    await user.keyboard('typed first');
    expect(waiting()).toBeNull();
    expect(send()).toBeEnabled();
    expect(screen.getByText(strings.voice.finishing)).toBeInTheDocument();

    act(() => socket.deliver({ type: 'stt.final', text: 'then the dictation' }));

    expect(field()).toHaveValue('typed first then the dictation');
    expect(screen.queryByText(strings.voice.finishing)).toBeNull();
    expect(send()).toBeEnabled();
  });

  it('gives Send back to typing after Done too, and appends the words when they come', async () => {
    const { user, field } = setup(false);
    const socket = await listening(user);
    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));
    expect(waiting()).not.toBeNull();

    await user.click(field());
    await user.keyboard('while waiting');
    expect(waiting()).toBeNull();
    expect(send()).toBeEnabled();

    act(() => socket.deliver({ type: 'stt.final', text: 'the words' }));
    expect(field()).toHaveValue('while waiting the words');
  });

  it('offers no new dictation until the words on their way have landed', async () => {
    const { user } = setup(false);
    const socket = await listening(user);
    await user.click(screen.getByLabelText(strings.composer.placeholder));
    await user.keyboard('typed');

    expect(mic()).toBeDisabled();
    act(() => socket.deliver({ type: 'stt.final', text: 'the words' }));
    expect(mic()).toBeEnabled();
  });

  it("says the gateway's error when Done is answered with one, keeping the draft", async () => {
    const { user, field } = setup(false);
    await user.click(field());
    await user.keyboard('as typed');
    const socket = await listening(user);
    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));

    // With no partials, a backend that fails is first heard at stt.stop.
    act(() => socket.deliver({ type: 'stt.error', code: 'internal', message: 'backend down' }));

    const notice = screen.getByRole('alert');
    expect(within(notice).getByText('backend down')).toBeInTheDocument();
    expect(field()).toHaveValue('as typed');
  });

  it('writes nothing when nothing was said', async () => {
    const { user, field } = setup(false);
    await user.click(field());
    await user.keyboard('as typed');
    const socket = await listening(user);

    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));
    act(() => socket.deliver({ type: 'stt.final', text: '' }));

    expect(field()).toHaveValue('as typed');
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();
  });
});

describe('polish after a batch dictation', () => {
  it('asks the model about the words once they land after Done', async () => {
    useSettings.setState({ polishEnabled: true, polishModel: 'gpt-4.1-mini' });
    const asked: { path: string; text: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body ?? '{}')) as { text?: unknown };
        asked.push({ path: new URL(String(input), 'http://gateway.test').pathname, text: body.text });
        return new Promise<Response>(() => undefined);
      }),
    );
    const { user, field } = setup(false, true);
    const socket = await listening(user);
    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));

    act(() => socket.deliver({ type: 'stt.final', text: 'um run the the suite' }));

    expect(field()).toHaveValue('um run the the suite');
    await waitFor(() =>
      expect(asked).toContainEqual({ path: '/api/polish', text: 'um run the the suite' }),
    );
    expect(screen.getByText(strings.voice.polishing)).toBeInTheDocument();
  });
});

/**
 * A57: Chinese and Japanese are typed through an input method, which
 * holds marked text while it composes. Words that arrive then wait for the
 * composition to end, committed or cancelled, and land after it.
 */
describe('words that arrive while an input method composes', () => {
  it('wait for the composition in a field that was taken over, then follow it', async () => {
    const { user, field } = setup(false);
    const socket = await listening(user);
    await user.click(field());
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));

    fireEvent.compositionStart(field());
    fireEvent.change(field(), { target: { value: 'ni' } });
    act(() => socket.deliver({ type: 'stt.final', text: 'the words' }));
    expect(field()).toHaveValue('ni');

    fireEvent.change(field(), { target: { value: '你' } });
    fireEvent.compositionEnd(field());

    await waitFor(() => expect(field()).toHaveValue('你 the words'));
  });

  it('wait in the open composer too, after Done', async () => {
    const { user, field } = setup(false);
    const socket = await listening(user);
    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    await waitFor(() => expect(socket.said('stt.stop')).toBe(true));

    fireEvent.compositionStart(field());
    act(() => socket.deliver({ type: 'stt.final', text: 'the words' }));
    expect(field()).toHaveValue('');

    fireEvent.compositionEnd(field());
    await waitFor(() => expect(field()).toHaveValue('the words'));
  });
});

describe('a gateway that streams, or does not say', () => {
  it.each([true, undefined])('dictates live as before (streaming: %s)', async (streaming) => {
    const { user, field } = setup(streaming);
    const socket = await listening(user);

    expect(new URL(socket.url).search).toBe('');
    expect(screen.getByText(strings.voice.transcribing)).toBeInTheDocument();

    act(() => socket.deliver({ type: 'stt.partial', text: 'live words' }));
    expect(field()).toHaveValue('live words');
  });
});
