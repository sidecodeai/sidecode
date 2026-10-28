import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { startCall as pressStart } from '../../src/features/live/liveCall';
import { SILENCE_TIMEOUT_MS } from '../../src/features/live/liveSocket';
import { liveTables } from '../../src/features/live/strings';
import { useAuth } from '../../src/stores/auth';
import { useSettings } from '../../src/stores/settings';
import { FakeAudioContext, FakeWebSocket, READY } from './audioFakes';
import {
  afterLive,
  beforeLive,
  context,
  dial,
  en,
  endCall,
  type LiveEnv,
  openPanel,
  redial,
  renderEntry,
  startCall,
  status,
  worklet,
} from './harness';

let env: LiveEnv;
let fakes: LiveEnv['fakes'];

beforeEach(() => {
  env = beforeLive();
  fakes = env.fakes;
});

afterEach(afterLive);

describe('the entry', () => {
  it('renders nothing while the gateway does not offer the feature', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => status({ enabled: false }, { status: 200 })));
    const { container } = renderEntry();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the gateway answers 404', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => status({ error: { code: 'not_found' } }, { status: 404 })));
    const { container } = renderEntry();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the button and opens a panel in the interface language', async () => {
    await openPanel();
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
    expect(screen.getByText(en.experimental)).toBeInTheDocument();
  });

  it('speaks Chinese when the interface language is Chinese', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    await openPanel(liveTables['zh-Hans'].open);
    expect(screen.getByRole('button', { name: liveTables['zh-Hans'].start })).toBeInTheDocument();
  });

  it('keeps the call going when the panel is closed and reopened', async () => {
    await startCall();
    fireEvent.click(screen.getByRole('button', { name: en.close }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: en.openInCall }));
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
  });

  it('says a call is on while the panel is closed', async () => {
    await openPanel();
    const idle = screen.getByRole('button', { name: en.open });
    expect(idle).toHaveAttribute('data-active', 'false');
    expect(idle.querySelector('.live-dot')).toBeNull();

    const ws = await dial();
    ws.open();
    act(() => ws.serverSend(READY));
    await screen.findByRole('button', { name: en.hangUp });
    fireEvent.click(screen.getByRole('button', { name: en.close }));

    const button = screen.getByRole('button', { name: en.openInCall });
    expect(button).toHaveAttribute('data-active', 'true');
    const dot = button.querySelector('.live-dot');
    expect(dot).toHaveAttribute('data-speaker', 'none');
    act(() => ws.serverSend({ type: 'state', speaker: 'user' }));
    expect(dot).toHaveAttribute('data-speaker', 'user');

    act(() => {
      ws.serverSend({ type: 'ended', reason: 'idle' });
      ws.serverClose(1000);
    });
    expect(screen.getByRole('button', { name: en.open }).querySelector('.live-dot')).toBeNull();
  });

  it('asks the gateway once per page load, so a remount shows the button at once', async () => {
    const first = renderEntry();
    await screen.findByRole('button', { name: en.open });
    first.unmount();
    renderEntry();
    expect(screen.getByRole('button', { name: en.open })).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('does not take the answer given for one account as the answer for another', async () => {
    renderEntry();
    await screen.findByRole('button', { name: en.open });
    // The gateway has not yet said anything about the second account: nothing is shown for it.
    const unanswered = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal('fetch', unanswered);
    act(() => useAuth.setState({ username: 'dana' }));
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(unanswered).toHaveBeenCalledTimes(1);
  });
});

