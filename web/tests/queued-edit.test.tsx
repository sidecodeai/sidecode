/**
 * A43 — an edit of a queued message end to end: the page takes the entry out
 * with `session.queue_remove`, the field holds its words, and the edited words
 * go back as `session.send {mode: "queue", queue_ts}` — through the real chat,
 * outbox and drafts stores, so a Retry and a session switch are covered too.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { ChatPage } from '../src/features/chat/ChatPage';
import { queueRemoveText } from '../src/lib/errors';
import { RequestError } from '../src/lib/ws';
import { useChat, type ChatSession } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useDrafts } from '../src/stores/drafts';
import { useOutbox } from '../src/stores/outbox';
import { keyOf, useSessions } from '../src/stores/sessions';
import { emptyTimeline } from '../src/stores/timeline';
import { strings } from '../src/strings';
import { codexAgent, devices as deviceFixtures } from '../mock/fixtures';
import type { QueuedMessage, Session } from '../src/protocol/types';

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
  state: 'running',
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
  queued: 3,
});

const sessionA = session('ses-a', 'Session A');
const sessionB = session('ses-b', 'Session B');
const keyA = keyOf(sessionA);

const QUEUE: QueuedMessage[] = [
  { id: 'q-1', text: 'Then run the full test suite.', ts: 1_000 },
  { id: 'q-2', text: 'These two screenshots show the drawer.', ts: 2_000, attachments: 2 },
  { id: 'q-3', text: 'After that, bump Vite.', ts: 3_000 },
];

const chatOf = (s: Session, queue: QueuedMessage[]): ChatSession => ({
  key: keyOf(s),
  deviceId: s.device_id,
  sessionId: s.session_id,
  timeline: emptyTimeline(),
  todos: [],
  queue,
  usage: null,
  ready: true,
  historyLoading: false,
  historyHasMore: false,
  error: null,
  closedSeq: null,
});

/** What the device publishes after a change to the queue (A6). */
const snapshot = (seq: number, pending: QueuedMessage[]) =>
  act(() => {
    useChat.getState().ingestEvent('ses-a', { seq, ts: 1, kind: 'queue', pending }, 'dev-mac');
  });

const field = (): HTMLTextAreaElement =>
  screen.getByLabelText(strings.composer.placeholder) as HTMLTextAreaElement;
const strip = () => screen.queryByText(strings.composer.editingQueued);
const sends = () => rpcMock.mock.calls.filter((call) => call[0] === 'session.send');

function renderChat() {
  render(
    <MemoryRouter initialEntries={[`/sessions/dev-mac/${sessionA.session_id}`]}>
      <Routes>
        <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function editFirst(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: strings.composer.upNextCount(3) }));
  await user.click(screen.getByRole('button', { name: QUEUE[0]!.text }));
  await waitFor(() => expect(field()).toHaveValue(QUEUE[0]!.text));
}

beforeEach(() => {
  rpcMock.mockReset();
  rpcMock.mockImplementation((type: string) =>
    Promise.resolve(type === 'session.send' ? { accepted: 'queued', queued_id: 'q-new' } : {}),
  );
  useSessions.setState({
    sessions: { [keyA]: sessionA, [keyOf(sessionB)]: sessionB },
    loaded: true,
    agentFilter: null,
  });
  useDevices.setState({ devices: [device], loaded: true, updateErrors: {} });
  useChat.setState({ sessions: { [keyA]: chatOf(sessionA, QUEUE) } });
  useOutbox.setState({ pending: {} });
  // Never 'open', so the page does not subscribe over the mocked socket.
  useConnection.setState({ status: 'closed' });
});

describe('the draft store holds the edit', () => {
  const FILE = { name: 'trace.log', mime: 'text/plain', size: 4, data_base64: 'AAAA' };

  it('sets the draft aside, keeps an emptied field editing, and gives the draft back', () => {
    const drafts = useDrafts.getState();
    drafts.setText('k', 'half a thought');
    drafts.addAttachments('k', [FILE]);

    drafts.beginEdit('k', { ts: 7, text: 'queued words' });
    expect(useDrafts.getState().drafts.k).toEqual({
      text: 'queued words',
      attachments: [],
      editing: {
        ts: 7,
        original: 'queued words',
        aside: { text: 'half a thought', attachments: [FILE] },
        sending: false,
      },
    });

    // An empty field is still an edit: the words that were queued are not lost.
    drafts.setText('k', '');
    expect(useDrafts.getState().drafts.k?.editing?.original).toBe('queued words');

    drafts.setEditSending('k', true);
    expect(useDrafts.getState().drafts.k?.editing?.sending).toBe(true);

    drafts.endEdit('k');
    expect(useDrafts.getState().drafts.k).toEqual({
      text: 'half a thought',
      attachments: [FILE],
      editing: null,
    });
  });

  it('forgets a draft that had nothing aside once the edit ends', () => {
    const drafts = useDrafts.getState();
    drafts.beginEdit('k', { ts: 7, text: 'queued words' });
    drafts.endEdit('k');
    expect(useDrafts.getState().drafts).toEqual({});
  });
});

