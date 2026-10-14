/**
 * A36 in the device list: a device keeps itself current, so a row says nothing
 * about its client until an update is running or has failed, and the only
 * action an app offers is trying a failed one again.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { DevicesPage } from '../src/features/devices/DevicesPage';
import { useAuth } from '../src/stores/auth';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { setSocket } from '../src/lib/gateway';
import { RequestError } from '../src/lib/ws';
import { strings } from '../src/strings';
import { CLIENT_BUILD, OLD_CLIENT_BUILD, devices } from '../mock/fixtures';
import type { AppSocket } from '../src/lib/ws';
import type { Device } from '../src/protocol/types';

const config = {
  public_origin: 'https://rc.example.com',
  stt: { enabled: false, languages: ['auto'] },
  push: { web_enabled: false, apns_enabled: false },
  version: '0.1.0',
  client: { version: '0.1.0', build: CLIENT_BUILD, url: '/dist/rc_client-latest.whl' },
};

const request = vi.fn(async () => ({ accepted: true, from: OLD_CLIENT_BUILD }));

/**
 * `mac` runs the gateway's build; `ci` runs the one before it. Both start idle,
 * whatever the mock's own story is, so each test names the state it is about.
 */
function setDevices(patch: { mac?: Partial<Device>; ci?: Partial<Device> } = {}): void {
  const idle: Partial<Device> = { update_state: 'idle', update_message: null };
  const next = devices.map((device) =>
    device.device_id === 'dev-mac'
      ? { ...device, ...idle, ...patch.mac }
      : { ...device, ...idle, ...patch.ci },
  );
  useDevices.setState({ devices: next, loaded: true, error: null, updateErrors: {} });
}

beforeEach(() => {
  request.mockClear();
  request.mockResolvedValue({ accepted: true, from: OLD_CLIENT_BUILD });
  setSocket({ request } as unknown as AppSocket);
  useAuth.setState({ status: 'signed-in', config });
  useSessions.setState({ sessions: {}, loaded: true });
  setDevices();
});

