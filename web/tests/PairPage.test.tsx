/**
 * A23 on the web: `/pair#<token>` claims the token a host printed as a QR code
 * and then shows the handshake the Add device modal already shows.
 */
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { PairPage } from '../src/features/devices/PairPage';
import { useConnection } from '../src/stores/connection';
import { strings } from '../src/strings';
import { claudeAgent } from '../mock/fixtures';
import type { Device } from '../src/protocol/types';

const TOKEN = '7ZK3M9Q2X5H8B1V4N6P0R2T4W6';
const CODE = 'RC-7K42-QX9M';

const pairedDevice: Device = {
  device_id: 'dev-new',
  name: 'new-laptop',
  platform: 'macos',
  hostname: 'new-laptop.local',
  arch: 'arm64',
  client_version: '0.1.0',
  client_build: 'abc',
  online: true,
  last_seen: Date.now(),
  created_at: Date.now(),
  latency_ms: 24,
  agents: [claudeAgent],
};

/** Every claim answers with `status`; 200 hands back a pairing code. */
function stubClaim(status: number, body: unknown): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const renderPair = (hash = `#${TOKEN}`) =>
  render(
    <MemoryRouter initialEntries={[`/pair${hash}`]}>
      <PairPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  useConnection.setState({ pairing: null });
});

afterEach(() => {
  vi.unstubAllGlobals();
  useConnection.setState({ pairing: null });
});

describe('PairPage', () => {
  it('claims the token in the fragment and shows the code it was given', async () => {
    const fetchMock = stubClaim(200, { code: CODE, expires_at: Date.now() + 600_000 });
    renderPair();

    await waitFor(() => expect(screen.getByText(CODE)).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/pairing/requests/${TOKEN}/claim`,
      expect.objectContaining({ method: 'POST' }),
    );
    expect(screen.getByText(strings.pairing.stepGateway)).toBeInTheDocument();
  });

  it('walks the same steps the minted code walks', async () => {
    stubClaim(200, { code: CODE, expires_at: Date.now() + 600_000 });
    renderPair();
    await waitFor(() => expect(screen.getByText(CODE)).toBeInTheDocument());

    expect(screen.getByText(strings.pairing.waiting)).toBeInTheDocument();

    useConnection.setState({ pairing: { code: CODE, step: 'online', device: pairedDevice } });
    await waitFor(() =>
      expect(screen.getByText(strings.pairing.connected('new-laptop'))).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: strings.common.done })).toBeInTheDocument();
  });

  it('ignores progress for another code', async () => {
    stubClaim(200, { code: CODE, expires_at: Date.now() + 600_000 });
    renderPair();
    await waitFor(() => expect(screen.getByText(CODE)).toBeInTheDocument());

    useConnection.setState({ pairing: { code: 'RC-0000-0000', step: 'online', device: pairedDevice } });
    await waitFor(() => expect(screen.getByText(strings.pairing.waiting)).toBeInTheDocument());
  });

  it('tells the reader to run the command again when the token is gone', async () => {
    stubClaim(410, { ok: false, error: { code: 'not_found', message: 'expired' } });
    renderPair();
    await waitFor(() =>
      expect(screen.getByText(strings.pairing.claimExpired)).toBeInTheDocument(),
    );
  });

  it('treats an unknown token the same way', async () => {
    stubClaim(404, { ok: false, error: { code: 'not_found', message: 'unknown' } });
    renderPair();
    await waitFor(() =>
      expect(screen.getByText(strings.pairing.claimExpired)).toBeInTheDocument(),
    );
  });

  it('says so when the token was already claimed', async () => {
    stubClaim(409, { ok: false, error: { code: 'conflict', message: 'claimed' } });
    renderPair();
    await waitFor(() => expect(screen.getByText(strings.pairing.claimUsed)).toBeInTheDocument());
  });

  it('claims a token once even when its effect runs twice', async () => {
    const fetchMock = stubClaim(200, { code: CODE, expires_at: Date.now() + 600_000 });
    render(
      <StrictMode>
        <MemoryRouter initialEntries={[`/pair#${TOKEN}`]}>
          <PairPage />
        </MemoryRouter>
      </StrictMode>,
    );

    await waitFor(() => expect(screen.getByText(CODE)).toBeInTheDocument());
    // A second claim would come back 409 and hide the code behind an error.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('claims nothing when the link carries no token', async () => {
    const fetchMock = stubClaim(200, {});
    renderPair('');

    expect(await screen.findByText(strings.pairing.claimInvalid)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
