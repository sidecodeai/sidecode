/**
 * The device row menu. The list is one soft surface that clips its rounded
 * corners, so the panel has to leave that subtree entirely: it is rendered in
 * a portal on `document.body` and placed in viewport coordinates.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { DevicesPage } from '../src/features/devices/DevicesPage';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { agentLabel, platformLabel, strings } from '../src/strings';
import { devices } from '../mock/fixtures';

beforeEach(() => {
  useDevices.setState({ devices: [...devices], loaded: true, error: null });
  useSessions.setState({ sessions: {}, loaded: true });
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <DevicesPage />
    </MemoryRouter>,
  );

const openFirstMenu = async () => {
  const triggers = screen.getAllByRole('button', { name: strings.a11y.openMenu });
  await userEvent.click(triggers[0]!);
  return screen.getByRole('menu');
};

describe('device row menu', () => {
  it('renders the panel on the body, outside the clipping list surface', async () => {
    const { container } = renderPage();

    const panel = (await openFirstMenu()).closest('.popover-panel');

    expect(panel).not.toBeNull();
    expect(panel?.parentElement).toBe(document.body);
    expect(container.querySelector('.device-list')?.contains(panel!)).toBe(false);
  });

  it('places the panel against the viewport rather than against its trigger', async () => {
    renderPage();

    const panel = (await openFirstMenu()).closest('.popover-panel') as HTMLElement;

    // Written by the layout effect from the trigger's viewport rect. Anchoring
    // to the trigger instead is what let a list surface clip the panel.
    expect(panel.style.left).not.toBe('');
    expect(panel.style.top === '' && panel.style.bottom === '').toBe(false);
  });

  it('still routes a click inside the portal to the item it landed on', async () => {
    renderPage();
    await openFirstMenu();

    await userEvent.click(screen.getByRole('menuitem', { name: strings.common.rename }));

    expect(screen.getByText(strings.devices.renameTitle)).toBeInTheDocument();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    renderPage();
    await openFirstMenu();

    await userEvent.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

/**
 * A38 rule 20 — `docs/DESIGN.md` § "A device has a page, and a device row opens
 * a terminal": the row's own tap opens a shell on the machine, and everything
 * else moved into the menu, in one order.
 */
