/**
 * A38 §7.3 and rule 20 — the terminal page.
 *
 * jsdom cannot measure text, so xterm.js is replaced by a fake that records
 * what was written into it and can pretend somebody typed. What is under test
 * is everything around the emulator: the size the shell is opened at, the
 * bytes that go each way, the size that follows the window, taking the
 * terminal over again after a lost socket, and the two ends — the shell's own
 * and the person's.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { TerminalPage } from '../src/features/devices/TerminalPage';
import { strings } from '../src/strings';
import { RequestError } from '../src/lib/ws';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { pushTerminalFrame } from '../src/stores/terminal';
import { devices } from '../mock/fixtures';

interface Call {
  type: string;
  params: Record<string, unknown>;
}

/** What the fake emulator records where a real one would clear its screen. */
const RESET = '<reset>';

const harness = vi.hoisted(() => {
  const state = {
    calls: [] as Call[],
    terminals: [] as {
      cols: number;
      rows: number;
      written: string[];
      resets: number;
      type: (data: string) => void;
    }[],
    answer: (_type: string, _params: Record<string, unknown>): Promise<unknown> =>
      Promise.resolve({}),
  };
  return state;
});

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit(): void {}
  },
}));

vi.mock('@xterm/xterm', () => {
  const decoder = new TextDecoder();
  const RESET = '<reset>';
  class Terminal {
    cols = 100;
    rows = 30;
    written: string[] = [];
    resets = 0;
    private handlers: ((data: string) => void)[] = [];

    constructor() {
      harness.terminals.push(this);
    }
    loadAddon(): void {}
    open(): void {}
    onData(handler: (data: string) => void): { dispose: () => void } {
      this.handlers.push(handler);
      return { dispose: () => undefined };
    }
    write(bytes: Uint8Array): void {
      this.written.push(decoder.decode(bytes));
    }
    reset(): void {
      this.resets += 1;
      this.written.push(RESET);
    }
    dispose(): void {}
    /** What the person types, as xterm would report it. */
    type(data: string): void {
      for (const handler of this.handlers) handler(data);
    }
  }
  return { Terminal };
});

vi.mock('../src/lib/gateway', () => ({
  rpc: (type: string, params: Record<string, unknown>) => {
    harness.calls.push({ type, params });
    return harness.answer(type, params);
  },
  getSocket: () => null,
  setSocket: () => undefined,
}));

const TERMINAL_ID = '2f8d4b6a-1c3e-4a75-9b0d-6e2f8c4a1d57';
const b64 = (text: string): string => Buffer.from(text, 'utf8').toString('base64');

/** The mock devices, plus A38's answers: dev-mac has a shell, dev-ci has none. */
const openShell = (): void => {
  harness.answer = (type) => {
    if (type === 'terminal.open') return Promise.resolve({ terminal_id: TERMINAL_ID });
    return Promise.resolve({});
  };
};

const calls = (type: string): Call[] => harness.calls.filter((call) => call.type === type);
const emulator = () => {
  const term = harness.terminals.at(-1);
  if (!term) throw new Error('no emulator');
  return term;
};

const renderPage = (deviceId = 'dev-mac') =>
  render(
    <MemoryRouter initialEntries={[`/devices/${deviceId}/terminal`]}>
      <Routes>
        <Route path="/devices" element={<div>devices</div>} />
        <Route path="/devices/:deviceId/terminal" element={<TerminalPage />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  harness.calls = [];
  harness.terminals = [];
  openShell();
  window.sessionStorage.clear();
  useDevices.setState({ devices: [...devices], loaded: true, error: null });
  useConnection.setState({ status: 'open' });
});

