/**
 * A65, A66 — the composer's AI permissions. The first press of the microphone asks for dictation's
 * permission before anything is recorded: Agree starts that dictation, Not now records nothing and
 * the next press asks again. With the account's polish on and no polish permission in this
 * browser, a dictation's words land first and the permission is asked before they are sent to be
 * polished: Agree polishes them, Not now keeps them as dictated and the next dictation does not ask
 * until the service named changes. Each names the service the gateway names. A press while the
 * voice assistant's call holds the microphone says so, with Hang up, and records nothing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { claudeAgent } from '../mock/fixtures';
import { useLive } from '../src/features/live/store';
import { forgetConsents, grantConsent, storedConsent } from '../src/stores/aiConsent';
import { useAuth } from '../src/stores/auth';
import { useConnection } from '../src/stores/connection';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import type * as ReactModule from 'react';
import type * as LevelFeedModule from '../src/features/voice/levelFeed';
import type { Session } from '../src/protocol/types';

const voice = vi.hoisted(() => ({
  publish: null as null | ((text: string, isFinal: boolean) => void),
  starts: 0,
}));
const live = vi.hoisted(() => ({ hangUp: vi.fn() }));

vi.mock('../src/features/live/liveCall', () => ({ hangUp: live.hangUp }));

vi.mock('../src/features/voice/useVoice', async () => {
  const { useState } = await vi.importActual<typeof ReactModule>('react');
  const { LevelFeed } = await vi.importActual<typeof LevelFeedModule>('../src/features/voice/levelFeed');
  return {
    useVoice: (options: { onTranscript: (text: string, isFinal: boolean) => void }) => {
      const [state, setState] = useState('idle');
      const [levels] = useState(() => new LevelFeed());
      voice.publish = (text, isFinal) => {
        options.onTranscript(text, isFinal);
        if (isFinal) setState('idle');
      };
      return {
        state,
        mode: 'live',
        levels,
        elapsedMs: 0,
        error: null,
        // The microphone and `/ws/stt` are this, in the real controller.
        start: () => {
          voice.starts += 1;
          setState('listening');
        },
        done: () => setState('finishing'),
        takeover: () => {
          setState('idle');
          return false;
        },
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

const ME = 'dee';
const c = strings.consent;
const polishRequests: unknown[] = [];

function setup() {
  render(
    <MemoryRouter>
      <Composer
        session={session}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled
        polishEnabled
        question={null}
        onSend={vi.fn().mockResolvedValue(undefined)}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />
    </MemoryRouter>,
  );
  return { field: () => screen.getByLabelText(strings.composer.placeholder) };
}

const mic = () => screen.getByRole('button', { name: strings.composer.micStart });
const listening = () => document.querySelector('.voice-controls') !== null;
const say = (text: string) => act(() => voice.publish?.(text, true));

/** The gateway's hello named these services. */
const named = (name: string) =>
  useConnection.setState({
    helloAt: Date.now(),
    stt: { enabled: true, provider_name: name },
    polish: { enabled: true, provider_name: name },
  });

beforeEach(() => {
  voice.publish = null;
  voice.starts = 0;
  live.hangUp.mockReset();
  polishRequests.length = 0;
  forgetConsents(ME);
  useAuth.setState({ username: ME });
  useSettings.setState({ polishEnabled: false, polishModel: 'gpt-4.1-mini' });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/api/polish')) polishRequests.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({ text: 'Run the auth suite.' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  useConnection.setState({ helloAt: null, stt: { enabled: false }, polish: { enabled: false } });
  useLive.getState().reset();
  useAuth.setState({ username: null });
});

describe('dictation’s permission at the microphone', () => {
  it('asks at the first press, before anything is recorded, in the unnamed words of a gateway that names nobody', async () => {
    const user = userEvent.setup();
    setup();

    await user.click(mic());

    const dialog = screen.getByRole('dialog', { name: c.dictation.title });
    expect(within(dialog).getByText(c.dictation.lead)).toBeInTheDocument();
    expect(within(dialog).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      c.dictation.sends,
      c.dictation.goesTo(null),
      c.dictation.draft,
    ]);
    expect(within(dialog).getByText(c.dictation.withdraw)).toBeInTheDocument();
    expect(within(dialog).queryByRole('link')).toBeNull();
    expect(voice.starts).toBe(0);
    expect(listening()).toBe(false);
  });

  it('records nothing on Not now, and asks again at the next press', async () => {
    const user = userEvent.setup();
    setup();

    await user.click(mic());
    await user.click(screen.getByRole('button', { name: c.notNow }));
    expect(voice.starts).toBe(0);
    expect(storedConsent(ME, 'dictation')).toBeNull();

    await user.click(mic());
    expect(screen.getByRole('dialog', { name: c.dictation.title })).toBeInTheDocument();
  });

  it('starts that dictation on Agree and continue, and the next one straight away', async () => {
    const user = userEvent.setup();
    setup();

    await user.click(mic());
    await user.click(screen.getByRole('button', { name: c.agree }));

    expect(voice.starts).toBe(1);
    expect(listening()).toBe(true);
    expect(storedConsent(ME, 'dictation')).toBe('1|');
    say('run the auth suite');
    await user.click(mic());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(voice.starts).toBe(2);
  });

  it('names the service the gateway names', async () => {
    const user = userEvent.setup();
    named('Example AI');
    setup();

    await user.click(mic());

    const dialog = screen.getByRole('dialog', { name: c.dictation.title });
    expect(within(dialog).getByText(c.dictation.goesTo('Example AI'))).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: c.agree }));
    expect(storedConsent(ME, 'dictation')).toBe('1|Example AI');
  });

  it('asks again once the gateway names another service', async () => {
    const user = userEvent.setup();
    grantConsent(ME, 'dictation', null);
    named('OpenAI');
    setup();

    await user.click(mic());

    expect(screen.getByRole('dialog', { name: c.dictation.title })).toBeInTheDocument();
    expect(voice.starts).toBe(0);
  });
});

