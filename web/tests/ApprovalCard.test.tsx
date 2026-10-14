import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApprovalCard } from '../src/features/chat/blocks/ApprovalCard';
import type { ApprovalEvent } from '../src/protocol/types';
import { useSettings } from '../src/stores/settings';
import { fixturesAvailable, readFixture } from './fixtures';

const pending: ApprovalEvent = {
  seq: 12,
  ts: 1_700_000_000_000,
  kind: 'approval',
  block_id: 'ap-1',
  request_id: 'req-1',
  tool: 'Bash',
  tool_kind: 'shell',
  title: 'git commit -am "fix: isolate the auth clock"',
  input: { command: 'git commit -am "fix: isolate the auth clock"' },
  status: 'pending',
  options: [
    { id: 'allow', label: 'Allow once', style: 'primary' },
    { id: 'allow_session', label: 'Allow for session', style: 'secondary' },
    { id: 'deny', label: 'Deny', style: 'danger' },
  ],
};

afterEach(() => {
  useSettings.setState({ language: 'en' });
});

describe('ApprovalCard', () => {
  it('renders exactly the server-supplied options, accept first and reject last', () => {
    render(<ApprovalCard event={pending} onDecide={vi.fn()} />);
    const buttons = screen.getAllByRole('button').map((b) => b.textContent);
    expect(buttons).toEqual(['Allow once', 'Allow for session', 'Deny']);
    expect(screen.getByText(pending.title)).toBeInTheDocument();
  });

  it('sends the opaque option id and disables the row while deciding', async () => {
    const user = userEvent.setup();
    let resolve = (): void => {};
    const onDecide = vi.fn(
      () =>
        new Promise<void>((r) => {
          resolve = r;
        }),
    );
    render(<ApprovalCard event={pending} onDecide={onDecide} />);

    await user.click(screen.getByRole('button', { name: 'Allow for session' }));
    expect(onDecide).toHaveBeenCalledWith('req-1', 'allow_session');
    for (const button of screen.getAllByRole('button')) expect(button).toBeDisabled();

    resolve();
  });

  it('shows who decided and offers no buttons once resolved', () => {
    render(
      <ApprovalCard
        event={{
          ...pending,
          status: 'resolved',
          decision: { option_id: 'deny', by: 'terminal' },
        }}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument();
    expect(screen.getByText(/decided by terminal/)).toBeInTheDocument();
    expect(screen.getByText(/Deny/)).toBeInTheDocument();
  });

  it('says in Chinese who decided, and leaves the option as the device wrote it', () => {
    useSettings.setState({ language: 'zh-Hans' });
    render(
      <ApprovalCard
        event={{ ...pending, status: 'resolved', decision: { option_id: 'deny', by: 'terminal' } }}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByText('Deny · 已在终端决定')).toBeInTheDocument();
  });

  it('marks an expired request instead of letting it be answered', () => {
    render(<ApprovalCard event={{ ...pending, status: 'expired' }} onDecide={vi.fn()} />);
    expect(screen.getByText('This request expired.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Deny' })).not.toBeInTheDocument();
  });

  it.runIf(fixturesAvailable())('renders the canonical approval fixture', () => {
    const event = readFixture<ApprovalEvent>('events/approval.pending.json');
    render(<ApprovalCard event={event} onDecide={vi.fn()} />);
    for (const option of event.options) {
      expect(screen.getByRole('button', { name: option.label })).toBeEnabled();
    }
  });
});
