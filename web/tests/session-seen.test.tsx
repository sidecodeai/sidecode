/**
 * A47 (PROTOCOL §6.2, `docs/DESIGN.md` § "A red dot for a session that stopped
 * and waits for you"): the browser sends `session.seen` for the conversation
 * the person has in front of them — open in a tab that is visible, in a window
 * that has the focus — when it opens, when the page comes to the front with it
 * open, and when a `session.updated` marks it while it is on screen. Only while
 * this tab's copy says `unseen`, never twice at once, and a failure says
 * nothing: the next occasion sends it again.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { ChatPage } from '../src/features/chat/ChatPage';
import { useChat } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useFront } from '../src/stores/front';
import { useOutbox } from '../src/stores/outbox';
import { keyOf, useSessions } from '../src/stores/sessions';
import { RequestError } from '../src/lib/ws';
import { claudeAgent, devices as deviceFixtures } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

// jsdom has no scrolling, and the timeline scrolls itself to the latest row on
// mount. Stubbed the way `ChatPage.test.tsx` stubs it.
const nativeScrollTo = HTMLElement.prototype.scrollTo;

beforeAll(() => {
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement) {
    // Nothing to scroll: this suite is about the mark, not the timeline.
  } as HTMLElement['scrollTo'];
});

afterAll(() => {
  HTMLElement.prototype.scrollTo = nativeScrollTo;
});

const device = { ...deviceFixtures[0]!, device_id: 'dev-mac', agents: [claudeAgent] };

const marked: Session = {
  session_id: 'ses-done',
  device_id: 'dev-mac',
  agent: 'claude',
  title: 'Tidy the parser',
  cwd: '/Users/me/dev/gateway',
  git: null,
  state: 'idle',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'claude-sonnet-4-5',
  permission_mode: 'acceptEdits',
  effort: 'high',
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
  unseen: true,
};

const other: Session = { ...marked, session_id: 'ses-other', title: 'Another one' };

/** The tab and the window, as the browser reports them. */
let visibility: DocumentVisibilityState = 'visible';
let focused = true;

beforeEach(() => {
  rpcMock.mockReset();
  rpcMock.mockResolvedValue({});
  visibility = 'visible';
  focused = true;
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused);
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
  useSessions.getState().reset();
  useSessions.setState({
    sessions: { [keyOf(marked)]: marked, [keyOf(other)]: other },
    loaded: true,
  });
  useDevices.setState({ devices: [device], loaded: true, updateErrors: {} });
  useChat.setState({ sessions: {} });
  useOutbox.setState({ pending: {} });
  useConnection.setState({ status: 'open' });
  useFront.setState({ key: null });
});

afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(document, 'visibilityState');
  useConnection.setState({ status: 'closed' });
});

