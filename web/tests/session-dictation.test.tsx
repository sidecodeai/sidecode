/**
 * `docs/DESIGN.md` § "The composer" — "A dictation running at the switch ends,
 * its words staying in the session it was spoken for."
 *
 * The controller tears down on unmount, so what proves the rule is that the
 * composer is remounted when the route's session changes: the microphone of
 * the conversation being left is closed, and its transcript is still in that
 * conversation's draft when it is opened again.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { useChat } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useDrafts } from '../src/stores/drafts';
import { useOutbox } from '../src/stores/outbox';
import { keyOf, useSessions } from '../src/stores/sessions';
import { strings } from '../src/strings';
import { codexAgent, devices as deviceFixtures } from '../mock/fixtures';
import type * as ReactModule from 'react';
import type { Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const voice = vi.hoisted(() => ({
  tornDown: 0,
  say: null as null | ((text: string) => void),
}));

vi.mock('../src/features/voice/useVoice', async () => {
  const { useEffect, useState } = await vi.importActual<typeof ReactModule>('react');
  return {
    useVoice: (options: { onTranscript: (text: string, isFinal: boolean) => void }) => {
      const [state, setState] = useState('idle');
      voice.say = (text) => options.onTranscript(text, false);
      // The real controller's only teardown: the microphone and every socket
      // close when the hook unmounts.
      useEffect(() => () => void (voice.tornDown += 1), []);
      return {
        state,
        level: 0.4,
        elapsedMs: 3_000,
        error: null,
        start: () => setState('listening'),
        done: () => setState('finishing'),
        cancel: () => setState('idle'),
        dismissError: () => setState('idle'),
      };
    },
  };
});

const { ChatPage } = await import('../src/features/chat/ChatPage');

const nativeScrollTo = HTMLElement.prototype.scrollTo;

beforeAll(() => {
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement) {
    // jsdom does not scroll.
  } as HTMLElement['scrollTo'];
});

afterAll(() => {
  HTMLElement.prototype.scrollTo = nativeScrollTo;
});

const device = { ...deviceFixtures[0]!, device_id: 'dev-mac', agents: [codexAgent] };

const session = (id: string, title: string): Session => ({
  session_id: id,
  device_id: 'dev-mac',
  agent: 'codex',
  title,
  cwd: '/Users/me/dev/gateway',
  git: null,
  state: 'idle',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'gpt-5.4-codex',
  permission_mode: 'on-request',
  effort: 'medium',
  speed: null,
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
});

const sessionA = session('ses-a', 'Session A');
const sessionB = session('ses-b', 'Session B');
const keyA = keyOf(sessionA);
const keyB = keyOf(sessionB);

const field = (): HTMLTextAreaElement =>
  screen.getByLabelText(strings.composer.placeholder) as HTMLTextAreaElement;

beforeEach(() => {
  voice.tornDown = 0;
  voice.say = null;
  rpcMock.mockReset();
  rpcMock.mockResolvedValue({});
  useSessions.setState({
    sessions: { [keyA]: sessionA, [keyB]: sessionB },
    loaded: true,
    agentFilter: null,
  });
  useDevices.setState({ devices: [device], loaded: true, updateErrors: {} });
  useChat.setState({ sessions: {} });
  useOutbox.setState({ pending: {} });
  useDrafts.getState().reset();
  useConnection.setState({ status: 'closed', stt: { enabled: true } });
});

describe('a dictation running at the switch', () => {
  it('ends with the session it was spoken for, and leaves its words there', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={[`/sessions/dev-mac/${sessionA.session_id}`]}>
        <Routes>
          <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    act(() => voice.say?.('run the auth suite'));
    expect(field()).toHaveValue('run the auth suite');

    await user.click(screen.getByRole('button', { name: /Session B/ }));

    expect(voice.tornDown).toBe(1);
    expect(field()).toHaveValue('');
    expect(useDrafts.getState().drafts[keyA]?.text).toBe('run the auth suite');
    expect(useDrafts.getState().drafts[keyB]).toBeUndefined();
  });
});
