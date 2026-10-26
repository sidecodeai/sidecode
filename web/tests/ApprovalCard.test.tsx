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

describe('ApprovalCard for a plan (A54)', () => {
  const plan: ApprovalEvent = {
    seq: 31,
    ts: 1_700_000_000_000,
    kind: 'approval',
    block_id: 'ap-plan',
    request_id: 'req-plan',
    tool: 'ExitPlanMode',
    tool_kind: 'other',
    title: 'Plan ready: Add retries',
    approval_kind: 'plan',
    input: { plan: '# Add retries\n\n- wrap the client\n' },
    status: 'pending',
    options: [
      { id: 'plan_accept_edits', label: 'Yes, auto-accept edits', style: 'primary' },
      { id: 'plan_manual', label: 'Yes, manually approve edits', style: 'secondary' },
      { id: 'plan_keep', label: 'No, keep planning', style: 'danger' },
    ],
  };

  it('shows the plan and the three choices, accept first', async () => {
    render(<ApprovalCard event={plan} onDecide={vi.fn()} />);
    expect(await screen.findByText('wrap the client')).toBeInTheDocument();
    const buttons = screen.getAllByRole('button').map((b) => b.textContent);
    expect(buttons).toEqual([
      'Yes, auto-accept edits',
      'Yes, manually approve edits',
      'No, keep planning',
    ]);
  });

  it('sends the approval option id alone', async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn(() => Promise.resolve());
    render(<ApprovalCard event={plan} onDecide={onDecide} />);
    await user.click(screen.getByRole('button', { name: 'Yes, manually approve edits' }));
    expect(onDecide).toHaveBeenCalledWith('req-plan', 'plan_manual', undefined);
  });

  it('asks what to change before keeping on planning, and sends it as the message', async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn(() => Promise.resolve());
    render(<ApprovalCard event={plan} onDecide={onDecide} />);
    await user.click(screen.getByRole('button', { name: 'No, keep planning' }));
    expect(onDecide).not.toHaveBeenCalled();
    await user.type(screen.getByRole('textbox'), '  use a queue ');
    await user.click(screen.getByRole('button', { name: 'Send feedback' }));
    expect(onDecide).toHaveBeenCalledWith('req-plan', 'plan_keep', 'use a queue');
  });

  it('keeps on planning with no message when none is typed', async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn(() => Promise.resolve());
    render(<ApprovalCard event={plan} onDecide={onDecide} />);
    await user.click(screen.getByRole('button', { name: 'No, keep planning' }));
    await user.click(screen.getByRole('button', { name: 'Send feedback' }));
    expect(onDecide).toHaveBeenCalledWith('req-plan', 'plan_keep', undefined);
  });

  it('folds a long plan until asked to show it whole', async () => {
    const user = userEvent.setup();
    const long = { ...plan, input: { plan: Array.from({ length: 40 }, (_, i) => `- step ${i}`).join('\n') } };
    render(<ApprovalCard event={long} onDecide={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Show full plan' }));
    expect(screen.getByRole('button', { name: 'Collapse plan' })).toBeInTheDocument();
  });

  it('says who decided once resolved and offers no buttons', () => {
    render(
      <ApprovalCard
        event={{ ...plan, status: 'resolved', decision: { option_id: 'plan_accept_edits', by: 'remote' } }}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: 'No, keep planning' })).toBeNull();
    expect(screen.getByText(/Yes, auto-accept edits/)).toBeInTheDocument();
  });
});

describe('plan card in Chinese (A54)', () => {
  it('names the plan options in the reader\'s language and sends the same ids', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    const user = userEvent.setup();
    const onDecide = vi.fn(() => Promise.resolve());
    render(
      <ApprovalCard
        event={{
          seq: 1,
          ts: 1,
          kind: 'approval',
          block_id: 'b',
          request_id: 'r',
          tool: 'ExitPlanMode',
          title: 'Plan ready: x',
          approval_kind: 'plan',
          input: { plan: 'x' },
          status: 'pending',
          options: [
            { id: 'plan_accept_edits', label: 'Yes, auto-accept edits', style: 'primary' },
            { id: 'plan_keep', label: 'No, keep planning', style: 'danger' },
          ],
        }}
        onDecide={onDecide}
      />,
    );
    await user.click(screen.getByRole('button', { name: '是，自动接受编辑' }));
    expect(onDecide).toHaveBeenCalledWith('r', 'plan_accept_edits', undefined);
  });
});