describe('a row opens a terminal', () => {
  const rowFor = (name: string): HTMLElement => {
    const row = screen.getByText(name).closest('.device-row');
    if (!row) throw new Error(`no row for ${name}`);
    return row as HTMLElement;
  };

  it('links a row that can give a shell to the terminal', () => {
    renderPage();

    const link = screen.getByRole('link', { name: 'mac-studio-office' });
    expect(link).toHaveAttribute('href', '/devices/dev-mac/terminal');
    // Stretched over the row, so the whole row is the link's target.
    expect(link.closest('.device-row')).not.toBeNull();
  });

  it('opens nothing on a device whose client offers no terminal, and says so', async () => {
    renderPage();

    const row = rowFor('ci-runner-01');
    expect(within(row).queryByRole('link', { name: 'ci-runner-01' })).toBeNull();
    await userEvent.click(within(row).getByRole('button', { name: 'ci-runner-01' }));

    expect(within(row).getByText(strings.devices.noTerminal)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/');
  });

  it('says the device is offline when that is the reason', async () => {
    useDevices.setState({
      devices: devices.map((d) =>
        d.device_id === 'dev-mac' ? { ...d, online: false } : d,
      ),
      loaded: true,
      error: null,
    });
    renderPage();

    const row = rowFor('mac-studio-office');
    await userEvent.click(within(row).getByRole('button', { name: 'mac-studio-office' }));

    expect(within(row).getByText(strings.devices.deviceOffline)).toBeInTheDocument();
  });

  it('orders the menu Rename · Show quota · Revoke, and Show quota opens the page', async () => {
    renderPage();

    const menu = await openFirstMenu();
    const labels = [...menu.querySelectorAll('.menu-label')].map((n) => n.textContent);
    expect(labels).toEqual([
      strings.common.rename,
      strings.devices.showQuota,
      strings.common.revoke,
    ]);
    expect(screen.getByRole('menuitem', { name: strings.devices.showQuota })).toHaveAttribute(
      'href',
      '/devices/dev-mac',
    );
  });

  it('does not navigate when the menu is taken', async () => {
    renderPage();

    await openFirstMenu();

    expect(window.location.pathname).toBe('/');
    expect(screen.getByRole('menuitem', { name: strings.common.rename })).toBeInTheDocument();
  });
});

/**
 * `docs/DESIGN.md` § "The device row": a computer glyph at the leading edge,
 * the name once, a status line of dot, word and platform word, and the agents
 * as logos alone. The hostname and the architecture are the device page's.
 */
describe('the platform as a word', () => {
  it('writes the two platforms the way their makers do', () => {
    expect(platformLabel('macos')).toBe('macOS');
    expect(platformLabel('linux')).toBe('Linux');
  });

  it('prints a platform nobody knows as the device sent it', () => {
    expect(platformLabel('freebsd')).toBe('freebsd');
  });
});

describe('what a device row says', () => {
  const rowOf = (name: string): HTMLElement => {
    const row = screen.getByText(name).closest('.device-row');
    if (!row) throw new Error(`no row for ${name}`);
    return row as HTMLElement;
  };

  it('draws one computer glyph on every row, whatever the platform', () => {
    const { container } = renderPage();

    expect(container.querySelectorAll('.device-glyph')).toHaveLength(devices.length);
    for (const device of devices) {
      expect(rowOf(device.name).querySelector('.device-glyph')).not.toBeNull();
    }
  });

  it('draws the glyph as a minimal outline laptop: a rounded screen over one base line', () => {
    const { container } = renderPage();

    const glyph = container.querySelector('.device-glyph');
    if (!glyph) throw new Error('no glyph');
    expect(glyph.tagName.toLowerCase()).toBe('svg');
    expect(glyph.getAttribute('fill')).toBe('none');
    expect(glyph.getAttribute('stroke-linecap')).toBe('round');
    expect(glyph.getAttribute('stroke-linejoin')).toBe('round');
    expect(glyph.getAttribute('aria-hidden')).toBe('true');
    const screen_ = glyph.querySelector('rect');
    expect(screen_).not.toBeNull();
    expect(Number(screen_?.getAttribute('rx'))).toBeGreaterThan(0);
    const base = glyph.querySelector('line');
    expect(base).not.toBeNull();
    expect(base?.getAttribute('y1')).toBe(base?.getAttribute('y2'));
    expect(Number(base?.getAttribute('y1'))).toBeGreaterThan(
      Number(screen_?.getAttribute('y')) + Number(screen_?.getAttribute('height')),
    );
  });

  it('says the name once and drops the hostname and the architecture', () => {
    renderPage();

    for (const device of devices) {
      const row = rowOf(device.name);
      expect(row.textContent).not.toContain(device.arch);
      // On a machine `install.sh` set up the hostname *is* the name, so what
      // has to hold is that the row says it once, as the title and nowhere else.
      if (device.hostname !== device.name) {
        expect(row.textContent).not.toContain(device.hostname);
      }
      expect(within(row).getAllByText(device.name)).toHaveLength(1);
    }
  });

  it('puts the dot on the status line, with the word and the platform as a word', () => {
    renderPage();

    for (const device of devices) {
      const status = rowOf(device.name).querySelector('.device-status');
      expect(status).not.toBeNull();
      const word = device.online ? strings.devices.online : strings.devices.offline;
      expect(status?.textContent).toBe(`${word} · ${platformLabel(device.platform)}`);
      expect(within(status as HTMLElement).getByRole('img', { name: word })).toBeInTheDocument();
      expect(status?.textContent).not.toContain(device.platform);
    }
  });

  it('shows each agent as its logo alone, named for a reader and a hover', () => {
    renderPage();

    for (const device of devices) {
      const row = rowOf(device.name);
      const agents = device.agents.filter((agent) => agent.available);
      const logos = row.querySelectorAll('.device-agent');
      expect(logos).toHaveLength(agents.length);
      // No name and no version anywhere in the strip — only the marks.
      expect(row.querySelector('.device-agents')?.textContent).toBe('');

      for (const [index, agent] of agents.entries()) {
        const label = agentLabel(agent.agent);
        const logo = logos[index] as HTMLElement;
        expect(logo).toHaveAttribute('aria-label', label);
        expect(logo).toHaveAttribute('title', label);
        expect(within(row).getByRole('img', { name: label })).toBe(logo);
      }
    }
  });
});