describe('opening a terminal', () => {
  it('opens the shell at the size the emulator fitted itself to', async () => {
    renderPage();

    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));
    expect(calls('terminal.open')[0]?.params).toEqual({
      device_id: 'dev-mac',
      cols: 100,
      rows: 30,
    });
    expect(await screen.findByText(strings.terminal.connected)).toBeInTheDocument();
  });

  it('names the device and offers Close', async () => {
    renderPage();

    expect(screen.getByRole('heading', { name: 'mac-studio-office' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: strings.common.close })).toBeInTheDocument();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));
  });

  it('writes what the device sends into the emulator', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => {
      pushTerminalFrame({
        type: 'terminal.output',
        terminal_id: TERMINAL_ID,
        device_id: 'dev-mac',
        seq: 1,
        data: b64('$ ls\r\n'),
      });
    });

    expect(emulator().written).toContain('$ ls\r\n');
  });

  it('ignores output for another terminal or another device', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => {
      pushTerminalFrame({
        type: 'terminal.output',
        terminal_id: 'some-other-terminal',
        device_id: 'dev-mac',
        seq: 1,
        data: b64('not mine'),
      });
      pushTerminalFrame({
        type: 'terminal.output',
        terminal_id: TERMINAL_ID,
        device_id: 'dev-ci',
        seq: 1,
        data: b64('not mine either'),
      });
    });

    expect(emulator().written).toEqual([]);
  });

  it('says so when `seq` skipped, rather than pretending the output is whole', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => {
      pushTerminalFrame({
        type: 'terminal.output',
        terminal_id: TERMINAL_ID,
        device_id: 'dev-mac',
        seq: 1,
        data: b64('one'),
      });
      pushTerminalFrame({
        type: 'terminal.output',
        terminal_id: TERMINAL_ID,
        device_id: 'dev-mac',
        seq: 3,
        data: b64('three'),
      });
    });

    expect(await screen.findByText(strings.terminal.gap)).toBeInTheDocument();
  });
});

describe('what the person does', () => {
  it('sends what is typed as base64 bytes', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => emulator().type('ls -la\r'));

    expect(calls('terminal.input')).toHaveLength(1);
    expect(calls('terminal.input')[0]?.params).toEqual({
      device_id: 'dev-mac',
      terminal_id: TERMINAL_ID,
      data: b64('ls -la\r'),
    });
  });

  it('follows the window with one `terminal.resize`', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => {
      const term = emulator();
      term.cols = 120;
      term.rows = 40;
      window.dispatchEvent(new Event('resize'));
      window.dispatchEvent(new Event('resize'));
    });

    await waitFor(() => expect(calls('terminal.resize')).toHaveLength(1));
    expect(calls('terminal.resize')[0]?.params).toEqual({
      device_id: 'dev-mac',
      terminal_id: TERMINAL_ID,
      cols: 120,
      rows: 40,
    });
  });

  it('ends the shell when the page is left', async () => {
    const view = renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    view.unmount();

    expect(calls('terminal.close')).toHaveLength(1);
    expect(calls('terminal.close')[0]?.params).toEqual({
      device_id: 'dev-mac',
      terminal_id: TERMINAL_ID,
    });
  });

  it('ends the shell on Close and goes back to the devices', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    await userEvent.click(screen.getByRole('button', { name: strings.common.close }));

    expect(calls('terminal.close')).toHaveLength(1);
    expect(screen.getByText('devices')).toBeInTheDocument();
  });
});

