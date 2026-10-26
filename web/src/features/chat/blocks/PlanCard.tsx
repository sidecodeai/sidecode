/**
 * A54 — a plan Claude wants approved. The plan is Markdown in `input.plan`;
 * the options are Claude Code's own choices. "Keep planning" opens a field for
 * what to change, and sends it as the approval's `message`; without one the
 * turn is interrupted, as Escape does in the terminal.
 */
import { useState } from 'react';
import { cx } from '../../../lib/cx';
import { deciderLabel, strings } from '../../../strings';
import type { ApprovalEvent } from '../../../protocol/types';
import { MarkdownText } from '../MarkdownText';

interface Props {
  event: ApprovalEvent;
  onDecide: (requestId: string, optionId: string, message?: string) => Promise<void>;
}

const ORDER: Record<string, number> = { primary: 0, secondary: 1, danger: 2 };
const FOLD_LINES = 14;

export function PlanCard({ event, onDecide }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [giving, setGiving] = useState(false);
  const [feedback, setFeedback] = useState('');
  const pending = event.status === 'pending';
  const plan = typeof event.input?.plan === 'string' ? event.input.plan : '';
  const long = plan.split('\n').length > FOLD_LINES;
  const options = [...event.options].sort(
    (a, b) => (ORDER[a.style] ?? 1) - (ORDER[b.style] ?? 1),
  );
  const label = (id: string, fallback: string): string => strings.chat.planOptions[id] ?? fallback;
  const elsewhere = event.decision?.option_id === 'elsewhere';
  const chosen = event.decision
    ? label(
        event.decision.option_id,
        event.options.find((o) => o.id === event.decision?.option_id)?.label ?? event.decision.option_id,
      )
    : null;

  async function decide(optionId: string, message?: string): Promise<void> {
    setBusy(optionId);
    try {
      await onDecide(event.request_id, optionId, message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className={cx('approval', 'plan', !pending && 'resolved')} aria-live="polite">
      <header className="approval-head">
        <span className="approval-tool">{strings.chat.planReady}</span>
        <span className="approval-title">{event.title.replace(/^Plan ready: ?/, '')}</span>
      </header>

      <div className={cx('plan-body', long && !open && 'folded')}>
        <MarkdownText text={plan} />
      </div>
      {long ? (
        <button type="button" className="btn small plan-toggle" onClick={() => setOpen(!open)}>
          {open ? strings.chat.planCollapse : strings.chat.planShowFull}
        </button>
      ) : null}
      {event.plan_truncated ? <p className="approval-result hint">{strings.chat.planTruncated}</p> : null}

      {pending && giving ? (
        <div className="plan-feedback">
          <textarea
            className="field"
            rows={3}
            autoFocus
            aria-label={strings.chat.planFeedbackLabel}
            placeholder={strings.chat.planFeedbackPlaceholder}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
          />
          <div className="approval-actions">
            <button
              type="button"
              className="btn small danger"
              disabled={busy !== null}
              onClick={() => void decide('plan_keep', feedback.trim() || undefined)}
            >
              {strings.chat.planFeedbackSend}
            </button>
            <button
              type="button"
              className="btn small"
              disabled={busy !== null}
              onClick={() => setGiving(false)}
            >
              {strings.chat.planFeedbackCancel}
            </button>
          </div>
        </div>
      ) : pending ? (
        <div className="approval-actions">
          {options.map((option) => (
            <button
              key={option.id}
              type="button"
              className={cx('btn', 'small', option.style === 'primary' && 'primary', option.style === 'danger' && 'danger')}
              disabled={busy !== null}
              onClick={() => (option.id === 'plan_keep' ? setGiving(true) : void decide(option.id))}
            >
              {label(option.id, option.label)}
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
