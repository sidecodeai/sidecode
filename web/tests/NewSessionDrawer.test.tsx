/**
 * The New session drawer. The device picker is a Popover, which renders its
 * panel in a portal on `document.body` — a sibling of the drawer overlay rather
 * than a descendant. It has to open, stay open, and not take the drawer down
 * with it when the user clicks inside it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { NewSessionDrawer } from '../src/features/sessions/NewSessionDrawer';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { strings } from '../src/strings';
import { rpc } from '../src/lib/gateway';
import { useSettings } from '../src/stores/settings';
import { claudeAgent, codexAgent, devices } from '../mock/fixtures';
import { labelled } from './choice';

vi.mock('../src/lib/gateway', () => ({
  rpc: vi.fn(async (method: string, params: Record<string, unknown>) => {
    if (method === 'device.dirs') return { path: '/Users/me', parent: null, entries: [], recent: [] };
    if (method === 'device.git') return { git: null };
    if (method === 'session.create') {
      // Only the fields the drawer navigates on; the store keeps the rest.
      return { session: { ...params, session_id: 'ses-new' } };
    }
    throw new Error(`unexpected ${method}`);
  }),
}));

const onClose = vi.fn();

beforeEach(() => {
  onClose.mockReset();
  vi.mocked(rpc).mockClear();
  useDevices.setState({ devices, loaded: true, error: null });
  useSessions.setState({ sessions: {}, loaded: true, agentFilter: null });
});

const renderDrawer = () =>
  render(
    <MemoryRouter>
      <NewSessionDrawer open onClose={onClose} />
    </MemoryRouter>,
  );

describe('NewSessionDrawer', () => {
  it('opens the device menu above the drawer and selects from it', async () => {
    renderDrawer();

    const trigger = screen.getByRole('button', { name: labelled(strings.newSession.device) });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const menu = screen.getByRole('listbox', { name: strings.newSession.device });
    // The panel is portalled, so it is outside the drawer's own subtree.
    expect(screen.getByRole('dialog').contains(menu)).toBe(false);

    const second = devices[1]!;
    await userEvent.click(screen.getByRole('option', { name: new RegExp(second.name) }));

    // The drawer survived the click inside the portalled panel.
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText(second.name)).toBeInTheDocument();
  });

  // The Mac's picker reads the same: the field, then the device and its state.
  it('reads the device it is on and whether it is online, and marks that row', async () => {
    renderDrawer();

    const trigger = screen.getByRole('button', { name: 'Device, mac-studio-office, online' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'listbox');
    await userEvent.click(trigger);
    const rows = screen.getAllByRole('option');
    expect(rows.map((row) => row.getAttribute('aria-selected'))).toEqual(['true', 'false']);
    expect(rows[0]).toHaveTextContent('mac-studio-office');

    await userEvent.click(rows[1]!);
    expect(trigger).toHaveAccessibleName('Device, ci-runner-01, online');
  });

  it('closes an open list alone on Escape, keeping the drawer, the choice and the focus', async () => {
    renderDrawer();
    const model = screen.getByRole('button', { name: labelled(strings.newSession.model) });
    await userEvent.click(model);
    await userEvent.click(screen.getByRole('option', { name: 'Opus 4.1' }));
    await userEvent.click(model);
    expect(screen.getByRole('listbox', { name: strings.newSession.model })).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(model).toHaveAccessibleName('Model, Opus 4.1');
    expect(model).toHaveFocus();

    // The next Escape is the drawer's.
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('reads it in the interface language', () => {
    useSettings.setState({ language: 'zh-Hans' });
    try {
      renderDrawer();
      expect(screen.getByRole('button', { name: '设备：mac-studio-office，在线' })).toBeInTheDocument();
    } finally {
      useSettings.setState({ language: 'en' });
    }
  });

  it('labels its fields in sentence case', () => {
    renderDrawer();

    for (const label of [
      strings.newSession.device,
      strings.newSession.agent,
      strings.newSession.workingDirectory,
      strings.newSession.git,
    ]) {
      expect(label).toBe(label.charAt(0) + label.slice(1).toLowerCase());
    }
    expect(screen.getByText(strings.newSession.workingDirectory)).toBeInTheDocument();
  });

  it('offers no first-message field', () => {
    renderDrawer();

    expect(screen.queryByRole('textbox', { name: /message/i })).not.toBeInTheDocument();
    expect(document.querySelector('textarea')).toBeNull();
  });
});

/**
 * A21 — the forms keep list pickers: Model, Effort, Permissions in that order,
 * with the speed switch after the three when the agent offers one
 * (`docs/DESIGN.md` § "The composer", "The model card").
 */
describe('NewSessionDrawer options', () => {
  const labels = () => [...document.querySelectorAll('.label')].map((el) => el.textContent);

  it('lists Model, Effort and Permissions in that order', () => {
    renderDrawer();

    const order = labels();
    const model = order.indexOf(strings.newSession.model);
    expect(model).toBeGreaterThan(order.indexOf(strings.newSession.agent));
    expect(order.indexOf(strings.newSession.effort)).toBe(model + 1);
    expect(order.indexOf(strings.newSession.permissions)).toBe(model + 2);
    expect(order.indexOf(strings.newSession.workingDirectory)).toBeGreaterThan(model + 2);
  });

  it("starts at the agent's own defaults", () => {
    renderDrawer();

    expect(screen.getByRole('button', { name: 'Model, Sonnet 4.5' })).toHaveTextContent('Sonnet 4.5');
    expect(screen.getByRole('button', { name: 'Permissions, Auto-accept edits' })).toHaveTextContent(
      'Auto-accept edits',
    );
  });

  it('offers no speed switch for an agent that lists no tiers', () => {
    renderDrawer();

    expect(claudeAgent.speeds).toBeUndefined();
    expect(screen.queryByRole('switch', { name: strings.newSession.speed })).not.toBeInTheDocument();
  });

  it('puts the speed switch after the three lists for Codex, and sends the tier', async () => {
    renderDrawer();
    await userEvent.click(screen.getByRole('button', { name: /Codex/ }));

    const order = labels();
    expect(order.indexOf(strings.newSession.speed)).toBe(
      order.indexOf(strings.newSession.permissions) + 1,
    );

    const speed = screen.getByRole('switch', { name: strings.newSession.speed });
    expect(speed).toHaveAttribute('aria-checked', 'false');
    await userEvent.click(speed);
    await userEvent.click(screen.getByRole('button', { name: new RegExp(strings.newSession.start) }));

    expect(vi.mocked(rpc)).toHaveBeenCalledWith(
      'session.create',
      expect.objectContaining({
        agent: 'codex',
        model: codexAgent.default_model,
        effort: codexAgent.default_effort,
        permission_mode: codexAgent.default_permission_mode,
        speed: 'priority',
      }),
    );
  });

  it('omits the tier when the switch is off, because null is the standard speed', async () => {
    renderDrawer();
    await userEvent.click(screen.getByRole('button', { name: /Codex/ }));
    await userEvent.click(screen.getByRole('button', { name: new RegExp(strings.newSession.start) }));

    const call = vi.mocked(rpc).mock.calls.find(([method]) => method === 'session.create');
    expect(call?.[1]).not.toHaveProperty('speed');
  });
});
