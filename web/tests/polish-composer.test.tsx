/**
 * A29 — a dictation polished in the composer: the words land first, the status
 * line says "Polishing…", the answer replaces the dictated span alone, and the
 * note under the field can put the words back. `docs/DESIGN.md` § "Polishing
 * what you dictated". The pure rules are in polish.test.ts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { claudeAgent } from '../mock/fixtures';
import { strings } from '../src/strings';
import { useSettings } from '../src/stores/settings';
import type * as ReactModule from 'react';
import type { PolishContextItem, Session } from '../src/protocol/types';

const voice = vi.hoisted(() => ({
  publish: null as null | ((text: string, isFinal: boolean) => void),
}));

vi.mock('../src/features/voice/useVoice', async () => {
  const { useState } = await vi.importActual<typeof ReactModule>('react');
  return {
    useVoice: (options: { onTranscript: (text: string, isFinal: boolean) => void }) => {
      const [state, setState] = useState('idle');
      // Like the controller: Done starts the wait for the backend's last word,
      // and that word, in the same tick, is what starts the polish.
      voice.publish = (text, isFinal) => {
        options.onTranscript(text, isFinal);
        if (isFinal) setState('idle');
      };
      return {
        state,
        level: 0.4,
        elapsedMs: 12_000,
        error: null,
        start: () => setState('listening'),
        done: () => setState('finishing'),
        cancel: () => setState('idle'),
        dismissError: () => setState('idle'),
      };
    },
  };
});

const { Composer } = await import('../src/features/chat/Composer');

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

const context: PolishContextItem[] = [
  { role: 'user', text: 'Make the session status dot pulse while a turn is running.' },
  { role: 'assistant', text: 'Done: the dot breathes while the state is running.' },
];

interface Call {
  path: string;
  body: Record<string, unknown>;
}

const calls: Call[] = [];
/** The request the composer is waiting on, answered when a test decides. */
let answer: { ok: (text: string) => void; fail: () => void } | null = null;

const reply = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

