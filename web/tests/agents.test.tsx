/**
 * A25/A26 — the four agents a device can report, and the shape the app must
 * still handle: an agent that lists nothing for a setting.
 * `docs/DESIGN.md` § "Agents": every agent is named, each is marked by its own
 * logo, and what an agent does not have is not drawn.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { AgentLogo } from '../src/components/AgentLogo';
import { Composer } from '../src/features/chat/Composer';
import { StatusLine } from '../src/features/chat/StatusLine';
import { NewSessionDrawer } from '../src/features/sessions/NewSessionDrawer';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { agentLabel, strings } from '../src/strings';
import { rpc } from '../src/lib/gateway';
import { claudeAgent, devices, grokAgent, piAgent, sessions } from '../mock/fixtures';
import { fixturesAvailable, readFixture } from './fixtures';
import type { AgentInfo, Session } from '../src/protocol/types';

vi.mock('../src/lib/gateway', () => ({
  rpc: vi.fn(async (method: string, params: Record<string, unknown>) => {
    if (method === 'device.dirs') return { path: '/Users/me', parent: null, entries: [], recent: [] };
    if (method === 'device.git') return { git: null };
    if (method === 'session.create') return { session: { ...params, session_id: 'ses-new' } };
    throw new Error(`unexpected ${method}`);
  }),
}));

const sessionOf = (id: string): Session => {
  const found = sessions.find((s) => s.session_id === id);
  if (!found) throw new Error(`no such mock session ${id}`);
  return found;
};

/**
 * An agent no shipped device reports: nobody knows its id, it has no efforts
 * and no permission modes. `docs/DESIGN.md` § "Agents" — an absent control is
 * what an agent without that setting gets, and the mark falls back to a letter.
 */
const sparseAgent: AgentInfo = {
  agent: 'aider',
  available: true,
  version: '0.9.0',
  path: '/usr/local/bin/aider',
  models: [{ id: 'auto', label: 'Auto' }],
  default_model: 'auto',
  permission_modes: [],
  default_permission_mode: null,
  efforts: [],
  default_effort: null,
  capabilities: ['history'],
  attach: null,
  attach_ready: false,
  shared_interrupt: false,
  shared_settings: false,
  shared_attachments: false,
};

/** The logo the component drew, or the letter it fell back to. */
const logoOf = (agent: string): { tag: string; text: string } => {
  const { container } = render(<AgentLogo agent={agent} />);
  const el = container.querySelector('.agent-logo');
  if (!el) throw new Error(`no logo for ${agent}`);
  return { tag: el.tagName.toLowerCase(), text: el.textContent ?? '' };
};

describe('agent names and logos', () => {
  it('names every agent the device can report', () => {
    expect(agentLabel('claude')).toBe('Claude Code');
    expect(agentLabel('codex')).toBe('Codex');
    expect(agentLabel('grok')).toBe('Grok Build');
    expect(agentLabel('pi')).toBe('pi');
  });

  it('draws each of them as its own logo', () => {
    for (const agent of ['claude', 'codex', 'grok', 'pi']) {
      const logo = logoOf(agent);
      expect(logo.tag).toBe('svg');
      expect(logo.text).toBe('');
    }
  });

  it('renders an agent nobody knows as itself, marked by its first letter', () => {
    expect(agentLabel('aider')).toBe('aider');
    expect(logoOf('aider')).toEqual({ tag: 'span', text: 'A' });
  });

  it('withdrew Cursor, which is now an id nobody knows (A26)', () => {
    expect(agentLabel('cursor')).toBe('cursor');
    expect(logoOf('cursor')).toEqual({ tag: 'span', text: 'C' });
  });

  it('names the working agent in the status line', () => {
    render(
      <StatusLine
        session={{ ...sessionOf('ses-pi'), state: 'running' }}
        agent={piAgent}
        deviceOnline
        onTakeover={vi.fn()}
      />,
    );
    expect(screen.getByText(strings.status.workingSteer('pi'))).toBeInTheDocument();
  });
});

describe.runIf(fixturesAvailable())('the mock advertises what the contract does', () => {
  it.each([
    ['grok', grokAgent],
    ['pi', piAgent],
  ])('matches objects/agent.%s.json', (id, mocked) => {
    expect(mocked).toEqual(readFixture<AgentInfo>(`objects/agent.${id}.json`));
    // A27: both agents carry the capability the panel is drawn from.
    expect(mocked.capabilities).toContain('commands');
  });
});

const renderDrawer = () => {
  useDevices.setState({ devices, loaded: true, error: null });
  useSessions.setState({ sessions: {}, loaded: true, agentFilter: null });
  render(
    <MemoryRouter>
      <NewSessionDrawer open onClose={vi.fn()} />
    </MemoryRouter>,
  );
};

const rowLabels = () => [...document.querySelectorAll('.label')].map((el) => el.textContent);

