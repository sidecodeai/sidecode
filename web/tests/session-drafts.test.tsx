/**
 * `docs/DESIGN.md` § "The composer" — **A draft belongs to its session**.
 *
 * The chat route keeps one `ChatPage` across a session switch, so everything
 * the composer held in its own state used to follow the reader into the next
 * conversation: the words, the files, and a running dictation. This suite
 * drives the real page through the sidebar, the way a reader switches.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { ChatPage } from '../src/features/chat/ChatPage';
import { useChat } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useDrafts } from '../src/stores/drafts';
import { useOutbox } from '../src/stores/outbox';
import { keyOf, useSessions } from '../src/stores/sessions';
import { emptyTimeline } from '../src/stores/timeline';
import { strings } from '../src/strings';
import { codexAgent, devices as deviceFixtures } from '../mock/fixtures';
import type { QuestionEvent, Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const nativeScrollTo = HTMLElement.prototype.scrollTo;

beforeAll(() => {
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement) {
    // jsdom does not scroll; this suite is about the composer.
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

/** The chat route, started on A, with B one sidebar click away. */
function renderChat() {
  render(
    <MemoryRouter initialEntries={[`/sessions/dev-mac/${sessionA.session_id}`]}>
      <Routes>
        <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const openFromSidebar = async (title: string): Promise<void> => {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: new RegExp(title) }));
};

beforeEach(() => {
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
  // Never 'open', so the page does not subscribe over the mocked socket.
  useConnection.setState({ status: 'closed' });
});

describe('a draft belongs to its session', () => {
  it('leaves A’s words with A and shows B’s own', async () => {
    const user = userEvent.setup();
    renderChat();

    await user.click(field());
    await user.keyboard('secret for session A');
    expect(field()).toHaveValue('secret for session A');

    await openFromSidebar('Session B');
    expect(field()).toHaveValue('');

    await user.click(field());
    await user.keyboard('for B alone');
    await openFromSidebar('Session A');
    expect(field()).toHaveValue('secret for session A');

    await openFromSidebar('Session B');
    expect(field()).toHaveValue('for B alone');
  });

  it('never offers A’s words as the answer to a question waiting in B', async () => {
    const user = userEvent.setup();
    const question: QuestionEvent = {
      seq: 4,
      ts: 1,
      kind: 'question',
      block_id: 'q-block',
      request_id: 'req-1',
      status: 'pending',
      questions: [{ id: 'q1', prompt: 'Which branch?', options: [], multi: false, allow_text: true }],
    } as QuestionEvent;
    useChat.setState({
      sessions: {
        [keyB]: {
          key: keyB,
          deviceId: 'dev-mac',
          sessionId: 'ses-b',
          timeline: {
            ...emptyTimeline(),
            order: ['q-block'],
            items: {
              'q-block': {
                key: 'q-block',
                seq: 4,
                ts: 1,
                parentBlockId: null,
                event: question,
              },
            },
            lastSeq: 4,
          },
          todos: [],
          queue: [],
          usage: null,
          ready: true,
          historyLoading: false,
          historyHasMore: false,
          error: null,
          closedSeq: null,
        },
      },
    });
    renderChat();

    await user.click(field());
    await user.keyboard('secret for session A');
    await openFromSidebar('Session B');

    // The composer is B's answer field, and it has nothing to submit.
    expect(field()).toHaveValue('');
    expect(field()).toHaveAttribute('placeholder', strings.composer.placeholderAnswer);
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeDisabled();
  });

  it('keeps A’s attachments out of B and hands them back to A', async () => {
    renderChat();
    useDrafts
      .getState()
      .addAttachments(keyA, [
        { name: 'trace.log', mime: 'text/plain', size: 4, data_base64: 'AAAA' },
      ]);

    await waitFor(() => expect(screen.getByText('trace.log')).toBeInTheDocument());
    await openFromSidebar('Session B');
    expect(screen.queryByText('trace.log')).not.toBeInTheDocument();

    await openFromSidebar('Session A');
    expect(screen.getByText('trace.log')).toBeInTheDocument();
  });
});
