import { Paperclip } from 'lucide-react';
import { cx } from '../../../lib/cx';
import { useNow } from '../../../lib/useNow';
import { strings } from '../../../strings';
import { deliveryLabel } from '../attach';
import { isUnconfirmed, type OptimisticBlock } from '../../../stores/timeline';
import type { UserMessageEvent } from '../../../protocol/types';

export function UserMessageRow({
  event,
  pending,
}: {
  event: UserMessageEvent;
  /** A12: set while this app is still waiting for the device to echo the send. */
  pending?: OptimisticBlock;
}) {
  // A10: on a shared session the device may still be holding this message.
  const delivery = deliveryLabel(event.delivery);
  const origin = originLabel(event.source);
  return (
    <div className="user-row">
      <div className={cx('user-bubble', pending !== undefined && 'pending')}>
        {origin ? <span className="user-origin">{origin}</span> : null}
        <p>{event.text}</p>
        {event.attachments && event.attachments.length > 0 ? (
          <p className="user-attachments">
            <Paperclip size={12} aria-hidden />
            {strings.chat.attachments(event.attachments.length)}
          </p>
        ) : null}
        {pending ? <PendingChip block={pending} /> : null}
        {!pending && delivery ? <span className="delivery-chip">{delivery}</span> : null}
      </div>
    </div>
  );
}

/**
 * The caption above the bubble. A message this app or another one sent needs
 * none; a terminal-typed one says where it was typed. A34: a message another
 * agent filed never reaches this row — `AgentMessageRow` draws it on the
 * agent's side instead. A35: the one prompt the device wrote for the person,
 * once the usage limit reset, says so.
 */
function originLabel(source: UserMessageEvent['source']): string | null {
  if (source === 'terminal') return strings.chat.fromTerminal;
  if (source === 'resume') return strings.chat.fromResume;
  return null;
}

/** The chip on a send the device has not confirmed, re-read on a slow clock. */
function PendingChip({ block }: { block: OptimisticBlock }) {
  const now = useNow(5_000);
  return <span className="delivery-chip">{pendingLabel(block, now)}</span>;
}

/**
 * A14: a steered message is accepted at once but only reaches the agent at its
 * next step, so it says so instead of counting the seconds towards a delivery
 * problem it does not have.
 */
function pendingLabel(block: OptimisticBlock, now: number): string {
  if (block.accepted === 'steered') return strings.chat.steering;
  if (isUnconfirmed(block, now)) return strings.composer.deliveryUnconfirmed;
  return strings.chat.sending;
}
