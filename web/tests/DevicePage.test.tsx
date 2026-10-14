/**
 * A33 — a device has a page. `docs/DESIGN.md` § "A device has a page" and
 * § "Quota is a meter, drawn for accounts only": one card per agent the device
 * found, how each is signed in, a meter per window for accounts only, fresh
 * figures asked for the moment the page opens, and nothing drawn for what the
 * device did not report.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { DevicePage } from '../src/features/devices/DevicePage';
import { useAuth } from '../src/stores/auth';
import { useDevices } from '../src/stores/devices';
import { strings } from '../src/strings';
import { rpc } from '../src/lib/gateway';
import { RequestError } from '../src/lib/ws';
import { deviceAgents, devices, withoutLimits } from '../mock/fixtures';
import type { AgentInfo, Device } from '../src/protocol/types';

vi.mock('../src/lib/gateway', () => ({ rpc: vi.fn() }));

const deviceNamed = (id: string): Device => {
  const found = devices.find((d) => d.device_id === id);
  if (!found) throw new Error(`no mock device ${id}`);
  return found;
};

const freshAgents = (id: string): AgentInfo[] => {
  const found = deviceAgents[id];
  if (!found) throw new Error(`no mock agents for ${id}`);
  return found;
};

/** The reply the device sends back: the same agents, with their windows. */
const answersWithLimits = (id: string) =>
  vi.mocked(rpc).mockResolvedValue({ agents: freshAgents(id) } as never);

const renderPage = (id: string) =>
  render(
    <MemoryRouter initialEntries={[`/devices/${id}`]}>
      <Routes>
        <Route path="/devices/:deviceId" element={<DevicePage />} />
      </Routes>
    </MemoryRouter>,
  );

const cardOf = (name: string): HTMLElement => {
  const heading = screen.getByText(name);
  const card = heading.closest('.device-page-agent');
  if (!card) throw new Error(`no card for ${name}`);
  return card as HTMLElement;
};

beforeEach(() => {
  useDevices.setState({ devices: [...devices], loaded: true, error: null, updateErrors: {} });
  vi.mocked(rpc).mockReset();
  answersWithLimits('dev-mac');
});

afterEach(() => vi.mocked(rpc).mockReset());

describe('the header and the agent list', () => {
  // `docs/DESIGN.md` § "The device row": the row dropped both of these, and
  // this line under the title is the only place left that carries them.
  it('keeps the hostname and the architecture the row no longer shows', async () => {
    renderPage('dev-mac');
    const device = deviceNamed('dev-mac');

    expect(screen.getByRole('heading')).toHaveTextContent(device.name);
    expect(screen.getByRole('img', { name: 'online' })).toBeInTheDocument();
    expect(screen.getByText(`${device.hostname} · ${device.platform} · ${device.arch}`)).toBeInTheDocument();
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });

  // A36: a device keeps itself current, so no surface names its client.
  it('names neither the client version nor its build', async () => {
    renderPage('dev-mac');
    const device = deviceNamed('dev-mac');

    expect(screen.queryByText(new RegExp(device.client_version))).not.toBeInTheDocument();
    expect(screen.queryByText(new RegExp(device.client_build!.slice(0, 8)))).not.toBeInTheDocument();
    expect(screen.queryByText(/client/i)).not.toBeInTheDocument();
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });

  it('draws one card per available agent, in the device order', async () => {
    renderPage('dev-mac');

    const names = [...document.querySelectorAll('.device-page-agent-name')].map((el) => el.textContent);
    expect(names).toEqual(['Claude Code', 'Codex', 'Grok Build', 'pi']);
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });

  it('asks the device for fresh windows the moment it opens', async () => {
    renderPage('dev-mac');

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('device.agents', { device_id: 'dev-mac' }),
    );
  });

  it('says so in one line when the device found no agents', async () => {
    useDevices.setState({
      devices: [{ ...deviceNamed('dev-mac'), device_id: 'dev-bare', agents: [] }],
      loaded: true,
    });
    renderPage('dev-bare');

    expect(screen.getByText(strings.devices.noAgents)).toBeInTheDocument();
  });

  it('says a device it does not know is gone', () => {
    renderPage('dev-nope');

    expect(screen.getByText(strings.devicePage.gone)).toBeInTheDocument();
    expect(rpc).not.toHaveBeenCalled();
  });
});

