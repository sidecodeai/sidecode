import { useState } from 'react';
import { cx } from '../../../lib/cx';
import { deciderLabel, strings } from '../../../strings';
import type { ApprovalEvent } from '../../../protocol/types';
import { DiffView } from './DiffView';
import { OutputBox } from './OutputBox';
import { PlanCard } from './PlanCard';
import { ToolIcon } from './toolIcons';
import { toolCategory } from './toolCategory';

interface Props {
  event: ApprovalEvent;
  onDecide: (requestId: string, optionId: string, message?: string) => Promise<void>;
}

const ORDER: Record<string, number> = { primary: 0, secondary: 1, danger: 2 };

export function ApprovalCard({ event, onDecide }: Props) {
  if (event.approval_kind === 'plan') return <PlanCard event={event} onDecide={onDecide} />;
  return <ToolApprovalCard event={event} onDecide={onDecide} />;
}

function ToolApprovalCard({ event, onDecide }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const pending = event.status === 'pending';
  const options = [...event.options].sort(
    (a, b) => (ORDER[a.style] ?? 1) - (ORDER[b.style] ?? 1),
  );
  // A11 §5.7: a shared request answered in the terminal resolves with the
  // reserved `elsewhere` id, which matches none of the options on purpose.
  const elsewhere = event.decision?.option_id === 'elsewhere';
  const chosen = event.decision
    ? (event.options.find((o) => o.id === event.decision?.option_id)?.label ?? event.decision.option_id)
    : null;

  return (
    <section className={cx('approval', !pending && 'resolved')} aria-live="polite">
      <header className="approval-head">
        <ToolIcon category={toolCategory(event.tool, event.tool_kind)} size={14} />
        <span className="approval-tool">{event.tool}</span>
        <span className="approval-title mono">{event.title}</span>
      </header>

      {event.diff?.patch ? <DiffView diff={event.diff} /> : null}
      {event.input !== undefined ? (
        <OutputBox text={JSON.stringify(event.input, null, 2)} />
      ) : null}

      {pending ? (
        <div className="approval-actions">
          {options.map((option) => (
            <button
              key={option.id}
              type="button"
              className={cx('btn', 'small', option.style === 'primary' && 'primary', option.style === 'danger' && 'danger')}
              disabled={busy !== null}
              onClick={async () => {
                setBusy(option.id);
                try {
                  await onDecide(event.request_id, option.id);
                } finally {
                  setBusy(null);
                }
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : (
        <p className="approval-result hint">
          {event.status === 'expired'
            ? strings.chat.approvalExpired
            : elsewhere
              ? strings.chat.approvalElsewhere
              : chosen
                ? strings.chat.approvalResolved(deciderLabel(event.decision?.by ?? 'policy'), chosen)
                : strings.chat.questionResolved}
        </p>
      )}
    </section>
  );
}
