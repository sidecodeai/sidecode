/**
 * PROTOCOL §5: eight attachments to a message, and no more. Two attach
 * operations can be in flight at once — a pasted batch, then the file dialog
 * before the paste has finished encoding — and each used to compute its budget
 * from the count it read before it started, so sixteen files could reach the
 * draft and earn a `too_large` refusal the app could have prevented.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Composer } from '../src/features/chat/Composer';
import { MAX_ATTACHMENTS } from '../src/features/chat/attachments';
import { useDrafts } from '../src/stores/drafts';
import { strings } from '../src/strings';
import { claudeAgent } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const KEY = 'dev-1/ses-1';

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

function setup() {
  render(
    <Composer
      session={session}
      agent={claudeAgent}
      deviceOnline
      queue={[]}
      question={null}
      sttEnabled={false}
      onSend={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />,
  );
}

const batch = (prefix: string): File[] =>
  Array.from({ length: 5 }, (_, i) => new File(['x'], `${prefix}-${i}.txt`, { type: 'text/plain' }));

const attached = (): number => useDrafts.getState().drafts[KEY]?.attachments.length ?? 0;

beforeEach(() => {
  useDrafts.getState().reset();
});

describe('two attach operations at once', () => {
  it('never puts more than the cap in the draft, and says so once', async () => {
    setup();
    const input = screen.getByLabelText(strings.composer.attach, {
      selector: 'input',
    }) as HTMLInputElement;

    // Both picked before either has finished encoding: each sees an empty draft.
    fireEvent.change(input, { target: { files: batch('paste') } });
    fireEvent.change(input, { target: { files: batch('dialog') } });

    await waitFor(() => expect(attached()).toBe(MAX_ATTACHMENTS));
    expect(
      await screen.findByText(strings.composer.attachTooMany(MAX_ATTACHMENTS)),
    ).toBeInTheDocument();
  });

  it('adds what the second batch has to say to what the first said', async () => {
    setup();
    const input = screen.getByLabelText(strings.composer.attach, {
      selector: 'input',
    }) as HTMLInputElement;
    const huge = new File([new Uint8Array(7 * 1024 * 1024)], 'core.dump', {
      type: 'application/octet-stream',
    });

    fireEvent.change(input, { target: { files: [huge] } });
    await screen.findByText(/core\.dump/);
    fireEvent.change(input, { target: { files: batch('dialog') } });

    await waitFor(() => expect(attached()).toBe(5));
    // The first batch's message is still there beside the new files.
    expect(screen.getByText(/core\.dump/)).toBeInTheDocument();
  });
});

describe('the cap in the store', () => {
  it('reports what it had to drop', () => {
    const files = Array.from({ length: 5 }, (_, i) => ({
      name: `f-${i}.txt`,
      mime: 'text/plain',
      size: 1,
      data_base64: 'eA==',
    }));

    expect(useDrafts.getState().addAttachments(KEY, files)).toBe(0);
    expect(useDrafts.getState().addAttachments(KEY, files)).toBe(2);
    expect(attached()).toBe(MAX_ATTACHMENTS);
  });
});
