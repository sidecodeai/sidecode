/**
 * The voice assistant's entry in the terminal page's header, which is outside the top bar: the
 * call carries on when the person closes the shell, and the gateway is told no chat is on screen.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';
import { TerminalPage } from '../../src/features/devices/TerminalPage';
import { strings } from '../../src/strings';
import { useConnection } from '../../src/stores/connection';
import { useDevices } from '../../src/stores/devices';
import { devices } from '../../mock/fixtures';
import { FakeWebSocket, READY } from './audioFakes';
import { Host } from './Host';
import { afterLive, beforeLive, context, dial, en, status } from './harness';

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit(): void {}
  },
}));

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 100;
    rows = 30;
    loadAddon(): void {}
    open(): void {}
    onData(): { dispose: () => void } {
      return { dispose: () => undefined };
    }
    write(): void {}
    reset(): void {}
    dispose(): void {}
  },
}));

vi.mock('../../src/lib/gateway', () => ({
  rpc: () => new Promise(() => undefined),
  getSocket: () => null,
  setSocket: () => undefined,
}));

/** The page's own Close, which the panel's close button shares a name with. */
const closeShell = (): HTMLElement =>
  within(document.querySelector('.terminal-head') as HTMLElement).getByRole('button', {
    name: strings.common.close,
  });

const closePanel = (): HTMLElement =>
  within(screen.getByRole('dialog')).getByRole('button', { name: en.close });

const renderTerminal = () =>
  render(
    <MemoryRouter initialEntries={['/devices/dev-mac/terminal']}>
      <Host />
      <Routes>
        <Route path="/devices" element={<div>devices</div>} />
        <Route path="/devices/:deviceId/terminal" element={<TerminalPage />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  beforeLive();
  useDevices.setState({ devices: [...devices], loaded: true, error: null });
  useConnection.setState({ status: 'open' });
});

afterEach(afterLive);

describe('the entry in the terminal header', () => {
  it('sits in the header beside Close', async () => {
    renderTerminal();
    const entry = await screen.findByRole('button', { name: en.open });
    expect(entry.closest('.terminal-head')).not.toBeNull();
    expect(screen.getByRole('button', { name: strings.common.close })).toBeInTheDocument();
  });

  it('shows nothing while the gateway does not offer the feature', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => status({ enabled: false })));
    renderTerminal();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: en.open })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: strings.common.close })).toBeInTheDocument();
  });

  it('keeps the call going when the shell is closed and the page changes', async () => {
    renderTerminal();
    fireEvent.click(await screen.findByRole('button', { name: en.open }));
    const ws = await dial();
    ws.open();
    act(() => ws.serverSend(READY));
    await screen.findByRole('button', { name: en.hangUp });
    await waitFor(() => expect(context().worklets).toHaveLength(1));
    // No chat is on screen here.
    expect(ws.commands()).toContainEqual({ type: 'focus', device_id: null, session_id: null });

    fireEvent.click(closeShell());
    await screen.findByText('devices');
    expect(ws.closedWith).toBeNull();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(context().closed).toBe(false);
  });

  it('says a call is on while the panel is closed', async () => {
    renderTerminal();
    fireEvent.click(await screen.findByRole('button', { name: en.open }));
    const ws = await dial();
    ws.open();
    act(() => ws.serverSend(READY));
    await screen.findByRole('button', { name: en.hangUp });
    fireEvent.click(closePanel());
    expect(screen.getByRole('button', { name: en.openInCall })).toHaveAttribute('data-active', 'true');
  });
});
