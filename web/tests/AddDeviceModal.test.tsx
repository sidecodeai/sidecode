import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AddDeviceModal } from '../src/features/devices/AddDeviceModal';
import { useConnection } from '../src/stores/connection';
import { claudeAgent, codexAgent } from '../mock/fixtures';
import type { Device } from '../src/protocol/types';

const CODE = 'RC-7K42-QX9M';
// Two one-liners are on screen now: this one, and the scan flow's (A23).
const PAIR_COMMAND = /curl -fsSL.+--pair/;

const pairingResponse = {
  code: CODE,
  expires_at: Date.now() + 10 * 60_000,
  install: {
    macos: `curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair ${CODE}`,
    // The gateway sends both keys and they hold the same string today. This one
    // differs so the test can see which key the modal reads now that nobody is
    // asked which platform they are on.
    linux: `curl -fsSL https://rc.example.com/install.sh | sh -s -- --pair-on-linux ${CODE}`,
  },
};

const pairedDevice: Device = {
  device_id: 'dev-new',
  name: 'new-laptop',
  platform: 'macos',
  hostname: 'new-laptop.local',
  arch: 'arm64',
  client_version: '0.1.0',
  online: true,
  last_seen: Date.now(),
  created_at: Date.now(),
  latency_ms: 24,
  agents: [claudeAgent, codexAgent],
};

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/devices/pairing') && init?.method === 'POST') {
        return new Response(JSON.stringify(pairingResponse), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
  useConnection.setState({ pairing: null });
});

afterEach(() => {
  vi.unstubAllGlobals();
  useConnection.setState({ pairing: null });
});

describe('AddDeviceModal', () => {
  it('renders nothing until it is opened', () => {
    const { container } = render(<AddDeviceModal open={false} onClose={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('requests a pairing code and shows the one-liner with the code', async () => {
    render(<AddDeviceModal open onClose={vi.fn()} />);
    // The code appears twice: inside the one-liner and on its own below it.
    expect(await screen.findAllByText(new RegExp(CODE))).toHaveLength(2);
    expect(screen.getByText(PAIR_COMMAND)).toHaveTextContent(CODE);
    expect(screen.getByText(/single use/)).toHaveTextContent(/expires in \d+:\d\d/);
  });

  it('asks nobody which platform they are on and shows the one command', async () => {
    render(<AddDeviceModal open onClose={vi.fn()} />);
    await screen.findByText(PAIR_COMMAND);

    expect(screen.queryByRole('button', { name: 'macOS' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Linux' })).toBeNull();
    expect(screen.getByText(PAIR_COMMAND)).toHaveTextContent(pairingResponse.install.macos);
  });

  it('walks the live steps from pairing.progress and only then enables Continue', async () => {
    render(<AddDeviceModal open onClose={vi.fn()} />);
    await screen.findByText(PAIR_COMMAND);

    const stepItem = (label: string): HTMLElement => {
      const node = screen.getByText(label).closest('li');
      if (!node) throw new Error(`no step row for ${label}`);
      return node;
    };

    expect(stepItem('Gateway ready')).toHaveClass('done');
    expect(stepItem('Device handshake')).toHaveClass('active');
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    expect(screen.getByText('Waiting for this device')).toBeInTheDocument();

    useConnection.setState({ pairing: { code: CODE, step: 'enrolled' } });
    await waitFor(() => expect(stepItem('Device handshake')).toHaveClass('done'));
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();

    useConnection.setState({ pairing: { code: CODE, step: 'online', device: pairedDevice } });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled(),
    );
    expect(screen.getByText('new-laptop connected')).toBeInTheDocument();

    useConnection.setState({ pairing: { code: CODE, step: 'agents', device: pairedDevice } });
    await waitFor(() => expect(stepItem('Detect installed agents')).toHaveClass('done'));
    expect(screen.getByText('claude · codex')).toBeInTheDocument();
  });

  it('ignores progress frames for a different pairing code', async () => {
    render(<AddDeviceModal open onClose={vi.fn()} />);
    await screen.findByText(PAIR_COMMAND);
    useConnection.setState({ pairing: { code: 'RC-0000-0000', step: 'online', device: pairedDevice } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled());
  });

  it('offers the scan flow beside the code, with its own one-liner (A23)', async () => {
    render(<AddDeviceModal open onClose={vi.fn()} />);
    await screen.findByText(PAIR_COMMAND);

    expect(screen.getByRole('heading', { name: 'From your phone' })).toBeInTheDocument();
    // The scan one-liner carries no code: the host asks for its own token.
    const scan = screen.getByText(`curl -fsSL ${window.location.origin}/install.sh | sh`);
    expect(scan).toHaveTextContent(/install\.sh \| sh$/);
    expect(scan.textContent).not.toContain(CODE);
    expect(screen.getByText(/The host prints a QR code/)).toBeInTheDocument();
  });

  it('reveals the manual install steps on demand', async () => {
    const user = userEvent.setup();
    render(<AddDeviceModal open onClose={vi.fn()} />);
    await screen.findByText(PAIR_COMMAND);
    await user.click(screen.getByRole('button', { name: 'Manual install' }));
    expect(screen.getByText(/rc-client pair --gateway/)).toHaveTextContent(CODE);
  });

  it('cancels an unclaimed code when the dialog is dismissed', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<AddDeviceModal open onClose={onClose} />);
    await screen.findByText(PAIR_COMMAND);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        `/api/devices/pairing/${CODE}`,
        expect.objectContaining({ method: 'DELETE' }),
      ),
    );
  });
});
