/**
 * The live handshake of one pairing code, from the app socket.
 *
 * Both ways in share it: the Add device modal, which mints a code, and the
 * `/pair` page, which claims a host's scan token and is handed the same kind of
 * code back (A23).
 */
import { useConnection, type PairingProgress } from '../../stores/connection';
import type { PairingStep } from '../../protocol/frames';

export interface PairingLive {
  live: PairingProgress | null;
  step: PairingStep | null;
  /** The device answered: it is enrolled and its socket is up. */
  connected: boolean;
}

/** Nothing while another code's progress is the one in flight. */
export function usePairingProgress(code: string | null): PairingLive {
  const progress = useConnection((s) => s.pairing);
  const live = code && progress && progress.code === code ? progress : null;
  const step = live?.step ?? null;
  return { live, step, connected: step === 'online' || step === 'agents' };
}