beforeEach(() => {
  voice.publish = null;
  calls.length = 0;
  answer = null;
  useSettings.setState({
    polishEnabled: true,
    polishModel: 'gpt-4.1-mini',
    polishStrength: 'strong',
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        path: new URL(String(input), 'http://gateway.test').pathname,
        body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
      });
      return new Promise<Response>((resolve) => {
        answer = {
          ok: (text) => resolve(reply(200, { text })),
          fail: () => resolve(reply(502, { error: { code: 'upstream', message: 'provider' } })),
        };
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  useSettings.setState({ polishEnabled: false, polishModel: '', polishStrength: 'moderate' });
});

function setup(polishEnabled = true, state: Session['state'] = 'idle') {
  const onSend = vi.fn().mockResolvedValue(undefined);
  render(
    <Composer
      session={{ ...session, state }}
      agent={claudeAgent}
      deviceOnline
      queue={[]}
      sttEnabled
      polishEnabled={polishEnabled}
      polishContext={() => context}
      question={null}
      onSend={onSend}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />,
  );
  return { onSend, field: () => screen.getByLabelText(strings.composer.placeholder) };
}

const say = (text: string, isFinal = false) => act(() => voice.publish?.(text, isFinal));

/** Mic, a partial, Done, then the transcript the gateway settled on. */
const dictate = async (user: ReturnType<typeof userEvent.setup>, text: string) => {
  await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
  say(text);
  await user.click(screen.getByRole('button', { name: strings.voice.done }));
  say(text, true);
};

const settle = async (text: string) => {
  await act(async () => {
    answer?.ok(text);
    await Promise.resolve();
  });
};

const polishCalls = () => calls.filter((call) => call.path === '/api/polish');

/** The spinner standing in Send's slot, or null when Send itself is there. */
const working = () => document.querySelector('.working-pill');
const sendButton = () => document.querySelector('.send-btn');

describe('polishing a dictation', () => {
  it('sends the dictated words, the choices and the conversation, and says it is working', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await user.click(field());
    await user.keyboard('after lunch');

    await dictate(user, 'um run the the auth suite');

    // The words are in the field the instant dictation ends, as they are today.
    expect(field()).toHaveValue('after lunch um run the the auth suite');
    expect(screen.getByText(strings.voice.polishing)).toBeInTheDocument();

    await waitFor(() => expect(polishCalls()).toHaveLength(1));
    expect(polishCalls()[0]?.body).toEqual({
      // The dictated span only: what was typed before it is not the model's.
      text: 'um run the the auth suite',
      model: 'gpt-4.1-mini',
      strength: 'strong',
      // A44: the gateway transcribed the words and detected their language.
      language: 'auto',
      context,
    });

    await settle('Run the auth suite.');
    expect(field()).toHaveValue('after lunch Run the auth suite.');
    expect(screen.queryByText(strings.voice.polishing)).toBeNull();
  });

  it('offers Undo under the field, which gives the dictated words back', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await dictate(user, 'um run the the auth suite');
    await settle('Run the auth suite.');

    expect(screen.getByText(strings.voice.polished)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: strings.voice.undo }));

    expect(field()).toHaveValue('um run the the auth suite');
    expect(screen.queryByText(strings.voice.polished)).toBeNull();
  });

  it('takes the note away on the next edit', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await dictate(user, 'um run the the auth suite');
    await settle('Run the auth suite.');

    await user.click(field());
    await user.keyboard(' twice');

    expect(field()).toHaveValue('Run the auth suite. twice');
    expect(screen.queryByText(strings.voice.polished)).toBeNull();
  });

  it('keeps the words and says so when the request fails', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await dictate(user, 'um run the the auth suite');

    await act(async () => {
      answer?.fail();
      await Promise.resolve();
    });

    expect(field()).toHaveValue('um run the the auth suite');
    expect(screen.getByText(strings.voice.polishFailed)).toBeInTheDocument();
    expect(screen.queryByText(strings.voice.polishing)).toBeNull();
    // The dictated words are sendable as they are, so Send comes back with them.
    expect(working()).toBeNull();
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();
  });

  it('offers no Send while the model is writing, and Enter waits with it', async () => {
    const user = userEvent.setup();
    const { field, onSend } = setup();
    await dictate(user, 'um run the the auth suite');

    // The slot Done gave way to is still a spinner: the words are on their way.
    const pill = screen.getByRole('status', { name: strings.voice.polishing });
    expect(pill.className).toContain('working-pill');
    expect(pill.querySelector('.spinner')).not.toBeNull();
    expect(sendButton()).toBeNull();

    // Enter is Send, so a keystroke sends no more than the button would.
    await user.click(field());
    await user.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();

    await settle('Run the auth suite.');

    // The field holds what will be sent, so the slot is Send again.
    expect(working()).toBeNull();
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();
  });

  it('draws no menu beside the spinner, and brings it back with Send', async () => {
    const user = userEvent.setup();
    setup(true, 'running');
    await dictate(user, 'um run the the auth suite');

    expect(working()).not.toBeNull();
    expect(screen.queryByRole('button', { name: strings.composer.sendOptions })).toBeNull();

    await settle('Run the auth suite.');

    expect(screen.getByRole('button', { name: strings.composer.sendOptions })).toBeInTheDocument();
    expect(sendButton()).not.toBeNull();
  });

  it('gives Send back to the person who types over the wait', async () => {
    const user = userEvent.setup();
    const { field, onSend } = setup();
    await dictate(user, 'um run the the auth suite');
    expect(sendButton()).toBeNull();

    await user.click(field());
    await user.keyboard(' twice');

    // The person's words won: the request is dropped and the slot is Send.
    expect(working()).toBeNull();
    expect(screen.queryByText(strings.voice.polishing)).toBeNull();
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();

    await settle('Run the auth suite.');
    expect(field()).toHaveValue('um run the the auth suite twice');

    await user.click(screen.getByRole('button', { name: strings.composer.send }));
    expect(onSend).toHaveBeenCalledWith('um run the the auth suite twice', [], 'auto');
  });

  it('polishes nothing when this gateway has no polish model', async () => {
    const user = userEvent.setup();
    const { field } = setup(false);
    await dictate(user, 'um run the the auth suite');

    expect(field()).toHaveValue('um run the the auth suite');
    expect(polishCalls()).toHaveLength(0);
    expect(screen.queryByText(strings.voice.polishing)).toBeNull();
    // Nothing is on its way, so Send is offered the moment dictation ends.
    expect(working()).toBeNull();
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();
  });

  it('polishes nothing when the reader has chosen no model', async () => {
    const user = userEvent.setup();
    useSettings.setState({ polishModel: '' });
    const { field } = setup();
    await dictate(user, 'um run the the auth suite');

    expect(field()).toHaveValue('um run the the auth suite');
    expect(polishCalls()).toHaveLength(0);
  });
});
