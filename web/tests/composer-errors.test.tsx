/**
 * What the composer says when a send fails. Every other surface routes its
 * failures through `lib/errors.ts` and the string tables; the composer used to
 * render `err.message`, which for the app's own failures was English minted in
 * `lib/ws.ts` — "not connected", "no reply from the gateway", "connection lost"
 * — whatever the interface language was set to.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from '../src/features/chat/Composer';
import { AppSocket, RequestError, type SocketLike } from '../src/lib/ws';
import { useDrafts } from '../src/stores/drafts';
import { strings } from '../src/strings';
import { claudeAgent } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

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

function setup(error: unknown) {
  const onSend = vi.fn().mockRejectedValue(error);
  render(
    <Composer
      session={session}
      agent={claudeAgent}
      deviceOnline
      queue={[]}
      question={null}
      sttEnabled={false}
      onSend={onSend}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />,
  );
  return { onSend };
}

const send = async (text: string): Promise<void> => {
  const user = userEvent.setup();
  await user.click(screen.getByLabelText(strings.composer.placeholder));
  await user.keyboard(text);
  await user.click(screen.getByRole('button', { name: strings.composer.send }));
};

beforeEach(() => {
  useDrafts.getState().reset();
});

describe('a refused send', () => {
  it('says it in the table’s words, not the socket’s', async () => {
    setup(new RequestError({ code: 'device_offline', message: 'not connected' }));
    await send('run the tests');

    expect(await screen.findByText(strings.errors.deviceOffline)).toBeInTheDocument();
    expect(screen.queryByText('not connected')).not.toBeInTheDocument();
  });

  it('falls back to its own sentence for a code with no table entry', async () => {
    setup(new RequestError({ code: 'internal', message: '' }));
    await send('run the tests');

    expect(await screen.findByText(strings.composer.sendFailed)).toBeInTheDocument();
  });
});

describe('the socket mints no sentences of its own', () => {
  const socketThatNeverOpens = (): SocketLike => ({
    send: () => undefined,
    close: () => undefined,
    onopen: null,
    onclose: null,
    onerror: null,
    onmessage: null,
  });

  it('rejects a request on a closed socket with a code and no message', async () => {
    const socket = new AppSocket({
      url: 'ws://gateway.test/ws/app',
      factory: socketThatNeverOpens,
      onFrame: () => undefined,
      onStatus: () => undefined,
    });
    socket.start();

    await expect(socket.request('session.stop', { session_id: 'ses-1' })).rejects.toMatchObject({
      code: 'device_offline',
      message: '',
    });
  });
});