describe('the microphone held by a voice assistant call', () => {
  it('says so, with Hang up, records nothing and asks for nothing', async () => {
    const user = userEvent.setup();
    grantConsent(ME, 'dictation', null);
    act(() => useLive.getState().dispatch({ type: 'connecting' }));
    setup();

    await user.click(mic());

    const notice = screen.getByRole('alert');
    expect(notice).toHaveTextContent(strings.voice.callHoldsMic);
    expect(voice.starts).toBe(0);
    expect(screen.queryByRole('dialog')).toBeNull();

    await user.click(within(notice).getByRole('button', { name: 'Hang up' }));
    expect(live.hangUp).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('stays until it is closed, and the next dictation takes its place', async () => {
    const user = userEvent.setup();
    grantConsent(ME, 'dictation', null);
    act(() => useLive.getState().dispatch({ type: 'connecting' }));
    setup();

    await user.click(mic());
    await user.click(within(screen.getByRole('alert')).getByRole('button', { name: strings.common.close }));
    expect(screen.queryByRole('alert')).toBeNull();

    await user.click(mic());
    expect(screen.getByRole('alert')).toBeInTheDocument();
    act(() => useLive.getState().reset());
    await user.click(mic());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(voice.starts).toBe(1);
  });
});

describe('polish’s permission, with the account’s polish on and none in this browser', () => {
  beforeEach(() => {
    grantConsent(ME, 'dictation', null);
    useSettings.setState({ polishEnabled: true });
  });

  async function dictate(user: ReturnType<typeof userEvent.setup>, text: string) {
    await user.click(mic());
    say(text);
  }

  it('asks once the words are in the field, before anything is sent to be polished', async () => {
    const user = userEvent.setup();
    const { field } = setup();

    await dictate(user, 'um run the auth suite');

    expect(field()).toHaveValue('um run the auth suite');
    const dialog = screen.getByRole('dialog', { name: c.polish.title });
    expect(within(dialog).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      c.polish.sends,
      c.polish.goesTo(null),
      c.polish.draft,
    ]);
    expect(polishRequests).toEqual([]);
  });

  it('polishes that dictation on Agree and continue', async () => {
    const user = userEvent.setup();
    const { field } = setup();

    await dictate(user, 'um run the auth suite');
    await user.click(screen.getByRole('button', { name: c.agree }));

    await waitFor(() => expect(polishRequests).toHaveLength(1));
    expect(polishRequests[0]).toMatchObject({ text: 'um run the auth suite' });
    await waitFor(() => expect(field()).toHaveValue('Run the auth suite.'));
    expect(storedConsent(ME, 'polish')).toBe('1|');
  });

  it('keeps the words as dictated on Not now, and does not ask at the next dictation', async () => {
    const user = userEvent.setup();
    const { field } = setup();

    await dictate(user, 'um run the auth suite');
    await user.click(screen.getByRole('button', { name: c.notNow }));
    await user.clear(field());
    await dictate(user, 'and the billing suite');

    expect(field()).toHaveValue('and the billing suite');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(polishRequests).toEqual([]);
    expect(storedConsent(ME, 'polish')).toBeNull();
  });

  it('asks again at a dictation once the gateway names another service', async () => {
    const user = userEvent.setup();
    setup();

    await dictate(user, 'um run the auth suite');
    await user.click(screen.getByRole('button', { name: c.notNow }));
    grantConsent(ME, 'dictation', 'OpenAI');
    act(() => named('OpenAI'));
    await dictate(user, 'and the billing suite');

    const dialog = screen.getByRole('dialog', { name: c.polish.title });
    expect(within(dialog).getByText(c.polish.goesTo('OpenAI'))).toBeInTheDocument();
  });

  it('asks nothing while the account’s polish is off', async () => {
    const user = userEvent.setup();
    useSettings.setState({ polishEnabled: false });
    setup();

    await dictate(user, 'um run the auth suite');

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(polishRequests).toEqual([]);
  });
});