describe('the four shapes an account comes in', () => {
  it('draws the account line and a meter per window', async () => {
    renderPage('dev-mac');

    const card = cardOf('Claude Code');
    expect(
      within(card).getByText('Anthropic account · Max · Max 5x · me@example.com'),
    ).toBeInTheDocument();
    await waitFor(() => expect(within(card).getAllByRole('progressbar')).toHaveLength(3));
    const names = [...card.querySelectorAll('.device-page-meter-name')].map((el) => el.textContent);
    expect(names).toEqual(['5-hour', '7-day', '7-day · Fable']);
    expect(within(card).getByText('16%')).toBeInTheDocument();
  });

  it('never draws a meter for a key, and names the host it goes to', async () => {
    renderPage('dev-mac');

    const card = cardOf('pi');
    expect(within(card).getByText('OpenAI API key · api.relay.example')).toBeInTheDocument();
    // pi's other provider is an account, so the card has meters — but only two.
    await waitFor(() => expect(within(card).getAllByRole('progressbar')).toHaveLength(2));
  });

  it('draws the account line alone when the vendor exposes no windows', async () => {
    renderPage('dev-mac');

    const card = cardOf('Grok Build');
    expect(within(card).getByText('xAI account · me@example.com')).toBeInTheDocument();
    await waitFor(() => expect(within(card).queryByText(strings.devicePage.checking)).toBeNull());
    expect(within(card).queryAllByRole('progressbar')).toHaveLength(0);
  });

  it('says an agent signed in nowhere is not signed in', async () => {
    answersWithLimits('dev-ci');
    renderPage('dev-ci');

    expect(within(cardOf('Claude Code')).getByText(strings.devicePage.notSignedIn)).toBeInTheDocument();
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });

  it('draws nothing at all for an agent whose device never looked', async () => {
    answersWithLimits('dev-ci');
    renderPage('dev-ci');

    const card = cardOf('Grok Build');
    await waitFor(() => expect(within(cardOf('Codex')).queryByText(strings.devicePage.checking)).toBeNull());
    expect(card.querySelector('.device-page-signin')).toBeNull();
    expect(card.querySelector('.device-page-note')).toBeNull();
  });
});

describe('what stands where the meters go', () => {
  it('reads Checking… until the reply arrives', async () => {
    vi.mocked(rpc).mockReturnValue(new Promise(() => undefined) as never);
    renderPage('dev-mac');

    expect(screen.getAllByText(strings.devicePage.checking).length).toBeGreaterThan(0);
    expect(screen.queryAllByRole('progressbar')).toHaveLength(0);
  });

  it('keeps the account line and says the quota is unavailable for an offline device', () => {
    const device = { ...deviceNamed('dev-mac'), online: false };
    useDevices.setState({ devices: [device], loaded: true });
    renderPage('dev-mac');

    const card = cardOf('Claude Code');
    expect(within(card).getByText(/^Anthropic account/)).toBeInTheDocument();
    expect(within(card).getByText(strings.devicePage.offlineQuota)).toBeInTheDocument();
    // An offline device is never asked: the request could only time out.
    expect(rpc).not.toHaveBeenCalled();
  });

  it('says the same when the gateway answers device_offline', async () => {
    vi.mocked(rpc).mockRejectedValue(
      new RequestError({ code: 'device_offline', message: 'the device is offline' }),
    );
    renderPage('dev-mac');

    await waitFor(() =>
      expect(screen.getAllByText(strings.devicePage.offlineQuota).length).toBeGreaterThan(0),
    );
  });

  it('repeats the device own words when a check failed', async () => {
    answersWithLimits('dev-ci');
    renderPage('dev-ci');

    await waitFor(() =>
      expect(screen.getByText('Codex shared daemon is not running')).toBeInTheDocument(),
    );
    expect(within(cardOf('Codex')).queryAllByRole('progressbar')).toHaveLength(0);
  });

  it('asks again when Refresh is taken', async () => {
    renderPage('dev-mac');
    await waitFor(() => expect(screen.getAllByRole('progressbar').length).toBeGreaterThan(0));

    await userEvent.click(screen.getByRole('button', { name: strings.devicePage.refresh }));

    await waitFor(() => expect(vi.mocked(rpc).mock.calls).toHaveLength(2));
  });
});

