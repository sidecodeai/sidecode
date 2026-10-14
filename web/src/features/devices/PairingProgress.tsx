/**
 * The handshake a pairing code walks through, drawn the same way wherever a
 * code came from: minted in the Add device modal, or claimed from a host's QR
 * code on `/pair` (A23).
 */
import { useMemo, type ReactNode } from 'react';
import { Check } from 'lucide-react';
import { clock } from '../../lib/format';
import { strings } from '../../strings';
import { useNow } from '../../lib/useNow';
import { usePairingProgress } from './usePairingProgress';
import type { PairingStep } from '../../protocol/frames';

const STEP_ORDER: Record<PairingStep, number> = { waiting: 0, enrolled: 1, online: 2, agents: 3 };

/** `openedAt` is when this screen started listening, so it can say how long. */
export function PairingSteps({ code, openedAt }: { code: string | null; openedAt: number }) {
  const { live, step, connected } = usePairingProgress(code);
  const now = useNow(500);
  const elapsed = Math.max(0, now - openedAt);

  const agentChips = useMemo(
    () => live?.device?.agents.filter((a) => a.available).map((a) => a.agent) ?? [],
    [live],
  );

  return (
    <div className="pair-steps">
      <div className="pair-steps-head">
        <span className="pair-steps-title">
          <span className={`dot ${connected ? 'running' : 'pulse'}`} aria-hidden />
          {connected && live?.device
            ? strings.pairing.connected(live.device.name)
            : strings.pairing.waiting}
        </span>
        <span className="mono hint">{strings.pairing.listening(clock(elapsed))}</span>
      </div>
      <div className="pair-progress" aria-hidden>
        <span style={{ width: `${progressWidth(step)}%` }} />
      </div>
      <ol className="pair-step-list">
        <Step done label={strings.pairing.stepGateway} />
        <Step
          done={rank(step) >= STEP_ORDER.enrolled}
          active={rank(step) < STEP_ORDER.enrolled}
          label={strings.pairing.stepHandshake}
        />
        <Step
          done={rank(step) >= STEP_ORDER.agents}
          active={rank(step) === STEP_ORDER.online}
          label={strings.pairing.stepAgents}
          trailing={
            agentChips.length > 0 ? (
              <span className="mono pair-agents">{agentChips.join(' · ')}</span>
            ) : null
          }
        />
      </ol>
    </div>
  );
}

function Step({
  done,
  active,
  label,
  trailing,
}: {
  done?: boolean;
  active?: boolean;
  label: string;
  trailing?: ReactNode;
}) {
  return (
    <li className={done ? 'done' : active ? 'active' : ''}>
      <span className="pair-step-mark" aria-hidden>
        {done ? <Check size={11} strokeWidth={3} /> : null}
      </span>
      <span className="pair-step-label">{label}</span>
      {trailing}
    </li>
  );
}

const rank = (step: PairingStep | null): number => (step ? STEP_ORDER[step] : -1);

function progressWidth(step: PairingStep | null): number {
  const r = rank(step);
  if (r < 0) return 12;
  return Math.min(100, 25 * (r + 1));
}