afterEach(() => {
  setSocket(null);
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <DevicesPage />
    </MemoryRouter>,
  );

const openMenuFor = async (name: string): Promise<HTMLElement> => {
  const row = screen.getByText(name).closest('li');
  if (!row) throw new Error(`no row for ${name}`);
  await userEvent.click(within(row).getByRole('button', { name: strings.a11y.openMenu }));
  return screen.getByRole('menu');
};

const retryItem = () => screen.getByRole('menuitem', { name: strings.devices.retryUpdate });

const rowOf = (name: string): HTMLElement => {
  const row = screen.getByText(name).closest('li');
  if (!row) throw new Error(`no row for ${name}`);
  return row as HTMLElement;
};

const failed = { update_state: 'failed' as const, update_message: 'the device did not come back' };

describe('the client line on a device row', () => {
  it('says nothing at all about a device that runs the gateway’s build', () => {
    renderPage();
    const row = rowOf('mac-studio-office');
    expect(row.querySelector('.device-client')).toBeNull();
    expect(within(row).queryByText('0.1.0')).not.toBeInTheDocument();
    expect(within(row).queryByText(/client/i)).not.toBeInTheDocument();
    expect(within(row).queryByText(new RegExp(CLIENT_BUILD.slice(0, 8)))).not.toBeInTheDocument();
  });

  it('says nothing about a device behind the gateway either: it updates itself', () => {
    renderPage();
    const row = rowOf('ci-runner-01');
    expect(row.querySelector('.device-client')).toBeNull();
    expect(within(row).queryByText(/0\.0\.9/)).not.toBeInTheDocument();
    expect(screen.queryByText(/available/i)).not.toBeInTheDocument();
  });

  it('says nothing when the gateway serves no build', () => {
    useAuth.setState({ config: { ...config, client: undefined } });
    const { container } = renderPage();
    expect(container.querySelectorAll('.device-client')).toHaveLength(0);
  });

  it('draws "Updating…" and a pulsing dot while an update runs', () => {
    setDevices({ ci: { update_state: 'updating' } });
    const { container } = renderPage();

    expect(screen.getByText(strings.devices.updating)).toBeInTheDocument();
    expect(container.querySelectorAll('.dot.pulse')).toHaveLength(1);
  });

  it('draws the reason an update failed', () => {
    setDevices({ ci: failed });
    renderPage();
    expect(
      screen.getByText(strings.devices.updateFailed('the device did not come back')),
    ).toBeInTheDocument();
    expect(rowOf('mac-studio-office').querySelector('.device-client')).toBeNull();
  });
});

describe('the Retry update item', () => {
  it('is not offered while nothing is wrong', async () => {
    renderPage();
    const menu = await openMenuFor('ci-runner-01');
    const labels = [...menu.querySelectorAll('.menu-label')].map((n) => n.textContent);
    expect(labels).toEqual([
      strings.common.rename,
      strings.devices.showQuota,
      strings.common.revoke,
    ]);
  });

  it('is not offered while the update is still running', async () => {
    setDevices({ ci: { update_state: 'updating' } });
    renderPage();
    const menu = await openMenuFor('ci-runner-01');
    const labels = [...menu.querySelectorAll('.menu-label')].map((n) => n.textContent);
    expect(labels).toEqual([
      strings.common.rename,
      strings.devices.showQuota,
      strings.common.revoke,
    ]);
  });

  // A38 rule 20: Rename · Retry update (only while failed) · Show quota · Revoke.
  it('sits between Rename and Show quota once the update failed', async () => {
    setDevices({ ci: failed });
    renderPage();
    const menu = await openMenuFor('ci-runner-01');
    const labels = [...menu.querySelectorAll('.menu-label')].map((n) => n.textContent);
    expect(labels).toEqual([
      strings.common.rename,
      strings.devices.retryUpdate,
      strings.devices.showQuota,
      strings.common.revoke,
    ]);
  });

  it('confirms with the version it installs, then asks the device for that build', async () => {
    setDevices({ ci: failed });
    renderPage();
    await openMenuFor('ci-runner-01');
    await userEvent.click(retryItem());

    expect(
      screen.getByText(strings.devices.updateBody('ci-runner-01', '0.1.0')),
    ).toBeInTheDocument();
    expect(screen.getByText(/Update ci-runner-01 to 0\.1\.0\?/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: strings.devices.updateConfirm }));

    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        'device.update',
        { device_id: 'dev-ci', build: CLIENT_BUILD },
        undefined,
      ),
    );
  });

  it('confirms with the gateway’s client when the version is unknown', async () => {
    useAuth.setState({
      config: { ...config, client: { build: CLIENT_BUILD, url: '/dist/rc_client-latest.whl' } },
    });
    setDevices({ ci: failed });
    renderPage();
    await openMenuFor('ci-runner-01');
    await userEvent.click(retryItem());

    expect(
      screen.getByText(strings.devices.updateBody('ci-runner-01', undefined)),
    ).toBeInTheDocument();
    expect(screen.queryByText(/to 0\.1\.0\?/)).not.toBeInTheDocument();
  });

  it('is disabled while the device is offline, and says why', async () => {
    setDevices({ ci: { ...failed, online: false } });
    renderPage();
    await openMenuFor('ci-runner-01');
    expect(retryItem()).toBeDisabled();
    expect(retryItem()).toHaveAttribute('title', strings.devices.deviceOffline);
  });

  it('is disabled while the gateway serves no wheel, and says why', async () => {
    useAuth.setState({ config: { ...config, client: undefined } });
    setDevices({ ci: failed });
    renderPage();
    await openMenuFor('ci-runner-01');
    expect(retryItem()).toBeDisabled();
    expect(retryItem()).toHaveAttribute('title', strings.devices.updateNoBuild);
  });

  it('shows the device’s own words when the retry is refused', async () => {
    request.mockRejectedValue(new RequestError({ code: 'conflict', message: '2 sessions running' }));
    setDevices({ ci: failed });
    renderPage();
    await openMenuFor('ci-runner-01');
    await userEvent.click(retryItem());
    await userEvent.click(screen.getByRole('button', { name: strings.devices.updateConfirm }));

    await waitFor(() =>
      expect(
        screen.getByText(strings.devices.updateFailed('2 sessions running')),
      ).toBeInTheDocument(),
    );
    // The row still offers the retry: the gateway never accepted this one.
    await openMenuFor('ci-runner-01');
    expect(retryItem()).toBeEnabled();
  });
});
