import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { api, type PairingResponse } from '../../lib/api';
import { clock } from '../../lib/format';
import { strings } from '../../strings';
import { useConnection } from '../../stores/connection';
import { useNow } from '../../lib/useNow';
import { PairingSteps } from './PairingProgress';
import { usePairingProgress } from './usePairingProgress';

/**
 * The one command to run on the host. The gateway hands out `install.macos` and
 * `install.linux` so a platform whose command really differs can be added
 * without a wire change, but the two are the same string today: the installer
 * tells macOS from Linux itself with `uname`. So the modal reads one of them and
 * asks nobody which platform they are on.
 */
const installCommand = (pairing: PairingResponse | null): string => pairing?.install.macos ?? '';

/** Mounted only while open so each visit requests exactly one pairing code. */
export function AddDeviceModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return <AddDevice onClose={onClose} />;
}

function AddDevice({ onClose }: { onClose: () => void }) {
  const [pairing, setPairing] = useState<PairingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [manual, setManual] = useState(false);
  const [copied, setCopied] = useState<'code' | 'scan' | null>(null);
  const [attempt, setAttempt] = useState(0);

  const clearPairing = useConnection((s) => s.clearPairing);
  const clockSkewMs = useConnection((s) => s.clockSkewMs);
  const now = useNow(500);
  // The dialog requests the code on mount, so this is when listening started.
  const [openedAt] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    void api
      .createPairing()
      .then((response) => {
        if (!cancelled) setPairing(response);
      })
      .catch(() => {
        if (!cancelled) setError(strings.pairing.createFailed);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const { connected } = usePairingProgress(pairing?.code ?? null);

  const close = useCallback(() => {
    if (pairing && !connected) void api.cancelPairing(pairing.code).catch(() => undefined);
    clearPairing();
    onClose();
  }, [pairing, connected, clearPairing, onClose]);

  const command = installCommand(pairing);
  // `expires_at` is a gateway timestamp, so compare it against the gateway clock.
  const serverNow = now + clockSkewMs;
  const remaining = pairing ? Math.max(0, pairing.expires_at - serverNow) : 0;
  const expired = pairing !== null && remaining === 0 && !connected;
  const scanCommand = strings.pairing.scanCommand(window.location.origin);

  return (
    <Modal
      open
      onClose={close}
      title={strings.pairing.title}
      width={580}
      footer={
        <>
          <Button onClick={close}>{strings.common.cancel}</Button>
          <Button variant="primary" disabled={!connected} onClick={close}>
            {strings.common.continue}
          </Button>
        </>
      }
    >
      <p className="hint pairing-intro">{strings.pairing.intro}</p>

      {error ? <p className="login-error">{error}</p> : null}

      <div className="pair-command">
        <div className="pair-command-top">
          <code>{command || ' '}</code>
          <Button
            small
            disabled={!command}
            onClick={async () => {
              await copyText(command);
              setCopied('code');
              window.setTimeout(() => setCopied(null), 1400);
            }}
          >
            {copied === 'code' ? strings.common.copied : strings.common.copy}
          </Button>
        </div>
        <div className="pair-command-foot">
          <span className="mono pair-code">{pairing?.code ?? '—'}</span>
          <span className="pair-sep" aria-hidden />
          <span className="hint">
            {strings.pairing.singleUse} ·{' '}
            {expired ? strings.pairing.expired : strings.pairing.expiresIn(clock(remaining))}
          </span>
          {expired ? (
            <button type="button" className="link-btn" onClick={() => setAttempt((n) => n + 1)}>
              {strings.pairing.newCode}
            </button>
          ) : null}
        </div>
      </div>

      <PairingSteps code={pairing?.code ?? null} openedAt={openedAt} />

      <section className="pair-scan">
        <h3 className="pair-scan-title">{strings.pairing.scanTitle}</h3>
        <div className="pair-scan-command">
          <code>{scanCommand}</code>
          <Button
            small
            onClick={async () => {
              await copyText(scanCommand);
              setCopied('scan');
              window.setTimeout(() => setCopied(null), 1400);
            }}
          >
            {copied === 'scan' ? strings.common.copied : strings.common.copy}
          </Button>
        </div>
        <p className="hint">{strings.pairing.scanBody}</p>
      </section>

      <p className="pair-manual">
        {strings.pairing.noCurl}{' '}
        <button type="button" className="link-btn" onClick={() => setManual((v) => !v)}>
          {strings.pairing.manualInstall}
        </button>
      </p>

      {manual ? (
        <ol className="pair-manual-steps">
          {strings.pairing.manualSteps.map((line) => (
            <li key={line}>{line}</li>
          ))}
          <li>
            <code>
              {strings.pairing.manualPairCommand(
                window.location.origin,
                pairing?.code ?? 'RC-XXXX-XXXX',
              )}
            </code>
          </li>
        </ol>
      ) : null}
    </Modal>
  );
}

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
}