describe('a call', () => {
  it('asks for the microphone with echo cancellation and dials the gateway', async () => {
    await openPanel();
    const ws = await dial();
    expect(fakes.getUserMedia).toHaveBeenCalledWith({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    expect(ws.url).toMatch(/\/ws\/live$/);
    expect(screen.getByText(en.connecting)).toBeInTheDocument();
    // No audio is sent before the gateway says it is ready.
    expect(context().worklets).toHaveLength(0);
  });

  it('is live once the gateway says ready, and shows the time left', async () => {
    const ws = await startCall();
    expect(screen.getByText(en.listening)).toBeInTheDocument();
    expect(screen.getByText(en.timeLeft(20))).toBeInTheDocument();
    expect(ws.commands()).toContainEqual({ type: 'captions', enabled: true });
    expect(context().audioWorklet.addModule).toHaveBeenCalledWith('/audio-worklet.js');
  });

  it('ignores a second start while one is under way, and while a call is on', async () => {
    await openPanel();
    // Two starts in the same breath: the second meets the one still waiting for the microphone.
    await Promise.all([pressStart(), pressStart()]);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeAudioContext.instances).toHaveLength(1);
    expect(fakes.getUserMedia).toHaveBeenCalledTimes(1);
    // And one more with the call already open.
    await pressStart();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeAudioContext.instances).toHaveLength(1);
  });

  it('streams the microphone up as frames of 20 ms', async () => {
    const ws = await startCall();
    ws.readyState = 1;
    for (let i = 0; i < 30; i += 1) worklet().post(new Float32Array(128).fill(0.1));
    const frames = ws.frames();
    expect(frames).toHaveLength(4);
    for (const frame of frames) expect(frame.byteLength).toBe(960);
  });

  it('plays what the assistant says and silences it on a flush', async () => {
    const ws = await startCall();
    act(() => {
      ws.serverAudio(1920);
      ws.serverAudio(1920);
      ws.serverAudio(1920);
    });
    const { sources } = context();
    expect(sources).toHaveLength(3);
    expect(sources.every((s) => !s.stopped)).toBe(true);
    act(() => ws.serverSend({ type: 'flush' }));
    expect(sources.every((s) => s.stopped)).toBe(true);
    act(() => ws.serverAudio(1920));
    expect(sources[3]?.stopped).toBe(false);
  });

  it('says so when the voice service reports a problem, and the call goes on', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'notice', code: 'server_error' }));
    expect(screen.getByRole('status')).toHaveTextContent(en.notice);
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_100);
    });
    expect(screen.queryByText(en.notice)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
  });

  it('shows who is speaking', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'state', speaker: 'assistant' }));
    expect(screen.getByText(en.speaking)).toBeInTheDocument();
    act(() => ws.serverSend({ type: 'state', speaker: 'user' }));
    expect(screen.getByText(en.hearingYou)).toBeInTheDocument();
    act(() => ws.serverSend({ type: 'state', speaker: 'none' }));
    expect(screen.getByText(en.listening)).toBeInTheDocument();
  });

  it('says checking while the assistant is working and nobody is speaking', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'working', on: true }));
    expect(screen.getByText(en.checking)).toBeInTheDocument();
    expect(screen.queryByText(en.listening)).not.toBeInTheDocument();
    act(() => ws.serverSend({ type: 'working', on: false }));
    expect(screen.getByText(en.listening)).toBeInTheDocument();
  });

  it('puts the assistant speaking and hearing you before checking, and ending before everything', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'working', on: true }));
    act(() => ws.serverSend({ type: 'state', speaker: 'user' }));
    expect(screen.getByText(en.hearingYou)).toBeInTheDocument();
    act(() => ws.serverSend({ type: 'state', speaker: 'assistant' }));
    expect(screen.getByText(en.speaking)).toBeInTheDocument();
    act(() => ws.serverSend({ type: 'state', speaker: 'none' }));
    expect(screen.getByText(en.checking)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    expect(screen.getByText(en.ending)).toBeInTheDocument();
    expect(screen.queryByText(en.checking)).not.toBeInTheDocument();
  });

  it('says checking in the interface language', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'working', on: true }));
    // The words are read when the panel draws; the next message from the gateway draws it again.
    act(() => {
      useSettings.setState({ language: 'zh-Hans' });
      ws.serverSend({ type: 'time', seconds_left: 1100 });
    });
    expect(screen.getByText('正在查询…')).toBeInTheDocument();
    expect(liveTables['zh-Hans'].checking).toBe('正在查询…');
  });

  it('forgets what it was checking when the call ends, and a new call starts listening', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'working', on: true }));
    endCall(ws, 'idle');
    await redial();
    expect(screen.getByText(en.listening)).toBeInTheDocument();
    expect(screen.queryByText(en.checking)).not.toBeInTheDocument();
  });

  it('asks the gateway for the captions at every call start, with no switch to turn them off', async () => {
    const ws = await startCall();
    expect(ws.commands()[0]).toEqual({ type: 'captions', enabled: true });
    expect(ws.commands().filter((command) => command.type === 'captions')).toHaveLength(1);
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });
});