function openChat(session: Session = marked) {
  return render(
    <MemoryRouter initialEntries={[`/sessions/${session.device_id}/${session.session_id}`]}>
      <Routes>
        <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Every `session.seen` the page sent, by the session it named. */
const seenSent = (): string[] =>
  rpcMock.mock.calls
    .filter(([type]) => type === 'session.seen')
    .map(([, params]) => (params as { session_id: string }).session_id);

function blur(): void {
  act(() => {
    focused = false;
    window.dispatchEvent(new Event('blur'));
  });
}

function focus(): void {
  act(() => {
    focused = true;
    window.dispatchEvent(new Event('focus'));
  });
}

function setVisibility(next: DocumentVisibilityState): void {
  act(() => {
    visibility = next;
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

/** What the gateway's `session.updated` does to this tab's copy. */
function update(session: Session): void {
  act(() => useSessions.getState().upsert(session));
}

/** Lets a request that already settled run its handlers to the end. */
const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));

const { unseen: _mark, ...cleared } = marked;

describe('opening a conversation', () => {
  it('tells the gateway the person has a marked one in front of them', () => {
    openChat();

    expect(seenSent()).toEqual(['ses-done']);
    expect(rpcMock).toHaveBeenCalledWith('session.seen', { session_id: 'ses-done' });
  });

  it('sends nothing for a conversation nobody marked', () => {
    update(cleared);
    openChat();

    expect(seenSent()).toEqual([]);
  });

  it('names the open conversation alone, whatever else is marked', () => {
    openChat(other);

    expect(seenSent()).toEqual(['ses-other']);
  });

  it('waits while the window is behind another, and sends when it comes to the front', () => {
    focused = false;
    openChat();
    expect(seenSent()).toEqual([]);

    focus();
    expect(seenSent()).toEqual(['ses-done']);
  });

  it('waits while the tab is in the background, and sends when it is shown', () => {
    visibility = 'hidden';
    openChat();
    expect(seenSent()).toEqual([]);

    setVisibility('visible');
    expect(seenSent()).toEqual(['ses-done']);
  });

  it('waits while the socket is down, and sends when it is back', () => {
    useConnection.setState({ status: 'reconnecting' });
    openChat();
    expect(seenSent()).toEqual([]);

    act(() => useConnection.setState({ status: 'open' }));
    expect(seenSent()).toEqual(['ses-done']);
  });
});

describe('a conversation already on screen', () => {
  it('is told about at once when a session.updated marks it', () => {
    update(cleared);
    openChat();
    expect(seenSent()).toEqual([]);

    // The turn ended while the person watched it.
    update({ ...cleared, updated_at: 3, unseen: true });
    expect(seenSent()).toEqual(['ses-done']);
  });

  it('is not told about while the page is out of sight, even when marked', () => {
    update(cleared);
    openChat();
    setVisibility('hidden');

    update({ ...cleared, unseen: true });
    expect(seenSent()).toEqual([]);

    setVisibility('visible');
    expect(seenSent()).toEqual(['ses-done']);
  });

  it('draws no dot on its own sidebar row while it is in front', () => {
    openChat();
    const ownRow = () => document.querySelector('.sidebar-item.active .unseen-dot');

    expect(document.querySelector('.sidebar-item.active')).not.toBeNull();
    expect(ownRow()).toBeNull();
    // Behind another window the mark is the gateway's to show, until it clears.
    blur();
    expect(ownRow()).not.toBeNull();
  });
});

/**
 * The conversation in front is the app's, not the page's: the sidebar leaves
 * its dot out and the app's icon does not count it (`tests/app-badge.test.tsx`).
 */
describe('the conversation in front', () => {
  it('is this one while the page is in front, and none once it is not', () => {
    const { unmount } = openChat();
    expect(useFront.getState().key).toBe(keyOf(marked));

    blur();
    expect(useFront.getState().key).toBeNull();
    focus();
    expect(useFront.getState().key).toBe(keyOf(marked));

    unmount();
    expect(useFront.getState().key).toBeNull();
  });

  it('is none while the tab is in the background', () => {
    visibility = 'hidden';
    openChat();

    expect(useFront.getState().key).toBeNull();
  });
});

describe('the request itself', () => {
  it('goes once while it is out, however often the page comes to the front', () => {
    rpcMock.mockReturnValue(new Promise(() => undefined));
    openChat();
    blur();
    focus();
    blur();
    focus();

    expect(seenSent()).toEqual(['ses-done']);
  });

  it('fails without a word, and the next occasion sends it again', async () => {
    rpcMock.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: '' }));
    openChat();
    await settle();

    expect(seenSent()).toEqual(['ses-done']);
    expect(screen.queryByRole('alert')).toBeNull();
    // Nothing is retried on its own; the window coming back to the front is
    // the next occasion.
    blur();
    focus();
    expect(seenSent()).toEqual(['ses-done', 'ses-done']);
  });

  it('leaves the mark to the gateway: the copy clears with its session.updated', () => {
    openChat();
    expect(useSessions.getState().sessions[keyOf(marked)]?.unseen).toBe(true);

    update(cleared);
    expect(useSessions.getState().sessions[keyOf(marked)]?.unseen).toBeUndefined();
    expect(seenSent()).toEqual(['ses-done']);
  });
});

describe('markSeen in the sessions store', () => {
  const markSeen = () => useSessions.getState().markSeen('dev-mac', 'ses-done');

  it('sends only while the copy says unseen', () => {
    update(cleared);
    markSeen();
    expect(seenSent()).toEqual([]);

    update(marked);
    markSeen();
    expect(seenSent()).toEqual(['ses-done']);
  });

  it('sends nothing for a session it does not hold', () => {
    useSessions.getState().markSeen('dev-mac', 'ses-unknown');

    expect(rpcMock).not.toHaveBeenCalled();
  });

  it('sends again once the last request has settled', async () => {
    markSeen();
    await settle();
    markSeen();

    expect(seenSent()).toEqual(['ses-done', 'ses-done']);
  });

  it('forgets a request in flight when the account signs out', () => {
    rpcMock.mockReturnValue(new Promise(() => undefined));
    markSeen();
    useSessions.getState().reset();
    useSessions.setState({ sessions: { [keyOf(marked)]: marked }, loaded: true });
    markSeen();

    expect(seenSent()).toEqual(['ses-done', 'ses-done']);
  });
});
