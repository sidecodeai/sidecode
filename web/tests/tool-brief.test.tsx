/**
 * A60: a page of history carries tool calls brief — the input cut to 1 KiB, no
 * output, no patch — and the row fetches the whole block (`session.block`) the
 * moment the person opens it, once. A device from before A60 sends the page in
 * full, and the row then draws what it holds without asking.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ToolRow } from '../src/features/chat/blocks/ToolRow';
import { strings } from '../src/strings';
import type { ToolCallEvent } from '../src/protocol/types';

afterEach(cleanup);

/** What a brief page carries for a finished shell call. */
const brief: ToolCallEvent = {
  seq: 26,
  ts: 26,
  kind: 'tool_call',
  block_id: 't26',
  tool: 'Bash',
  tool_kind: 'shell',
  title: 'pytest -k refresh',
  status: 'failed',
  input: { command: 'pytest -k refresh' },
  output_truncated: true,
  summary: '2 failed',
  started_at: 1,
  ended_at: 2,
};

/** The same block as `session.block` answers it. */
const whole: ToolCallEvent = {
  ...brief,
  output: 'collected 20 items\n2 failed',
  output_truncated: undefined,
};

/** A brief edit: the patch left out, its path and counts kept. */
const briefEdit: ToolCallEvent = {
  ...brief,
  block_id: 't27',
  tool: 'Edit',
  tool_kind: 'edit',
  title: 'tokens.py',
  status: 'succeeded',
  output_truncated: undefined,
  diff: { path: 'tokens.py', additions: 4, deletions: 2, patch_truncated: true },
};

function setup(event: ToolCallEvent, options: { full?: boolean; onOpenFull?: () => Promise<void> } = {}) {
  const onOpenFull = vi.fn(options.onOpenFull ?? (() => Promise.resolve()));
  const view = render(<ToolRow event={event} full={options.full ?? false} onOpenFull={onOpenFull} />);
  const head = () => screen.getByRole('button', { name: new RegExp(event.tool) });
  return { onOpenFull, view, head };
}

describe('a brief tool row', () => {
  it('can be opened although it holds no output yet', () => {
    const { head } = setup(brief);
    expect(head()).toBeEnabled();
  });

  it('fetches the whole block when it is opened, and says so meanwhile', async () => {
    let finish!: () => void;
    const { onOpenFull, head } = setup(brief, {
      onOpenFull: () => new Promise<void>((resolve) => { finish = resolve; }),
    });

    await userEvent.click(head());

    expect(onOpenFull).toHaveBeenCalledTimes(1);
    expect(onOpenFull).toHaveBeenCalledWith('t26');
    expect(screen.getByText(strings.common.loading)).toBeInTheDocument();
    finish();
  });

  it('draws the output once the whole block is held, and asks no more', async () => {
    const { onOpenFull, view, head } = setup(brief);
    await userEvent.click(head());
    view.rerender(<ToolRow event={whole} full onOpenFull={onOpenFull} />);

    expect(screen.getByText(/2 failed/, { selector: 'pre' })).toBeInTheDocument();

    // Closed and opened again: the block is held.
    await userEvent.click(head());
    await userEvent.click(head());
    expect(onOpenFull).toHaveBeenCalledTimes(1);
  });

  it('fetches a patch a brief page left out', async () => {
    const { onOpenFull, head } = setup(briefEdit);
    await userEvent.click(head());
    expect(onOpenFull).toHaveBeenCalledWith('t27');
  });

  it('offers the fetch again when it did not come back', async () => {
    const { onOpenFull, head } = setup(brief);
    await userEvent.click(head());
    // The page reports the failure itself; the row is still without its output.
    const again = await screen.findByRole('button', { name: strings.chat.openFullOutput });

    await userEvent.click(again);

    expect(onOpenFull).toHaveBeenCalledTimes(2);
  });

  it('asks nothing for a block still running: its next event brings the output', async () => {
    const { onOpenFull, head } = setup({ ...brief, status: 'running', ended_at: undefined });
    await userEvent.click(head());
    expect(onOpenFull).not.toHaveBeenCalled();
  });

  it('asks nothing for a row held in full already', async () => {
    const { onOpenFull, head } = setup(brief, { full: true });
    await userEvent.click(head());
    expect(onOpenFull).not.toHaveBeenCalled();
  });
});

describe('a full page, from a device before A60', () => {
  it('draws the output it holds and asks nothing', async () => {
    const { onOpenFull, head } = setup(whole);
    await userEvent.click(head());

    expect(screen.getByText(/2 failed/, { selector: 'pre' })).toBeInTheDocument();
    expect(onOpenFull).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: strings.chat.openFullOutput })).toBeNull();
  });
});
