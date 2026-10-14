/**
 * `/pair#<token>` — the link a host's QR code encodes (A23).
 *
 * The host asked the gateway for a claim token and is long-polling for the
 * outcome. Opening this page claims that token as the signed-in user; the
 * gateway mints the host an ordinary pairing code and hands it to the poll, so
 * from here on the handshake is the one the Add device modal already shows.
 */
import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { Button } from '../../components/Button';
import { ApiError, api } from '../../lib/api';
import { strings } from '../../strings';
import { PairingSteps } from './PairingProgress';
import { usePairingProgress } from './usePairingProgress';
import './devices.css';

export function PairPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const token = location.hash.replace(/^#/, '');

  const [code, setCode] = useState<string | null>(null);
  const [claimError, setClaimError] = useState<string | null>(null);
  const [openedAt] = useState(() => Date.now());
  const { connected } = usePairingProgress(code);
  // A link with no fragment carries no token; nothing is requested for it.
  const error = token.length === 0 ? strings.pairing.claimInvalid : claimError;

  // Claiming spends the token, so it happens once per token and not once per
  // mount: an effect that runs twice would answer its own first request with
  // "already claimed".
  const requested = useRef<string | null>(null);

  useEffect(() => {
    if (token.length === 0 || requested.current === token) return;
    requested.current = token;
    void api
      .claimPairingRequest(token)
      .then((claim) => setCode(claim.code))
      .catch((err: unknown) => setClaimError(claimErrorText(err)));
  }, [token]);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{strings.pairing.claimTitle}</h1>
          <p className="hint">{strings.pairing.claimIntro}</p>
        </div>
        <Button variant={connected ? 'primary' : 'default'} onClick={() => navigate('/devices')}>
          {connected ? strings.common.done : strings.common.cancel}
        </Button>
      </div>

      <div className="card pair-claim">
        {error ? (
          <p className="login-error" role="alert">
            {error}
          </p>
        ) : code === null ? (
          <p className="hint">{strings.pairing.claiming}</p>
        ) : (
          <>
            <p className="hint">
              {strings.pairing.singleUse} · <span className="mono pair-code">{code}</span>
            </p>
            <PairingSteps code={code} openedAt={openedAt} />
          </>
        )}
      </div>
    </>
  );
}

/** 404 and 410 both mean the host has to run the command again. */
function claimErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404 || err.status === 410) return strings.pairing.claimExpired;
    if (err.status === 409) return strings.pairing.claimUsed;
  }
  return strings.pairing.claimFailed;
}
