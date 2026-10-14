/**
 * Signing out empties the tab. Everything the previous account did used to stay
 * in memory until the page was reloaded: transcripts, unsent messages, the
 * slash-command cache, the working state of every question card — including a
 * field whose own placeholder says the value is not stored.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QuestionCard } from '../src/features/chat/blocks/QuestionCard';
import { useAnswers } from '../src/stores/answers';
import { useChat } from '../src/stores/chat';
import { useCommands } from '../src/stores/commands';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useDrafts } from '../src/stores/drafts';
import { useFront } from '../src/stores/front';
import { useOutbox } from '../src/stores/outbox';
import { useSessions } from '../src/stores/sessions';
import { signOut } from '../src/stores/signOut';
import { rememberTerminal, rememberedTerminal } from '../src/stores/terminal';
import { useUsers } from '../src/stores/users';
import { emptyTimeline } from '../src/stores/timeline';
import type { QuestionEvent } from '../src/protocol/types';

vi.mock('../src/lib/gateway', () => ({
  rpc: vi.fn(),
  getSocket: () => null,
  setSocket: () => undefined,
}));

const KEY = 'dev-a/ses-a';

const question = (status: QuestionEvent['status']): QuestionEvent =>
  ({
    seq: 7,
    ts: 1,
    kind: 'question',
    block_id: 'q-block',
    request_id: 'req-1',
    status,
    questions: [
      { id: 'q1', prompt: 'API key?', options: [], multi: false, allow_text: true, secret: true },
    ],
  }) as QuestionEvent;

/** What one signed-in account leaves behind in the stores. */
function fillStores(): void {
  useChat.setState({
    sessions: {
      [KEY]: {
        key: KEY,
        deviceId: 'dev-a',
        sessionId: 'ses-a',
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
  useDrafts.getState().setText(KEY, 'UNSENT-A');
  useOutbox.getState().add({
    id: 'req-9',
    sessionKey: KEY,
    sessionId: 'ses-a',
    text: 'UNSENT-A',
    attachments: [],
    mode: 'auto',
    at: 1,
    error: 'unconfirmed',
  });
  useAnswers.getState().setText('req-1', 'q1', 'MY-API-KEY');
  useCommands.setState({ entries: { 'ses-a': { commands: [], at: 1, loading: false } } });
  useConnection.setState({
    status: 'open',
    username: 'alice',
    gatewayVersion: '1.3.0',
    protocol: 1,
    stt: { enabled: true },
    polish: { enabled: true },
  });
  useSessions.setState({ sessions: {}, loaded: true, agentFilter: 'codex' });
  useDevices.setState({ devices: [], loaded: true, updateErrors: { 'dev-a': 'busy' } });
  useUsers.setState({ users: [], registrationOpen: true, loaded: true, error: null });
  // A38: the shell this account left detached on a device. The next person in
  // the browser must not attach to it.
  rememberTerminal('dev-a', 'term-a');
  // A47: the conversation this account had in front of it.
  useFront.setState({ key: KEY });
}

beforeEach(() => {
  fillStores();
});

describe('signing out', () => {
  it('leaves nothing of the account in the tab', () => {
    signOut();

    expect(Object.keys(useChat.getState().sessions)).toEqual([]);
    expect(Object.keys(useDrafts.getState().drafts)).toEqual([]);
    expect(Object.keys(useOutbox.getState().pending)).toEqual([]);
    expect(useAnswers.getState().drafts).toEqual({});
    expect(Object.keys(useCommands.getState().entries)).toEqual([]);
    expect(useSessions.getState().sessions).toEqual({});
    expect(useDevices.getState().updateErrors).toEqual({});
    expect(useUsers.getState().registrationOpen).toBe(false);
    expect(rememberedTerminal('dev-a')).toBeNull();
    expect(useFront.getState().key).toBeNull();
  });

  it('puts the gateway’s capabilities back to what no hello has confirmed', () => {
    signOut();

    const connection = useConnection.getState();
    expect(connection.username).toBeNull();
    expect(connection.gatewayVersion).toBeNull();
    expect(connection.protocol).toBeNull();
    expect(connection.stt).toEqual({ enabled: false });
    expect(connection.polish).toEqual({ enabled: false });
  });
});

describe('a question that stops being pending', () => {
  it('takes its working state with it, however it ended', () => {
    const answered = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<QuestionCard event={question('pending')} onAnswer={answered} />);
    expect(useAnswers.getState().drafts['req-1']?.text['q1']).toBe('MY-API-KEY');

    // Answered in the terminal, or expired: neither goes through this card's
    // own submit, which is the only thing that used to clear the draft.
    rerender(<QuestionCard event={question('resolved')} onAnswer={answered} />);

    expect(useAnswers.getState().drafts['req-1']).toBeUndefined();
    // And the secret is not echoed back into the resolved card either.
    expect(screen.getByLabelText('API key?')).toHaveValue('');
  });
});
