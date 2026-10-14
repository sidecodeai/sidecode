/**
 * `docs/DESIGN.md` § "The composer": every change made from the model card is
 * drawn the moment it is made, and the device's reply confirms it or a refusal
 * puts the previous value back. The card never waits for the round trip —
 * except on a session a terminal shares, where A40 has it follow the terminal
 * rather than run ahead of it.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { ChatPage } from '../src/features/chat/ChatPage';
import { useChat } from '../src/stores/chat';
import { emptyTimeline } from '../src/stores/timeline';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useOutbox } from '../src/stores/outbox';
import { keyOf, useSessions } from '../src/stores/sessions';
import { RequestError } from '../src/lib/ws';
import { strings } from '../src/strings';
import { claudeAgent, codexAgent, devices as deviceFixtures } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

// jsdom has no scrolling, and the timeline scrolls itself to the latest row on
// mount. Stubbed the way `Timeline.test.tsx` stubs it.
const nativeScrollTo = HTMLElement.prototype.scrollTo;

beforeAll(() => {
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement) {
    // Nothing to scroll: this suite is about the composer, not the timeline.
  } as HTMLElement['scrollTo'];
});

afterAll(() => {
  HTMLElement.prototype.scrollTo = nativeScrollTo;
});

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const device = { ...deviceFixtures[0]!, device_id: 'dev-mac', agents: [codexAgent] };

const session: Session = {
  session_id: 'ses-card',
  device_id: 'dev-mac',
  agent: 'codex',
  title: 'Tidy the parser',
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
};

const key = keyOf(session);
const stored = (): Session | undefined => useSessions.getState().sessions[key];

function renderChat() {
  render(
    <MemoryRouter initialEntries={[`/sessions/${session.device_id}/${session.session_id}`]}>
      <Routes>
        <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Opens the model card and presses the speed toggle inside it. */
async function toggleSpeed() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: strings.composer.modelCard }));
  await user.click(
    screen.getByRole('button', {
      name: strings.composer.speed(strings.composer.speedStandard),
    }),
  );
}

beforeEach(() => {
  rpcMock.mockReset();
  useSessions.setState({ sessions: { [key]: session }, loaded: true, agentFilter: null });
  useDevices.setState({ devices: [device], loaded: true, updateErrors: {} });
  useChat.setState({ sessions: {} });
  useOutbox.setState({ pending: {} });
  // Never 'open', so the page does not try to subscribe over the mocked socket.
  useConnection.setState({ status: 'closed' });
});

describe('the model card writes before the device answers', () => {
  it('lights the tier before the request resolves', async () => {
    rpcMock.mockReturnValue(new Promise(() => undefined));
    renderChat();
    await toggleSpeed();

    expect(rpcMock).toHaveBeenCalledWith('session.set', {
      session_id: session.session_id,
      speed: 'priority',
    });
    expect(stored()?.speed).toBe('priority');
  });

  it('puts the previous value back when the device refuses', async () => {
    rpcMock.mockRejectedValue(new Error('the agent is busy'));
    renderChat();
    await toggleSpeed();

    await waitFor(() => expect(stored()?.speed).toBeNull());
    expect(await screen.findByText('the agent is busy')).toBeInTheDocument();
  });

  it('does not overwrite a session.updated that landed in between', async () => {
    let refuse: (error: Error) => void = () => undefined;
    rpcMock.mockReturnValue(
      new Promise((_resolve, reject) => {
        refuse = reject;
      }),
    );
    renderChat();
    await toggleSpeed();

    // What the socket does with a `/model` typed in the terminal meanwhile.
    useSessions.getState().upsert({ ...session, speed: 'priority', model: 'gpt-5.4' });
    refuse(new Error('too late'));

    await waitFor(() => expect(screen.getByText('too late')).toBeInTheDocument());
    expect(stored()?.model).toBe('gpt-5.4');
    expect(stored()?.speed).toBe('priority');
  });
});

/**
 * A40 — a session a terminal shares. The device may have to type the change
 * into that terminal, so the card follows the reply rather than running ahead
 * of the screen beside it, and a refusal is shown in the device's own words.
 * `docs/DESIGN.md` § "The composer" → "The device types into a Claude
 * terminal".
 */
describe('the model card on a shared session waits for the terminal', () => {
  const attached: Session = {
    ...session,
    control: 'shared',
    origin: 'terminal',
    effort: 'medium',
  };

  const shareSession = () => {
    useSessions.setState({ sessions: { [key]: attached }, loaded: true, agentFilter: null });
  };

  it('leaves the value alone until the device answers', async () => {
    let answer: (result: { session: Session }) => void = () => undefined;
    rpcMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    shareSession();
    renderChat();
    await toggleSpeed();

    expect(rpcMock).toHaveBeenCalledWith('session.set', {
      session_id: session.session_id,
      speed: 'priority',
    });
    // The terminal has not taken it yet, so neither has the card.
    expect(stored()?.speed).toBeNull();

    answer({ session: { ...attached, speed: 'priority' } });
    await waitFor(() => expect(stored()?.speed).toBe('priority'));
  });

  it('says the terminal is busy in the words the device used', async () => {
    rpcMock.mockRejectedValue(
      new RequestError({ code: 'conflict', message: 'the terminal is busy; try again in a moment' }),
    );
    shareSession();
    renderChat();
    await toggleSpeed();

    expect(
      await screen.findByText('the terminal is busy; try again in a moment'),
    ).toBeInTheDocument();
    // Nothing was guessed on the way out, so nothing has to be put back.
    expect(stored()?.speed).toBeNull();
  });
});

