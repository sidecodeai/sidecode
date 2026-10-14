import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatusLine } from '../src/features/chat/StatusLine';
import { claudeAgent, codexAgent } from '../mock/fixtures';
import { foldSession } from '../src/stores/chat';
import type { Session, SessionEvent, SessionState } from '../src/protocol/types';

const base: Session = {
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
  model: null,
  permission_mode: null,
  effort: null,
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
};

const show = (session: Partial<Session>, agent = claudeAgent, deviceOnline = true) =>
  render(
    <StatusLine
      session={{ ...base, ...session }}
      agent={agent}
      deviceOnline={deviceOnline}
      onTakeover={vi.fn()}
    />,
  );

describe('StatusLine', () => {
  it('says the terminal is in control while its turn runs (A7)', () => {
    show({ control: 'terminal', state: 'running' } as { control: Session['control']; state: SessionState });
    expect(screen.getByText(/Controlled by the terminal/)).toBeInTheDocument();
    expect(screen.getByText(/a turn is running there/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Take over' })).toBeInTheDocument();
  });

  it('says the terminal is in control when it is idle, and how to write to it', () => {
    show({ control: 'terminal', state: 'readonly' });
    expect(document.querySelector('.status-line')?.textContent).toBe(
      'Controlled by the terminal · take over to sendTake over',
    );
  });

  it('offers no takeover when the agent lacks the capability', () => {
    show({ control: 'terminal', state: 'readonly' }, codexAgent);
    expect(screen.getByText('Controlled by the terminal')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('warns that a message will be queued for a running remote turn', () => {
    show({ state: 'running' });
    expect(screen.getByText('Claude Code is working · your message will be queued')).toBeInTheDocument();
  });

  it('warns that a message will steer a steer-capable agent', () => {
    show({ state: 'running', agent: 'codex' }, codexAgent);
    expect(screen.getByText('Codex is working · your message will steer the turn')).toBeInTheDocument();
  });

  it('says queued, as the button does, while the composer edits a queued message (A43)', () => {
    render(
      <StatusLine
        session={{ ...base, state: 'running', agent: 'codex' }}
        agent={codexAgent}
        deviceOnline
        editingQueued
        onTakeover={vi.fn()}
      />,
    );
    expect(screen.getByText('Codex is working · your message will be queued')).toBeInTheDocument();
  });

  it('reports approvals, questions and errors', () => {
    const { unmount } = show({ state: 'needs_approval' });
    expect(screen.getByText('Needs your approval')).toBeInTheDocument();
    unmount();

    const q = show({ state: 'needs_input' });
    expect(screen.getByText('Waiting for your answer')).toBeInTheDocument();
    q.unmount();

    show({ state: 'error', state_detail: 'the CLI exited with 1' });
    expect(screen.getByText('the CLI exited with 1')).toBeInTheDocument();
  });

  it('reports an offline device before anything else', () => {
    show({ control: 'terminal', state: 'running' }, claudeAgent, false);
    expect(screen.getByText('Device offline')).toBeInTheDocument();
  });

  /**
   * A35, §8 rule 17: a turn the device started once the usage limit reset reads
   * exactly as a turn this app started. Nothing about the line says a machine
   * sent it — the caption on the bubble above already does.
   */
  it('reads a turn a resume started the way it reads a remote one', () => {
    const started: SessionEvent = {
      seq: 43,
      ts: 3,
      kind: 'turn_started',
      turn_id: 'resumed-turn',
      trigger: 'resume',
    };
    const running = foldSession({ ...base, state: 'running' }, [started]);

    expect(running.turn?.turn_id).toBe('resumed-turn');
    show(running);

    expect(
      screen.getByText('Claude Code is working \u00b7 your message will be queued'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/terminal/i)).toBeNull();
  });
});
