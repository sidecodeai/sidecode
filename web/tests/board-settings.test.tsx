/**
 * A62 — the Task board row of Settings › General › Reading: the account's switch
 * with the note that says what turning it on sends. It shows what the account
 * stored. Turning it on shows the disclosure before anything is written;
 * turning it off writes at once, and stays possible on a gateway that keeps no
 * boards, where the switch cannot be turned on. The row is not drawn on a
 * gateway too old to say, and the account's value is never written up from
 * this browser's: a `hello` that leaves it out is answered with the A41 fields
 * alone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useAuth } from '../src/stores/auth';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import { patchFor, SYNCED_KEYS } from '../src/stores/preferenceFields';
import { setPreferencesHeld } from '../src/stores/preferenceWrite';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import type { HelloFrame, PushFrame } from '../src/protocol/frames';
import type { Preferences } from '../src/protocol/types';
import type * as WsModule from '../src/lib/ws';
import { renderSettings, rowTitles } from './settingsScreen';

const { captured } = vi.hoisted(() => ({
  captured: { onFrame: null as ((frame: PushFrame) => void) | null },
}));

vi.mock('../src/lib/ws', async (importOriginal) => ({
  ...(await importOriginal<typeof WsModule>()),
  socketUrl: () => 'ws://test/ws/app',
  AppSocket: class {
    constructor(options: { onFrame: (frame: PushFrame) => void }) {
      captured.onFrame = options.onFrame;
    }
    start(): void {
      /* nothing to dial in a test */
    }
    stop(): void {
      /* nothing to close */
    }
  },
}));

const s = strings.settings;
const c = s.taskBoardConsent;

/** Every `PATCH /api/preferences` body, in the order sent. */
const writes: Record<string, unknown>[] = [];
/** What the gateway answers a write with; it holds what it was sent. */
let answer: (body: Record<string, unknown>) => Response;

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const holding = (preferences: Preferences) => usePreferences.setState({ preferences });

beforeEach(() => {
  writes.length = 0;
  answer = (body) => json({ preferences: { ...usePreferences.getState().preferences, ...body } });
  useSettings.setState({ language: 'en' });
  useAuth.setState({ status: 'signed-in', username: 'dev', role: 'admin' });
  useConnection.setState({ board: { enabled: true } });
  holding({ resume_after_limit: false });
  setPreferencesHeld(true);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'http://gateway.test').pathname;
      if (path !== '/api/preferences' || init?.method !== 'PATCH') return json({});
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      writes.push(body);
      return answer(body);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  setPreferencesHeld(false);
  useConnection.setState({ board: null });
  usePreferences.setState({ preferences: undefined });
});

const toggle = () => screen.getByRole('switch', { name: s.taskBoard });
const row = () => toggle().closest('.settings-row') as HTMLElement;
const readingRows = () => rowTitles(document.querySelector('.settings-group') as HTMLElement);