describe('a Remove through the page', () => {
  it('says the message was already sent when the device took it first, never Not found', async () => {
    const user = userEvent.setup();
    rpcMock.mockImplementation((type: string) =>
      type === 'session.queue_remove'
        ? Promise.reject(new RequestError({ code: 'not_found', message: 'that message is not queued' }))
        : Promise.resolve({}),
    );
    renderChat();

    await user.click(screen.getByRole('button', { name: strings.composer.upNextCount(3) }));
    const [firstRemove] = screen.getAllByRole('button', { name: strings.chat.queuedRemove });
    await user.click(firstRemove!);
    expect(rpcMock).toHaveBeenCalledWith('session.queue_remove', {
      session_id: 'ses-a',
      queued_id: 'q-1',
    });
    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent(strings.composer.alreadySent);
    expect(banner).not.toHaveTextContent(strings.errors.notFound);
  });

  it('keeps its own words for every other refusal', () => {
    expect(queueRemoveText(new RequestError({ code: 'device_offline', message: '' }))).toBe(
      strings.errors.deviceOffline,
    );
    expect(queueRemoveText(new RequestError({ code: 'internal', message: '' }))).toBe(
      strings.errors.queueRemoveFailed,
    );
  });
});

describe('an edit through the page', () => {
  it('takes the entry out, then puts the edited words back in its place', async () => {
    const user = userEvent.setup();
    renderChat();
    expect(screen.getByText('Codex is working · your message will steer the turn')).toBeInTheDocument();

    await editFirst(user);
    expect(rpcMock).toHaveBeenCalledWith('session.queue_remove', {
      session_id: 'ses-a',
      queued_id: 'q-1',
    });
    snapshot(1, QUEUE.slice(1));
    expect(screen.getByRole('button', { name: strings.composer.upNextCount(2) })).toBeInTheDocument();
    // The field's message goes back into the line, and the line says so.
    expect(screen.getByText('Codex is working · your message will be queued')).toBeInTheDocument();

    await user.keyboard(' And the linters.');
    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    await waitFor(() => expect(sends()).toHaveLength(1));
    expect(sends()[0]?.[1]).toEqual({
      session_id: 'ses-a',
      text: 'Then run the full test suite. And the linters.',
      mode: 'queue',
      queue_ts: 1_000,
    });

    await waitFor(() => expect(strip()).not.toBeInTheDocument());
    const requestId = (sends()[0]?.[2] as { id: string }).id;
    snapshot(2, [
      { id: requestId, text: 'Then run the full test suite. And the linters.', ts: 1_000 },
      ...QUEUE.slice(1),
    ]);
    expect(screen.getByRole('button', { name: strings.composer.upNextCount(3) })).toBeInTheDocument();
    expect(screen.getByText('Codex is working · your message will steer the turn')).toBeInTheDocument();
  });

  it('says under the field that the message was already sent, and draws no banner', async () => {
    const user = userEvent.setup();
    rpcMock.mockImplementation((type: string) =>
      type === 'session.queue_remove'
        ? Promise.reject(new RequestError({ code: 'not_found', message: 'that message is not queued' }))
        : Promise.resolve({}),
    );
    renderChat();

    await user.click(screen.getByRole('button', { name: strings.composer.upNextCount(3) }));
    await user.click(screen.getByRole('button', { name: QUEUE[0]!.text }));
    expect(await screen.findByText(strings.composer.alreadySent)).toBeInTheDocument();
    expect(document.querySelector('.action-error')).toBeNull();
    expect(field()).toHaveValue('');
    expect(strip()).not.toBeInTheDocument();
  });

  it('keeps the entry’s ts for a Retry of a put-back whose delivery is unknown', async () => {
    const user = userEvent.setup();
    let firstSend = true;
    rpcMock.mockImplementation((type: string) => {
      if (type !== 'session.send') return Promise.resolve({});
      if (!firstSend) return Promise.resolve({ accepted: 'queued', queued_id: 'q-new' });
      firstSend = false;
      return Promise.reject(new RequestError({ code: 'timeout', message: '' }));
    });
    renderChat();
    await editFirst(user);

    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    // Uncertain is not refused: the edit ends, and the page offers the Retry.
    await waitFor(() => expect(strip()).not.toBeInTheDocument());
    await user.click(await screen.findByRole('button', { name: strings.common.retry }));

    await waitFor(() => expect(sends()).toHaveLength(2));
    const [first, second] = sends();
    expect((second?.[2] as { id: string }).id).toBe((first?.[2] as { id: string }).id);
    expect(second?.[1]).toMatchObject({ mode: 'queue', queue_ts: 1_000 });
  });

  it('keeps the edit with its session when another conversation is opened', async () => {
    const user = userEvent.setup();
    renderChat();
    await editFirst(user);
    await user.keyboard(' Edited.');

    await user.click(screen.getByRole('button', { name: /Session B/ }));
    expect(field()).toHaveValue('');
    expect(strip()).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Session A/ }));
    expect(field()).toHaveValue(`${QUEUE[0]!.text} Edited.`);
    expect(strip()).toBeInTheDocument();
  });
});