describe('the new-session form draws what the agent has', () => {
  it('offers all four agents, each named though it draws only its logo', () => {
    renderDrawer();

    const control = screen.getByRole('group', { name: strings.newSession.agent });
    const logos = [...control.querySelectorAll('.agent-logo')].map((el) =>
      el.getAttribute('data-agent'),
    );
    expect(logos).toEqual(['claude', 'codex', 'grok', 'pi']);
    expect(control.textContent).toBe('');
    for (const name of ['Claude Code', 'Codex', 'Grok Build', 'pi']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
  });

  it('names the chosen agent under the row', async () => {
    renderDrawer();
    await userEvent.click(screen.getByRole('button', { name: 'Grok Build' }));

    expect(document.querySelector('.agent-line')?.textContent).toContain('Grok Build');
    expect(document.querySelector('.agent-line')?.textContent).toContain('grok-4.6');
  });

  it('draws a permission row for pi, whose modes the extension enforces (A26)', async () => {
    renderDrawer();
    await userEvent.click(screen.getByRole('button', { name: 'pi' }));

    expect(piAgent.permission_modes.map((mode) => mode.id)).toEqual([
      'untrusted',
      'on-request',
      'never',
    ]);
    expect(rowLabels()).toContain(strings.newSession.permissions);
    expect(
      screen.getByRole('button', { name: strings.newSession.permissions }),
    ).toHaveTextContent('Ask when needed');
    expect(rowLabels()).toContain(strings.newSession.model);
    expect(rowLabels()).toContain(strings.newSession.effort);
  });

  it('starts a pi session on the mode the device defaults to', async () => {
    renderDrawer();
    await userEvent.click(screen.getByRole('button', { name: 'pi' }));
    await userEvent.click(screen.getByRole('button', { name: new RegExp(strings.newSession.start) }));

    const call = vi.mocked(rpc).mock.calls.find(([method]) => method === 'session.create');
    expect(call?.[1]).toMatchObject({
      agent: 'pi',
      effort: 'medium',
      permission_mode: 'on-request',
    });
  });

  it('draws no effort or permission row for an agent that lists neither', () => {
    const device = devices[0];
    if (!device) throw new Error('no mock device');
    useDevices.setState({ devices: [{ ...device, agents: [sparseAgent] }], loaded: true, error: null });
    useSessions.setState({ sessions: {}, loaded: true, agentFilter: null });
    render(
      <MemoryRouter>
        <NewSessionDrawer open onClose={vi.fn()} />
      </MemoryRouter>,
    );

    expect(rowLabels()).not.toContain(strings.newSession.effort);
    expect(rowLabels()).not.toContain(strings.newSession.permissions);
    expect(rowLabels()).toContain(strings.newSession.model);
    // Nobody knows the id, so the control marks it with its own first letter.
    expect(document.querySelector('.agent-logo')?.textContent).toBe('A');
  });
});

const renderComposer = (session: Session, agent: AgentInfo) =>
  render(
    <Composer
      session={session}
      agent={agent}
      deviceOnline
      queue={[]}
      sttEnabled={false}
      question={null}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSend={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />,
  );

describe('the composer draws what the agent has', () => {
  it('offers the permission picker on a pi session (A26)', () => {
    renderComposer(sessionOf('ses-pi'), piAgent);

    expect(screen.getByRole('button', { name: strings.composer.permissionMode })).toHaveTextContent(
      'Ask when needed',
    );
    expect(document.querySelector('.composer-chip.readonly')).toBeNull();
    expect(screen.getByRole('button', { name: strings.composer.modelCard })).toHaveTextContent(
      'Claude Sonnet 4.5 High',
    );
  });

  it('reads the model alone for an agent with no efforts, and opens a card with no slider', async () => {
    renderComposer({ ...sessionOf('ses-pi'), agent: 'aider', model: 'auto', effort: null }, sparseAgent);

    const chip = screen.getByRole('button', { name: strings.composer.modelCard });
    expect(chip).toHaveTextContent('Auto');
    expect(document.querySelector('.sized-box-shown [data-effort-word]')).toBeNull();

    await userEvent.click(chip);
    expect(screen.queryByRole('slider', { name: strings.composer.effort })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: strings.composer.permissionMode }),
    ).not.toBeInTheDocument();
  });

  it('writes nothing on a terminal-held Grok session', () => {
    renderComposer(sessionOf('ses-grok-terminal'), grokAgent);

    expect(screen.getByLabelText(strings.composer.placeholder)).toBeDisabled();
    // A25: Grok cannot be taken over, so the bar only says who holds it.
    expect(grokAgent.capabilities).not.toContain('takeover');
    expect(screen.queryByRole('button', { name: strings.chat.takeOver })).not.toBeInTheDocument();
  });
});

/**
 * `docs/DESIGN.md` § "The composer": a held session's field only says who holds
 * it. Taking it over is the status line's clause, drawn where the button is.
 */
describe('taking over is offered where the agent can be taken over', () => {
  const placeholderOf = () =>
    screen.getByLabelText(strings.composer.placeholder).getAttribute('placeholder');

  const renderStatus = (id: string, agent: AgentInfo) => {
    render(<StatusLine session={sessionOf(id)} agent={agent} deviceOnline onTakeover={vi.fn()} />);
    return document.querySelector('.status-line')?.textContent ?? '';
  };

  it('keeps the field to who holds it on an agent that can be taken over', () => {
    expect(claudeAgent.capabilities).toContain('takeover');
    renderComposer(sessionOf('ses-terminal'), claudeAgent);

    expect(placeholderOf()).toBe(strings.composer.placeholderTerminal);
  });

  it('keeps the field to who holds it on one that cannot', () => {
    expect(grokAgent.capabilities).not.toContain('takeover');
    renderComposer(sessionOf('ses-grok-terminal'), grokAgent);

    expect(placeholderOf()).toBe(strings.composer.placeholderTerminal);
  });

  it('adds the clause to the status line where the agent can be taken over', () => {
    const line = renderStatus('ses-terminal', claudeAgent);

    expect(line).toContain(strings.status.terminalControlled);
    expect(line).toContain(strings.status.takeOverToSend);
    expect(screen.getByRole('button', { name: strings.chat.takeOver })).toBeInTheDocument();
  });

  it('leaves the clause off where it cannot', () => {
    const line = renderStatus('ses-grok-terminal', grokAgent);

    expect(line).toBe(strings.status.terminalControlled);
    expect(screen.queryByRole('button', { name: strings.chat.takeOver })).not.toBeInTheDocument();
  });
});