describe('the Task board row', () => {
  it('closes Reading, after Language and Detail, with the note that says what it sends', () => {
    renderSettings('/settings/general');

    expect(readingRows()).toEqual([s.language, s.timelineDetail, s.taskBoard]);
    expect(within(row()).getByText(s.taskBoardNote)).toBeInTheDocument();
    expect(toggle()).toHaveAttribute('aria-checked', 'false');
    expect(toggle()).toBeEnabled();
  });

  it('is not drawn on a gateway too old to keep boards', () => {
    useConnection.setState({ board: null });
    renderSettings('/settings/general');

    expect(readingRows()).toEqual([s.language, s.timelineDetail]);
  });

  it('cannot be turned on, and says why, on a gateway that keeps none', async () => {
    useConnection.setState({ board: { enabled: false } });
    const user = userEvent.setup();
    renderSettings('/settings/general');

    expect(toggle()).toBeDisabled();
    expect(within(row()).getByText(s.taskBoardUnsupported)).toBeInTheDocument();
    await user.click(row());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(writes).toEqual([]);
  });

  it('can still be turned off on a gateway that keeps none, which then cannot turn it on again', async () => {
    useConnection.setState({ board: { enabled: false } });
    holding({ resume_after_limit: false, task_board: true });
    const user = userEvent.setup();
    renderSettings('/settings/general');

    expect(toggle()).toHaveAttribute('aria-checked', 'true');
    expect(toggle()).toBeEnabled();
    expect(within(row()).getByText(s.taskBoardUnsupported)).toBeInTheDocument();
    await user.click(toggle());

    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'));
    expect(writes).toEqual([{ task_board: false }]);
    expect(toggle()).toBeDisabled();
  });

  it('reads the account’s value', () => {
    holding({ resume_after_limit: false, task_board: true });
    renderSettings('/settings/general');

    expect(toggle()).toHaveAttribute('aria-checked', 'true');
  });

  it('says 任务看板 in 中文', () => {
    useSettings.setState({ language: 'zh-Hans' });
    renderSettings('/settings/general');

    expect(screen.getByRole('switch', { name: '任务看板' })).toBeInTheDocument();
    expect(screen.getByText(/^为每个对话列出你提出的要求/)).toBeInTheDocument();
  });
});

describe('turning it on and off', () => {
  it('shows the disclosure before anything is written, and Not now writes nothing', async () => {
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(toggle());

    const dialog = screen.getByRole('dialog', { name: c.title });
    expect(within(dialog).getAllByRole('listitem').map((li) => li.textContent)).toEqual([c.sends, c.keeps, c.caution]);
    expect(writes).toEqual([]);
    await user.click(within(dialog).getByRole('button', { name: c.notNow }));
    expect(writes).toEqual([]);
    expect(toggle()).toHaveAttribute('aria-checked', 'false');
  });

  it('writes task_board: true on Agree and continue, and is on once the gateway answers', async () => {
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(toggle());
    await user.click(screen.getByRole('button', { name: c.agree }));

    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'));
    expect(writes).toEqual([{ task_board: true }]);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('turns off at once, with no question', async () => {
    holding({ resume_after_limit: false, task_board: true });
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(toggle());

    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'));
    expect(writes).toEqual([{ task_board: false }]);
  });

  it('says a refusal in place of the note, and stays where it was', async () => {
    holding({ resume_after_limit: false, task_board: true });
    answer = () => json({ error: { code: 'bad_request', message: 'no' } }, 400);
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(toggle());

    expect(await within(row()).findByText(strings.errors.setFailed)).toBeInTheDocument();
    expect(toggle()).toHaveAttribute('aria-checked', 'true');
  });
});

describe('the account’s value is the person’s alone', () => {
  const hello = (preferences: Preferences): HelloFrame => ({
    type: 'hello',
    protocol: 1,
    gateway_version: '1.21.0',
    user: { username: 'dev', role: 'admin' },
    devices: [],
    sessions: [],
    stt: { enabled: false, languages: ['auto'] },
    preferences,
    board: { enabled: true },
    server_time: Date.now(),
  });

  it('is never among the fields this app writes up from its own values', () => {
    expect(SYNCED_KEYS).not.toContain('task_board');
    expect(patchFor(SYNCED_KEYS, useSettings.getState())).not.toHaveProperty('task_board');
  });

  it('is not written up when a hello leaves it out, while the unset A41 fields are', async () => {
    useConnection.getState().disconnect();
    useConnection.getState().connect(() => undefined);
    if (!captured.onFrame) throw new Error('the connection store opened no socket');

    captured.onFrame(hello({ resume_after_limit: false, language: 'en' }));

    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).not.toHaveProperty('task_board');
    expect(Object.keys(writes[0] ?? {})).toEqual(
      expect.arrayContaining(['polish_enabled', 'polish_model', 'polish_strength', 'timeline_detail']),
    );
    useConnection.getState().disconnect();
  });
});
