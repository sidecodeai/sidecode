/**
 * A27 — the terminal's `/` menu in the composer: what opens it, what it draws,
 * what the keyboard does with it, and which of Send's two destinations a draft
 * reaches.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from '../src/features/chat/Composer';
import { strings } from '../src/strings';
import { claudeNoShim, codexAgent, commandsFor, piAgent } from '../mock/fixtures';
import type { AgentInfo, Command, Session, SessionState } from '../src/protocol/types';

const baseSession: Session = {
  session_id: 'ses-codex',
  device_id: 'dev-mac',
  agent: 'codex',
  title: 'Typecheck the web app',
  cwd: '/Users/me/dev/remote-control/web',
  git: null,
  state: 'idle',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'gpt-5.4-codex',
  permission_mode: 'on-request',
  effort: 'medium',
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
};

function setup(
  overrides: {
    state?: SessionState;
    agent?: AgentInfo;
    commands?: Command[];
    control?: Session['control'];
  } = {},
) {
  const agent = overrides.agent ?? codexAgent;
  const onSend = vi.fn().mockResolvedValue(undefined);
  const onRunCommand = vi.fn().mockResolvedValue(undefined);
  const onCommandsNeeded = vi.fn();
  render(
    <Composer
      session={{
        ...baseSession,
        agent: agent.agent,
        ...(overrides.state ? { state: overrides.state } : {}),
        ...(overrides.control ? { control: overrides.control } : {}),
      }}
      agent={agent}
      deviceOnline
      queue={[]}
      question={null}
      sttEnabled={false}
      commands={overrides.commands ?? commandsFor(agent.agent)}
      onSend={onSend}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
      onCommandsNeeded={onCommandsNeeded}
      onRunCommand={onRunCommand}
    />,
  );
  return { onSend, onRunCommand, onCommandsNeeded, user: userEvent.setup() };
}

const field = () => screen.getByLabelText(strings.composer.placeholder);
const panel = () => screen.queryByRole('listbox', { name: strings.commands.menu });
const rowNames = () =>
  screen.getAllByRole('option').map((row) => row.querySelector('.command-name')?.textContent);

describe('opening the panel', () => {
  it('draws the whole list when `/` is typed, and asks the device again', async () => {
    const { user, onCommandsNeeded } = setup();
    await user.click(field());
    await user.keyboard('/');
    expect(panel()).toBeInTheDocument();
    expect(rowNames()).toEqual(commandsFor('codex').map((c) => `/${c.name}`));
    expect(onCommandsNeeded).toHaveBeenCalledTimes(1);
  });

  it('draws nothing for an agent without the capability', async () => {
    // A40: a Claude the shim never attached has no terminal to type into, so
    // it is the agent with no command surface at all.
    const { user, onCommandsNeeded } = setup({ agent: claudeNoShim, commands: [] });
    expect(claudeNoShim.capabilities).not.toContain('commands');
    await user.click(field());
    await user.keyboard('/compact');
    expect(panel()).not.toBeInTheDocument();
    // The keystroke still reports itself; the page is what gates on capability.
    expect(onCommandsNeeded).toHaveBeenCalled();
  });

  it('draws nothing while the answer is empty', async () => {
    const { user } = setup({ commands: [] });
    await user.click(field());
    await user.keyboard('/');
    expect(panel()).not.toBeInTheDocument();
  });

  it('stays shut for ordinary text that happens to hold a slash', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('look at src/lib/ws.ts');
    expect(panel()).not.toBeInTheDocument();
  });

  it('closes once the first word is followed by a space', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/review ');
    expect(panel()).not.toBeInTheDocument();
    // …and the argument placeholder takes its place as the field's hint.
    expect(screen.getByRole('note')).toHaveTextContent('instructions');
  });
});

describe('filtering', () => {
  it('narrows by prefix of the name as more letters are typed', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/s');
    expect(rowNames()).toEqual(['/status', '/skills']);
    await user.keyboard('ki');
    expect(rowNames()).toEqual(['/skills']);
  });

  it('closes the panel when nothing matches', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/zz');
    expect(panel()).not.toBeInTheDocument();
  });

  it('sections the list only when the agent has more than one group', async () => {
    const { user } = setup({ agent: piAgent });
    await user.click(field());
    await user.keyboard('/');
    for (const group of ['Built-in', 'Prompts', 'Skills', 'Extensions']) {
      expect(screen.getByRole('group', { name: group })).toBeInTheDocument();
    }
  });

  it('draws no group header for an agent with one source', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/');
    expect(screen.queryAllByRole('group')).toHaveLength(0);
  });
});

describe('the keyboard', () => {
  const selected = () =>
    screen.getAllByRole('option').find((row) => row.getAttribute('aria-selected') === 'true');

  it('starts on the first row and moves with the arrows', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/');
    expect(selected()).toHaveTextContent('/compact');
    await user.keyboard('{ArrowDown}');
    expect(selected()).toHaveTextContent('/review');
    await user.keyboard('{ArrowUp}');
    expect(selected()).toHaveTextContent('/compact');
    // The highlight wraps, so the last row is one press away from the first.
    await user.keyboard('{ArrowUp}');
    expect(selected()).toHaveTextContent('/mcp');
  });

  it('names the highlighted row as the field’s active descendant', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/');
    expect(field()).toHaveAttribute('aria-activedescendant', selected()?.id);
  });

  it('takes the highlighted row with Tab, leaving a space for the argument', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/rev{Tab}');
    expect(field()).toHaveValue('/review ');
  });

  it('takes the row with Enter and runs it with the second Enter', async () => {
    const { user, onRunCommand, onSend } = setup();
    await user.click(field());
    await user.keyboard('/comp{Enter}');
    // `compact` takes no argument, so nothing follows the name…
    expect(field()).toHaveValue('/compact');
    expect(onRunCommand).not.toHaveBeenCalled();
    // …and the next Enter is the one that runs it — a moment later, since a
    // plain Enter waits out a possible compositionend before it acts.
    await user.keyboard('{Enter}');
    await waitFor(() => expect(onRunCommand).toHaveBeenCalledWith('compact', undefined));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('closes with Esc and leaves the draft alone', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/comp{Escape}');
    expect(panel()).not.toBeInTheDocument();
    expect(field()).toHaveValue('/comp');
    // Typing brings it back: Esc puts it away, it does not turn it off.
    await user.keyboard('a');
    expect(panel()).toBeInTheDocument();
  });

  it('takes a row with the mouse', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('/');
    await user.click(screen.getByRole('option', { name: /\/review/ }));
    expect(field()).toHaveValue('/review ');
  });
});

describe('sending', () => {
  it('runs a listed command instead of sending it as a message', async () => {
    const { user, onRunCommand, onSend } = setup();
    await user.click(field());
    await user.keyboard('/review focus on the retry logic');
    await user.click(screen.getByRole('button', { name: strings.composer.send }));
    expect(onRunCommand).toHaveBeenCalledWith('review', 'focus on the retry logic');
    expect(onSend).not.toHaveBeenCalled();
    expect(field()).toHaveValue('');
  });

  it('sends anything that matches no command as text', async () => {
    const { user, onRunCommand, onSend } = setup();
    await user.click(field());
    await user.keyboard('/nonesuch do the thing');
    await user.click(screen.getByRole('button', { name: strings.composer.send }));
    expect(onSend).toHaveBeenCalledWith('/nonesuch do the thing', [], 'auto');
    expect(onRunCommand).not.toHaveBeenCalled();
  });

  it('reports a refusal under the field and keeps the draft', async () => {
    const { user, onRunCommand } = setup();
    onRunCommand.mockRejectedValueOnce(new Error('wait for the turn to finish'));
    await user.click(field());
    await user.keyboard('/compact');
    await user.click(screen.getByRole('button', { name: strings.composer.send }));
    expect(await screen.findByRole('alert')).toHaveTextContent('wait for the turn to finish');
    expect(field()).toHaveValue('/compact');
  });
});

describe('while a turn is running', () => {
  it('dims the rows and says when the commands come back', async () => {
    const { user } = setup({ state: 'running' });
    await user.click(field());
    await user.keyboard('/');
    expect(screen.getByText(strings.commands.whileRunning)).toBeInTheDocument();
    expect(screen.getAllByRole('option')[0]).toHaveClass('dim');
  });

  it('refuses to send one, in the footer’s own words', async () => {
    const { user, onRunCommand, onSend } = setup({ state: 'running' });
    await user.click(field());
    await user.keyboard('/compact');
    await user.click(screen.getByRole('button', { name: strings.composer.send }));
    expect(onRunCommand).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(strings.commands.whileRunning);
    expect(field()).toHaveValue('/compact');
  });
});
