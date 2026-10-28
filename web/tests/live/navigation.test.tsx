/**
 * The call is the page's, not a screen's: it goes on across every route change, it is found again
 * by the entry of the next page, and only the person, the gateway, a change of account or the page
 * closing ends it. The entries here are the real ones, in the real top bar and chat header.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Link, MemoryRouter, Route, Routes, useParams } from 'react-router';
import { ChatHeader } from '../../src/features/chat/ChatHeader';
import { AppLayout } from '../../src/layout/AppLayout';
import { initialView } from '../../src/features/live/callState';
import { useLive } from '../../src/features/live/store';
import { liveTables } from '../../src/features/live/strings';
import { useAuth } from '../../src/stores/auth';
import { useSettings } from '../../src/stores/settings';
import { sessions } from '../../mock/fixtures';
import { FakeAudioContext, FakeWebSocket, READY } from './audioFakes';
import { Host } from './Host';
import {
  afterLive,
  beforeLive,
  context,
  dial,
  en,
  endCall,
  type LiveEnv,
  worklet,
} from './harness';

const session = { ...sessions[0]!, turn: null, usage: null };
const chatPath = `/sessions/${session.device_id}/${session.session_id}`;

let env: LiveEnv;

beforeEach(() => {
  env = beforeLive();
});

afterEach(afterLive);

/** A chat page: its header is keyed by the conversation, as the real page's is, so each one remounts it. */
function Chat() {
  const { deviceId, sessionId } = useParams();
  return (
    <>
      <ChatHeader
        key={`header/${deviceId}/${sessionId}`}
        session={session}
        agent={null}
        deviceName="mac-studio"
        deviceOnline
        todos={[]}
        stopping={false}
        onStop={vi.fn()}
      />
      <Link to="/settings">to settings</Link>
      <Link to="/sessions/dev-mac/ses-other">another session</Link>
    </>
  );
}

/**
 * The sessions list inside the top bar, a chat page with its own header, and nothing else. Like the
 * app's own, the routes are keyed by the interface language, so a change of it remounts every screen.
 */