describe('the fresh windows stay on the page', () => {
  it('leaves the stored device without them, so the list never diffs against them', async () => {
    renderPage('dev-mac');
    await waitFor(() => expect(screen.getAllByRole('progressbar').length).toBeGreaterThan(0));

    for (const agent of useDevices.getState().devices[0]!.agents) {
      for (const account of agent.accounts ?? []) {
        expect(account.limits).toBeUndefined();
        expect(account.limits_error).toBeUndefined();
        expect(account.limits_checked_at).toBeUndefined();
      }
    }
  });

  it('is what the mock device list already carries', () => {
    expect(deviceNamed('dev-mac').agents).toEqual(freshAgents('dev-mac').map(withoutLimits));
  });
});

/**
 * A36: the page says what the automatic update is doing and what it did wrong,
 * and a failure is the only thing a person is asked to act on.
 */
describe('the update notice on the page', () => {
  const CLIENT_BUILD = 'a'.repeat(64);
  const config = {
    public_origin: 'https://rc.example.com',
    stt: { enabled: false, languages: ['auto'] },
    push: { web_enabled: false, apns_enabled: false },
    version: '1.4.6',
    client: { version: '1.4.6', build: CLIENT_BUILD, url: '/dist/rc_client-latest.whl' },
  };

  const showDevice = (patch: Partial<Device>): void => {
    useDevices.setState({
      devices: devices.map((d) => (d.device_id === 'dev-mac' ? { ...d, ...patch } : d)),
      loaded: true,
      error: null,
      updateErrors: {},
    });
  };

  const retryButton = () => screen.getByRole('button', { name: strings.devices.retryUpdate });

  beforeEach(() => {
    useAuth.setState({ status: 'signed-in', config });
  });

  it('says nothing while nothing is happening', async () => {
    renderPage('dev-mac');

    expect(document.querySelector('.device-page-update')).toBeNull();
    expect(
      screen.queryByRole('button', { name: strings.devices.retryUpdate }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });

  it('says "Updating…" while the update runs, and offers nothing to press', async () => {
    showDevice({ update_state: 'updating' });
    renderPage('dev-mac');

    expect(screen.getByText(strings.devices.updating)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: strings.devices.retryUpdate }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });

  it('says why it failed and asks the device again on Retry', async () => {
    showDevice({ update_state: 'failed', update_message: 'the device did not come back' });
    renderPage('dev-mac');

    expect(
      screen.getByText(strings.devices.updateFailed('the device did not come back')),
    ).toBeInTheDocument();
    await userEvent.click(retryButton());

    expect(screen.getByText(/Update mac-studio-office to 1\.4\.6\?/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: strings.devices.updateConfirm }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('device.update', {
        device_id: 'dev-mac',
        build: CLIENT_BUILD,
      }),
    );
  });

  it('disables Retry while the device is offline, and says why', async () => {
    showDevice({ update_state: 'failed', update_message: 'no', online: false });
    renderPage('dev-mac');

    expect(retryButton()).toBeDisabled();
    expect(retryButton()).toHaveAttribute('title', strings.devices.deviceOffline);
  });

  it('disables Retry while the gateway serves no wheel, and says why', async () => {
    useAuth.setState({ config: { ...config, client: undefined } });
    showDevice({ update_state: 'failed', update_message: 'no' });
    renderPage('dev-mac');

    expect(retryButton()).toBeDisabled();
    expect(retryButton()).toHaveAttribute('title', strings.devices.updateNoBuild);
    await waitFor(() => expect(rpc).toHaveBeenCalled());
  });
});