describe('a lost socket', () => {
  it('says Disconnected, then attaches again with the scrollback first', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => useConnection.setState({ status: 'reconnecting' }));
    expect(await screen.findByText(strings.terminal.disconnected)).toBeInTheDocument();

    harness.answer = (type) => {
      if (type === 'terminal.attach') {
        return Promise.resolve({
          terminal_id: TERMINAL_ID,
          cols: 100,
          rows: 30,
          scrollback: b64('$ echo hi\r\nhi\r\n$ '),
        });
      }
      return Promise.resolve({});
    };
    act(() => useConnection.setState({ status: 'open' }));

    await waitFor(() => expect(calls('terminal.attach')).toHaveLength(1));
    expect(calls('terminal.attach')[0]?.params).toEqual({
      device_id: 'dev-mac',
      terminal_id: TERMINAL_ID,
    });
    // The scrollback is the screen as it was left, so it replaces what is
    // drawn rather than being appended under it.
    expect(emulator().written).toEqual([RESET, '$ echo hi\r\nhi\r\n$ ']);
    expect(await screen.findByText(strings.terminal.connected)).toBeInTheDocument();
    // §7.3: the terminal was opened once, and taken over once.
    expect(calls('terminal.open')).toHaveLength(1);
  });

  it('says the shell has ended once the device no longer keeps it', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => useConnection.setState({ status: 'reconnecting' }));
    harness.answer = () => Promise.reject(new RequestError({ code: 'not_found', message: '' }));
    act(() => useConnection.setState({ status: 'open' }));

    await waitFor(() => expect(calls('terminal.attach')).toHaveLength(1));
    // The ten minutes ran out, so nothing is opened behind the person's back.
    expect(await screen.findByText(strings.terminal.exited)).toBeInTheDocument();
    expect(calls('terminal.open')).toHaveLength(1);
  });

  it('says why it could not reach the device, and tries again when asked', async () => {
    harness.answer = () =>
      Promise.reject(new RequestError({ code: 'device_offline', message: '' }));
    renderPage();

    expect(await screen.findByText(strings.errors.deviceOffline)).toBeInTheDocument();
    openShell();
    await userEvent.click(screen.getByRole('button', { name: strings.terminal.reconnect }));

    await waitFor(() => expect(calls('terminal.open')).toHaveLength(2));
    expect(await screen.findByText(strings.terminal.connected)).toBeInTheDocument();
  });
});

/**
 * §7.3: a reload inside the ten minutes finds the shell still running, because
 * the id it was opened under is this tab's.
 */
describe('coming back to the page', () => {
  it('attaches to the terminal this tab left behind', async () => {
    window.sessionStorage.setItem('rc.terminal.dev-mac', TERMINAL_ID);
    harness.answer = (type) =>
      type === 'terminal.attach'
        ? Promise.resolve({
            terminal_id: TERMINAL_ID,
            cols: 100,
            rows: 30,
            scrollback: b64('$ '),
          })
        : Promise.resolve({});

    renderPage();

    await waitFor(() => expect(calls('terminal.attach')).toHaveLength(1));
    expect(calls('terminal.open')).toHaveLength(0);
  });

  it('opens a fresh shell when the remembered one is long gone', async () => {
    window.sessionStorage.setItem('rc.terminal.dev-mac', TERMINAL_ID);
    harness.answer = (type) =>
      type === 'terminal.attach'
        ? Promise.reject(new RequestError({ code: 'not_found', message: '' }))
        : Promise.resolve({ terminal_id: TERMINAL_ID });

    renderPage();

    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));
    expect(await screen.findByText(strings.terminal.connected)).toBeInTheDocument();
  });
});

describe('the shell ending', () => {
  it('says so with the code and offers a new one', async () => {
    renderPage();
    await waitFor(() => expect(calls('terminal.open')).toHaveLength(1));

    act(() => {
      pushTerminalFrame({
        type: 'terminal.exited',
        terminal_id: TERMINAL_ID,
        device_id: 'dev-mac',
        code: 0,
      });
    });

    expect(await screen.findByText(strings.terminal.exitedCode(0))).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: strings.terminal.newShell }));

    await waitFor(() => expect(calls('terminal.open')).toHaveLength(2));
  });
});

describe('a device that cannot give a shell', () => {
  it('says the client offers none and asks for nothing', async () => {
    renderPage('dev-ci');

    expect(await screen.findByText(strings.devices.noTerminal)).toBeInTheDocument();
    expect(harness.calls).toEqual([]);
  });

  it('says the device is offline when that is the reason', async () => {
    useDevices.setState({
      devices: devices.map((d) => (d.device_id === 'dev-mac' ? { ...d, online: false } : d)),
      loaded: true,
      error: null,
    });
    renderPage();

    expect(await screen.findByText(strings.devices.deviceOffline)).toBeInTheDocument();
    expect(harness.calls).toEqual([]);
  });
});
