import { Paperclip, Pencil, X } from 'lucide-react';
import { Popover } from '../../components/Popover';
import { strings } from '../../strings';
import type { QueuedMessage } from '../../protocol/types';
import './upNext.css';

interface Props {
  /** The device's snapshot, in the order the messages will be delivered. */
  queue: QueuedMessage[];
  /**
   * Whether a row can be taken back into the field at all: not while the
   * composer cannot send, and not while it already holds a queued message.
   */
  canEdit: boolean;
  onRemove: (queuedId: string) => void;
  onEdit: (item: QueuedMessage) => void;
}

/** How many files the device holds with a message; the field is absent for none. */
const filesOf = (item: QueuedMessage): number => item.attachments ?? 0;

/**
 * A43 — the queue as one control (`docs/DESIGN.md` § "Up next"). The chip that
 * leads the control row (A44) says how many messages wait behind the turn, and
 * only while one does; the list it opens has them in the order they will go,
 * one line each. × takes a message out of the line for good, and tapping one
 * edits it. A message that carries files has only the ×: its files are on the
 * device, and nothing can bring them back into the field.
 */
export function UpNext({ queue, canEdit, onRemove, onEdit }: Props) {
  // The last one removed takes the chip with it, and the open list goes too.
  if (queue.length === 0) return null;
  return (
    <Popover
      side="top"
      align="start"
      chevron={false}
      label={strings.composer.upNextCount(queue.length)}
    >
      {(close) => (
        <div className="up-next">
          <p className="up-next-title">{strings.composer.upNext}</p>
          <ol className="up-next-list">
            {queue.map((item) => (
              <UpNextRow
                key={item.id}
                item={item}
                editable={canEdit && filesOf(item) === 0}
                onRemove={onRemove}
                onEdit={(entry) => {
                  // The list closes as the words go into the field.
                  close();
                  onEdit(entry);
                }}
              />
            ))}
          </ol>
        </div>
      )}
    </Popover>
  );
}

function UpNextRow({
  item,
  editable,
  onRemove,
  onEdit,
}: {
  item: QueuedMessage;
  editable: boolean;
  onRemove: (queuedId: string) => void;
  onEdit: (item: QueuedMessage) => void;
}) {
  const files = filesOf(item);
  const words = (
    <>
      <span className="up-next-words">{item.text}</span>
      {files > 0 ? (
        <span className="up-next-files">
          <Paperclip size={12} aria-hidden />
          <span aria-hidden>{files}</span>
          <span className="sr-only">{strings.chat.attachments(files)}</span>
        </span>
      ) : null}
    </>
  );
  return (
    <li className="up-next-row">
      {editable ? (
        <button
          type="button"
          className="up-next-message"
          title={item.text}
          onClick={() => onEdit(item)}
        >
          {words}
        </button>
      ) : (
        <span className="up-next-message static" title={item.text}>
          {words}
        </span>
      )}
      <button
        type="button"
        className="icon-btn"
        aria-label={strings.chat.queuedRemove}
        onClick={() => onRemove(item.id)}
      >
        <X size={14} />
      </button>
    </li>
  );
}

/**
 * A43: what sits over the field while it holds a queued message. Cancel puts
 * the words back as they were queued, in the place they left; while the words
 * are on their way back there is nothing left to cancel.
 */
export function EditingStrip({ sending, onCancel }: { sending: boolean; onCancel: () => void }) {
  return (
    <div className="editing-strip">
      <span className="editing-strip-text">
        <Pencil size={13} aria-hidden />
        {strings.composer.editingQueued}
      </span>
      <button type="button" className="link-btn" disabled={sending} onClick={onCancel}>
        {strings.common.cancel}
      </button>
    </div>
  );
}