/**
 * A35, §8 rule 17 — a session whose `resume` is set says so above its
 * transcript, and both actions go to the device that holds the resume.
 * `docs/DESIGN.md` § "Paused by the usage limit".
 */
describe('the pending resume above the transcript', () => {
  const at = (): number => Date.now() + 90 * 60_000;

  const paused = (): Session => ({
    ...session,
    resume: { at: at(), estimated: false, attempts: 0, window_minutes: 300 },
  });

  it('draws nothing while the session has no resume', () => {
    renderChat();
    expect(screen.queryByText(new RegExp(strings.chat.pausedByLimit))).toBeNull();
  });

  it('sits between the header and the transcript while one is pending', () => {
    useSessions.setState({ sessions: { [key]: paused() }, loaded: true, agentFilter: null });
    renderChat();

    const notice = document.querySelector('.resume-notice');
    expect(notice).not.toBeNull();
    expect(notice?.textContent).toContain(strings.chat.pausedByLimit);
    // The transcript comes after it, which is what "above the transcript" is.
    const timeline = document.querySelector('.timeline-wrap');
    expect(notice?.compareDocumentPosition(timeline as Node)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('cancels the resume on the device and keeps what it answered', async () => {
    const user = userEvent.setup();
    useSessions.setState({ sessions: { [key]: paused() }, loaded: true, agentFilter: null });
    rpcMock.mockResolvedValue({ session: { ...session, resume: null } });
    renderChat();

    await user.click(screen.getByRole('button', { name: strings.chat.resumeCancel }));

    expect(rpcMock).toHaveBeenCalledWith('session.resume_cancel', {
      session_id: session.session_id,
    });
    await waitFor(() => expect(document.querySelector('.resume-notice')).toBeNull());
  });

  it('says so under the transcript when the cancel fails', async () => {
    const user = userEvent.setup();
    useSessions.setState({ sessions: { [key]: paused() }, loaded: true, agentFilter: null });
    rpcMock.mockRejectedValue(new Error('the device is offline'));
    renderChat();

    await user.click(screen.getByRole('button', { name: strings.chat.resumeCancel }));

    expect(await screen.findByText('the device is offline')).toBeInTheDocument();
    // Nothing was guessed: the notice stays until the device says otherwise.
    expect(document.querySelector('.resume-notice')).not.toBeNull();
  });

  it('moves the resume to the time the reader picked', async () => {
    const user = userEvent.setup();
    const wanted = new Date(Date.now() + 3 * 3_600_000);
    wanted.setSeconds(0, 0);
    const moved = { at: wanted.getTime(), estimated: false, attempts: 0 };
    useSessions.setState({ sessions: { [key]: paused() }, loaded: true, agentFilter: null });
    rpcMock.mockResolvedValue({ session: { ...session, resume: moved } });
    renderChat();

    await user.click(screen.getByRole('button', { name: strings.chat.resumeChange }));
    const field = screen.getByLabelText(strings.chat.resumeAt);
    await user.clear(field);
    await user.type(field, localValue(wanted));
    await user.click(screen.getByRole('button', { name: strings.chat.resumeSet }));

    await waitFor(() =>
      expect(rpcMock).toHaveBeenCalledWith('session.resume_set', {
        session_id: session.session_id,
        at: wanted.getTime(),
      }),
    );
    await waitFor(() => expect(stored()?.resume?.at).toBe(wanted.getTime()));
  });
});

/** What a `datetime-local` field must be typed with. */
function localValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * A42 — Stop on a Claude Code terminal the device is attached to. The shim's
 * pseudo-terminal takes a typed Escape, so the channel carries the interrupt
 * and the header draws Stop from the same flag it reads for a daemon. The one
 * thing the keystroke cannot do is talk over a prompt, and that refusal is the
 * device's own sentence rather than the app's canned one.
 */
describe('Stop on an attached Claude terminal', () => {
  const attachedClaude: Session = {
    ...session,
    agent: 'claude',
    control: 'shared',
    origin: 'terminal',
    state: 'running',
    model: 'claude-sonnet-4-5',
    permission_mode: 'acceptEdits',
    turn: { turn_id: 'trn-1', started_at: Date.now() },
  };

  /**
   * Stop reaches the device through the open conversation, which the page
   * only opens over a live socket. This suite has none, so the conversation
   * is put in the store the way `open()` would leave it.
   */
  const shareRunning = () => {
    useSessions.setState({
      sessions: { [key]: attachedClaude },
      loaded: true,
      agentFilter: null,
    });
    useDevices.setState({
      devices: [{ ...device, agents: [claudeAgent] }],
      loaded: true,
      updateErrors: {},
    });
    useChat.setState({
      sessions: {
        [key]: {
          key,
          deviceId: session.device_id,
          sessionId: session.session_id,
          timeline: emptyTimeline(),
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
  };

  const pressStop = async () => {
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: strings.chat.stop }));
  };

  it('asks the device to stop the turn the terminal started', async () => {
    rpcMock.mockResolvedValue({});
    shareRunning();
    renderChat();
    await pressStop();

    expect(rpcMock).toHaveBeenCalledWith('session.stop', { session_id: session.session_id });
  });

  it('says an open prompt has to be answered, in the device\'s words', async () => {
    rpcMock.mockRejectedValue(
      new RequestError({ code: 'conflict', message: 'answer the prompt first' }),
    );
    shareRunning();
    renderChat();
    await pressStop();

    expect(await screen.findByText('answer the prompt first')).toBeInTheDocument();
    // The canned conflict sentence would send the reader to Take over, which
    // a shared session does not even offer.
    expect(screen.queryByText(strings.errors.conflictTerminal)).not.toBeInTheDocument();
  });
});