describe('the draft card', () => {
  const draft = { id: 'd1', session: 'Billing', text: 'Skip the migration tests' };

  it('shows the pending draft and sends it on the button', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'draft', draft }));
    expect(screen.getByText('Skip the migration tests')).toBeInTheDocument();
    expect(screen.getByText(en.draftTo('Billing'))).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: en.send }));
    expect(ws.commands().at(-1)).toEqual({ type: 'draft.send', draft_id: 'd1' });
    expect(screen.getByRole('button', { name: en.sending })).toBeInTheDocument();

    act(() => {
      ws.serverSend({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' });
      ws.serverSend({ type: 'draft', draft: null });
    });
    // The card is gone; what was sent is part of the conversation now.
    expect(screen.queryByText(en.draftTitle)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.send })).not.toBeInTheDocument();
    expect(screen.getByText(en.sent)).toBeInTheDocument();
    const log = screen.getByRole('log', { name: en.conversation });
    expect(within(log).getByText(en.sentTo('Billing'))).toBeInTheDocument();
    expect(within(log).getByText('Skip the migration tests')).toBeInTheDocument();
  });

  it.each([
    ['the result, then the draft is cleared', ['result', 'cleared']],
    ['the draft is cleared, then the result', ['cleared', 'result']],
  ] as const)('puts a message sent by a spoken yes in the conversation too: %s', async (_order, order) => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'draft', draft }));
    expect(screen.getByText(en.draftTitle)).toBeInTheDocument();
    const says = {
      result: { type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' },
      cleared: { type: 'draft', draft: null },
    };
    for (const message of order) act(() => ws.serverSend(says[message]));

    // Nobody pressed anything: the person said yes.
    expect(ws.commands().some((command) => command.type === 'draft.send')).toBe(false);
    expect(screen.queryByText(en.draftTitle)).not.toBeInTheDocument();
    const log = screen.getByRole('log', { name: en.conversation });
    expect(within(log).getByText(en.sentTo('Billing'))).toBeInTheDocument();
    expect(within(log).getByText('Skip the migration tests')).toBeInTheDocument();
    expect(within(log).getAllByText('Skip the migration tests')).toHaveLength(1);
    expect(screen.getByText(en.sent)).toBeInTheDocument();
  });

  it('brings the panel up when a draft arrives, once for each draft', async () => {
    const ws = await startCall();
    fireEvent.click(screen.getByRole('button', { name: en.close }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    act(() => ws.serverSend({ type: 'draft', draft }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Skip the migration tests')).toBeInTheDocument();

    // Closed on purpose with the draft still waiting: it stays closed.
    fireEvent.click(screen.getByRole('button', { name: en.close }));
    act(() => ws.serverSend({ type: 'draft', draft }));
    act(() => ws.serverSend({ type: 'time', seconds_left: 1100 }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // A different draft is news again.
    act(() => ws.serverSend({ type: 'draft', draft: { ...draft, id: 'd2', text: 'Another one' } }));
    expect(screen.getByText('Another one')).toBeInTheDocument();
  });

  it('keeps the panel up after a draft is sent, so the person sees that it was', async () => {
    const ws = await startCall();
    fireEvent.click(screen.getByRole('button', { name: en.close }));
    act(() => ws.serverSend({ type: 'draft', draft }));
    fireEvent.click(screen.getByRole('button', { name: en.send }));
    act(() => {
      ws.serverSend({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' });
      ws.serverSend({ type: 'draft', draft: null });
    });
    expect(screen.getByText(en.sent)).toBeInTheDocument();
  });

  it('cancels a draft on the button', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'draft', draft }));
    fireEvent.click(screen.getByRole('button', { name: en.cancel }));
    expect(ws.commands().at(-1)).toEqual({ type: 'draft.cancel', draft_id: 'd1' });
  });

  it('says when the message could not be sent, and lets the person try again', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'draft', draft }));
    fireEvent.click(screen.getByRole('button', { name: en.send }));
    act(() => ws.serverSend({ type: 'draft.result', draft_id: 'd1', ok: false, error: 'send_failed' }));
    expect(screen.getByText(en.errorSend)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.send })).toBeEnabled();
  });
});