function App() {
  const language = useSettings((s) => s.language);
  return (
    <MemoryRouter initialEntries={['/sessions']}>
      <Host />
      <Routes key={language}>
        <Route path="/sessions/:deviceId/:sessionId" element={<Chat />} />
        <Route element={<AppLayout />}>
          <Route path="/sessions" element={<Link to={chatPath}>open the session</Link>} />
          <Route path="/settings" element={<Link to="/sessions">back to the list</Link>} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

async function startInTopBar(strict = false): Promise<FakeWebSocket> {
  render(strict ? <StrictMode><App /></StrictMode> : <App />);
  fireEvent.click(await screen.findByRole('button', { name: en.open }));
  const ws = await dial();
  ws.open();
  act(() => ws.serverSend(READY));
  await screen.findByRole('button', { name: en.hangUp });
  await waitFor(() => expect(context().worklets).toHaveLength(1));
  return ws;
}

const goToChat = async (): Promise<void> => {
  fireEvent.click(screen.getByRole('link', { name: 'open the session' }));
  await screen.findByRole('heading', { level: 1 });
};

describe('a call across the app', () => {
  it('goes on when the sessions list gives way to a chat page, and the chat header shows it', async () => {
    const ws = await startInTopBar();
    act(() => ws.serverSend({ type: 'caption', speaker: 'user', delta: 'What is running?' }));
    fireEvent.click(screen.getByRole('button', { name: en.close }));

    await goToChat();

    // Nothing was closed or dialled again; the page's own header carries the entry.
    expect(ws.closedWith).toBeNull();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeAudioContext.instances).toHaveLength(1);
    expect(env.fakes.stream.track.stopped).toBe(false);
    const entry = screen.getByRole('button', { name: en.openInCall });
    expect(entry).toHaveAttribute('data-active', 'true');
    expect(entry.closest('.chat-header')).not.toBeNull();

    // It is the same call: the same words so far, and the voice still plays.
    fireEvent.click(entry);
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
    expect(screen.getByText('What is running?')).toBeInTheDocument();
    act(() => ws.serverAudio(1920));
    expect(context().sources).toHaveLength(1);
  });

  it('goes on across any number of page changes and can be hung up from the last one', async () => {
    const ws = await startInTopBar();
    await goToChat();
    fireEvent.click(screen.getByRole('button', { name: en.openInCall }));
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    endCall(ws, 'hangup');
    expect(context().closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('is neither ended nor doubled by StrictMode mounting everything twice', async () => {
    const ws = await startInTopBar(true);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeAudioContext.instances).toHaveLength(1);
    expect(ws.closedWith).toBeNull();
    await goToChat();
    expect(ws.closedWith).toBeNull();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(screen.getByRole('button', { name: en.openInCall })).toBeInTheDocument();
    // The worklet was tapped once, too: one microphone for one call.
    expect(context().worklets).toHaveLength(1);
  });

  it('does not open a second call from the next page while one is on', async () => {
    await startInTopBar();
    await goToChat();
    fireEvent.click(screen.getByRole('button', { name: en.openInCall }));
    expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('goes on when a change of the interface language remounts every screen', async () => {
    const ws = await startInTopBar();
    act(() => useSettings.setState({ language: 'zh-Hans' }));
    expect(ws.closedWith).toBeNull();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(context().closed).toBe(false);
    // The entry is a new one, speaking the new language, and shows the same call.
    expect(
      await screen.findByRole('button', { name: liveTables['zh-Hans'].openInCall }),
    ).toHaveAttribute('data-active', 'true');
  });

  it('asks the gateway about the feature once, not once for each page', async () => {
    await startInTopBar();
    await goToChat();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe('the draft waiting for a yes', () => {
  const draft = { id: 'd1', session: 'Billing', text: 'Skip the migration tests' };
  const closePanel = (): void => void fireEvent.click(screen.getByRole('button', { name: en.close }));
  const go = (name: string): void => void fireEvent.click(screen.getByRole('link', { name }));

  it('brings the panel up on whatever page is showing, once for each draft', async () => {
    const ws = await startInTopBar();
    closePanel();

    act(() => ws.serverSend({ type: 'draft', draft }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // Closed on purpose with the draft still waiting: it stays closed.
    closePanel();
    act(() => ws.serverSend({ type: 'draft', draft }));
    act(() => ws.serverSend({ type: 'time', seconds_left: 1100 }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // A new draft is news again.
    act(() => ws.serverSend({ type: 'draft', draft: { ...draft, id: 'd2', text: 'Another one' } }));
    expect(screen.getByText('Another one')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('is waiting in the panel of the page the person moved to, if it was not seen yet', async () => {
    const ws = await startInTopBar();
    closePanel();
    await goToChat();
    act(() => ws.serverSend({ type: 'draft', draft }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: en.send }));
    expect(ws.commands().at(-1)).toEqual({ type: 'draft.send', draft_id: 'd1' });
  });

  it('shows a draft that still waits for a yes on the next page, once for each page change', async () => {
    const ws = await startInTopBar();
    closePanel();
    act(() => ws.serverSend({ type: 'draft', draft }));
    closePanel();

    // The page changes with the draft still waiting: the new page's entry shows it.
    await goToChat();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(draft.text)).toBeInTheDocument();

    // Closed here, it stays closed: nothing reopens it on its own.
    closePanel();
    act(() => ws.serverSend({ type: 'time', seconds_left: 1100 }));
    act(() => ws.serverSend({ type: 'draft', draft }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // The next page change shows it again, and only that once.
    go('to settings');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    closePanel();
    act(() => ws.serverSend({ type: 'time', seconds_left: 1000 }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows it again when the person goes to another session, whose header is a new one', async () => {
    const ws = await startInTopBar();
    await goToChat();
    act(() => ws.serverSend({ type: 'draft', draft }));
    closePanel();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    go('another session');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(draft.text)).toBeInTheDocument();
  });

  it('brings nothing up on the next page once the draft is gone', async () => {
    const ws = await startInTopBar();
    closePanel();
    act(() => ws.serverSend({ type: 'draft', draft }));
    closePanel();
    act(() => ws.serverSend({ type: 'draft', draft: null }));

    await goToChat();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('what ends a call', () => {
  it('ends when the person signs out: hangs up, then lets everything go at once', async () => {
    const ws = await startInTopBar();
    act(() => useAuth.setState({ status: 'signed-out', username: null, role: null }));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.closedWith).toBe(1000);
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(worklet().disconnected).toBe(true);
  });

  it('ends when a different account signs in', async () => {
    const ws = await startInTopBar();
    act(() => useAuth.setState({ username: 'dana' }));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.closedWith).toBe(1000);
    expect(context().closed).toBe(true);
  });

  it('lets nothing of the last account show for the next, and gives the first its own back', async () => {
    const earlier = {
      id: '1700000000000',
      startedAt: 1_700_000_000_000,
      endedAt: 1_700_000_060_000,
      reason: 'hangup',
      entries: [{ kind: 'turn', speaker: 'user', text: 'an earlier call of admin' }],
    };
    env.storage.set('live.history:admin', JSON.stringify([earlier]));
    const ws = await startInTopBar();
    act(() => ws.serverSend({ type: 'caption', speaker: 'user', delta: 'private words' }));
    act(() =>
      ws.serverSend({ type: 'draft', draft: { id: 'd1', session: 'Billing', text: 'a secret draft' } }),
    );
    expect(screen.getByText('a secret draft')).toBeInTheDocument();

    act(() => useAuth.setState({ username: 'dana' }));

    // The store is back to where it started, and the call was kept for admin alone.
    expect(useLive.getState().view).toEqual(initialView);
    expect(useLive.getState().saved).toEqual({ owner: null, records: [] });
    expect(useLive.getState().announced).toBeNull();
    expect(JSON.parse(env.storage.get('live.history:admin') ?? '[]')).toHaveLength(2);
    expect(env.storage.has('live.history:dana')).toBe(false);

    // The panel, still open, is dana's: nothing of admin's anywhere in it.
    expect(await screen.findByText(en.intro)).toBeInTheDocument();
    for (const text of ['private words', 'an earlier call of admin', 'a secret draft', en.lastConversation]) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }

    act(() => useAuth.setState({ username: 'admin' }));
    expect(await screen.findByText('private words')).toBeInTheDocument();
    // The earlier call of admin is in the list again, next to the one that was just kept.
    expect(document.querySelectorAll('.live-call')).toHaveLength(2);
  });

  it('is not ended by the same account being read again', async () => {
    const ws = await startInTopBar();
    act(() => useAuth.setState({ status: 'signed-in', username: 'admin' }));
    expect(ws.closedWith).toBeNull();
  });

  it('ends when the page is closed or reloaded', async () => {
    const ws = await startInTopBar();
    act(() => void window.dispatchEvent(new Event('pagehide')));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.closedWith).toBe(1000);
    expect(env.fakes.stream.track.stopped).toBe(true);
  });

  it('ends when the gateway ends it, on any page', async () => {
    const ws = await startInTopBar();
    await goToChat();
    endCall(ws, 'idle');
    expect(context().closed).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: en.open }));
    expect(screen.getByText(en.endedIdle)).toBeInTheDocument();
  });

  it('lets a call that was still being set up go when the account changes under it', async () => {
    let grant: (stream: unknown) => void = () => undefined;
    env.fakes.getUserMedia.mockReturnValue(new Promise((resolve) => (grant = resolve)));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: en.open }));
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    act(() => useAuth.setState({ username: 'dana' }));
    await act(async () => grant(env.fakes.stream));
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(context().closed).toBe(true);
  });

  it('can start again after the account changed', async () => {
    const first = await startInTopBar();
    act(() => useAuth.setState({ username: 'dana' }));
    expect(first.closedWith).toBe(1000);
    act(() => useAuth.setState({ username: 'admin' }));
    // The panel the first call was started from is still open, and offers a new call.
    fireEvent.click(await screen.findByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
  });
});