describe('ending a call', () => {
  it('hangs up: tells the gateway, stops the microphone and the voice at once', async () => {
    const ws = await startCall();
    act(() => ws.serverAudio(1920));
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(fakes.stream.track.stopped).toBe(true);
    expect(context().sources.every((s) => s.stopped)).toBe(true);
    expect(screen.getByText(en.ending)).toBeInTheDocument();

    act(() => {
      ws.serverSend({ type: 'ended', reason: 'hangup' });
      ws.serverClose(1000);
    });
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
    expect(screen.getByText(en.intro)).toBeInTheDocument();
    expect(context().closed).toBe(true);
  });

  it('closes the socket itself when the gateway does not say goodbye', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_600);
    });
    expect(ws.closedWith).toBe(1000);
    expect(context().closed).toBe(true);
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
  });

  it('lets the person hang up while it is still connecting, and lets everything go', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await openPanel();
    const ws = await dial();
    ws.open();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
    });
    const button = screen.getByRole('button', { name: en.hangUp });
    expect(button).toBeEnabled();
    fireEvent.click(button);

    expect(ws.closedWith).toBe(1000);
    expect(fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(context().worklets).toHaveLength(0);
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
    expect(screen.getByText(en.intro)).toBeInTheDocument();
  });

  it('gives up on a microphone still being asked for, and lets the stream go when it comes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let grant: (stream: unknown) => void = () => undefined;
    fakes.getUserMedia.mockReturnValue(new Promise((resolve) => (grant = resolve)));
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
    });
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
    expect(screen.getByText(en.intro)).toBeInTheDocument();

    await act(async () => grant(fakes.stream));
    expect(fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
  });

  it('can start again at once after giving up', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
    });
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1] as FakeWebSocket;
    second.open();
    act(() => second.serverSend(READY));
    await screen.findByText(en.listening);
    expect(FakeWebSocket.instances[0]?.closedWith).toBe(1000);
    expect(second.closedWith).toBeNull();
  });

  it('does not take the second click of a double click on Start for a hang-up', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    // The panel offers Hang up where Start was, and the second click of the double click lands on it.
    fireEvent.click(await screen.findByRole('button', { name: en.hangUp }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(screen.getByText(en.connecting)).toBeInTheDocument();
    expect(FakeWebSocket.instances[0]?.closedWith).toBeNull();
    expect(context().closed).toBe(false);

    // Past the guard, the same button does give up.
    await act(async () => void (await vi.advanceTimersByTimeAsync(800)));
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    expect(FakeWebSocket.instances[0]?.closedWith).toBe(1000);
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
  });

  it('lets go of a microphone that was granted after the page was closed', async () => {
    let grant: (stream: unknown) => void = () => undefined;
    fakes.getUserMedia.mockReturnValue(new Promise((resolve) => (grant = resolve)));
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    act(() => void window.dispatchEvent(new Event('pagehide')));
    await act(async () => grant(fakes.stream));
    expect(fakes.stream.track.stopped).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(context().closed).toBe(true);
  });

  it.each([
    ['idle', en.endedIdle],
    ['max_duration', en.endedLimit],
    ['expired', en.endedLimit],
    ['daily_limit', en.endedDaily],
    ['upstream_lost', en.endedLost],
    ['session_revoked', en.ended],
  ])('when the gateway ends it (%s), says so and lets everything go', async (reason, words) => {
    const ws = await startCall();
    act(() => {
      ws.serverSend({ type: 'ended', reason });
      ws.serverClose(reason === 'upstream_lost' ? 4502 : 1000);
    });
    expect(screen.getByText(words)).toBeInTheDocument();
    expect(fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(worklet().disconnected).toBe(true);
  });

  it('hangs up by itself when the gateway goes silent', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SILENCE_TIMEOUT_MS + 100);
    });
    expect(screen.getByText(en.endedLost)).toBeInTheDocument();
    expect(fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(ws.closedWith).toBe(1000);
  });

  it('keeps going while the gateway keeps ticking', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    for (let i = 0; i < 6; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
        ws.serverSend({ type: 'time', seconds_left: 1000 - i });
      });
    }
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
  });

  it('tells the gateway and lets everything go when the page is closed in the middle of a call', async () => {
    const ws = await startCall();
    act(() => void window.dispatchEvent(new Event('pagehide')));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(fakes.stream.track.stopped).toBe(true);
    expect(ws.closedWith).toBe(1000);
    expect(context().closed).toBe(true);
    expect(worklet().disconnected).toBe(true);
  });

  it('does not end the call when a screen goes away: the call is the page\'s, not the screen\'s', async () => {
    const ws = await startCall();
    cleanup();
    expect(ws.closedWith).toBeNull();
    expect(fakes.stream.track.stopped).toBe(false);
    expect(context().closed).toBe(false);
    renderEntry();
    expect(screen.getByRole('button', { name: en.openInCall })).toBeInTheDocument();
  });
});

describe('a call that cannot start', () => {
  it('words a refused microphone and dials nothing', async () => {
    fakes.getUserMedia.mockRejectedValue(new DOMException('no', 'NotAllowedError'));
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    expect(await screen.findByText(en.errorMic)).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(context().closed).toBe(true);
  });

  it('words a missing microphone', async () => {
    fakes.getUserMedia.mockRejectedValue(new DOMException('none', 'NotFoundError'));
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    expect(await screen.findByText(en.errorMicUnavailable)).toBeInTheDocument();
  });

  it.each([
    ['call_active', en.errorBusy],
    ['cooldown', en.errorCooldown],
    ['too_busy', en.errorTooBusy],
    ['daily_limit', en.errorDaily],
    ['not_allowed', en.errorNotAllowed],
    ['upstream_refused', en.errorService],
  ])('words a refusal (%s) from the gateway and lets the microphone go', async (reason, words) => {
    await openPanel();
    const ws = await dial();
    act(() => {
      ws.serverSend({ type: 'ended', reason });
      ws.serverClose(4409);
    });
    expect(screen.getByText(words)).toBeInTheDocument();
    expect(fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
  });

  it('words a gateway that cannot be reached', async () => {
    await openPanel();
    const ws = await dial();
    act(() => ws.serverClose(1006));
    expect(screen.getByText(en.errorService)).toBeInTheDocument();
  });

  it('can start again after a refusal', async () => {
    await openPanel();
    const first = await dial();
    act(() => {
      first.serverSend({ type: 'ended', reason: 'cooldown' });
      first.serverClose(4429);
    });
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
  });

  it('words a worklet that cannot be loaded and hangs up', async () => {
    await openPanel();
    const ws = await dial();
    context().audioWorklet.addModule.mockRejectedValue(new Error('404'));
    ws.open();
    act(() => ws.serverSend(READY));
    expect(await screen.findByText(en.errorMicUnavailable)).toBeInTheDocument();
    expect(ws.closedWith).toBe(1000);
    expect(fakes.stream.track.stopped).toBe(true);
  });
});

describe('the words', () => {
  it('never name the old product', () => {
    const text = JSON.stringify(
      Object.values(liveTables).map((table) =>
        Object.values(table).map((value) => (typeof value === 'function' ? value('x' as never) : value)),
      ),
    );
    expect(text.toLowerCase()).not.toContain('remote control');
    expect(text).not.toContain('远程控制');
  });

  it('say that the minutes left are those of this call, not of anything the account is given', () => {
    expect(en.timeLeft(20)).toBe('20 min left in this call');
    expect(liveTables['zh-Hans'].timeLeft(20)).toBe('本次通话还剩 20 分钟');
  });

  it('say the same things in both languages', () => {
    expect(Object.keys(liveTables['zh-Hans']).sort()).toEqual(Object.keys(liveTables.en).sort());
  });
});
