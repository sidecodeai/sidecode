/**
 * One settings row: a title, one sentence under it, and the control at the
 * trailing edge (`docs/DESIGN.md` § "The Settings screen"). No rule parts two
 * rows and nothing is drawn beside a row for a state — a row whose state has
 * something to say says it in place of its sentence.
 */
import { useId, useRef, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { cx } from '../../lib/cx';

interface RowProps {
  title: string;
  /** The row's own sentence, or what its state has to say instead. */
  sentence: ReactNode;
  /** The switch, menu or segmented control at the trailing edge. */
  control: ReactNode;
  /**
   * A row a click anywhere on reaches: the ruling gives this to a menu and to a
   * switch, never to a segmented control, where there is no one control to
   * reach. The control keeps its own role and its own click.
   */
  target?: boolean;
}

export function SettingsRow({ title, sentence, control, target }: RowProps) {
  const controlRef = useRef<HTMLSpanElement>(null);

  return (
    <div
      className={cx('settings-row', target && 'target')}
      onClick={(event) => {
        if (!target) return;
        // The control answers for itself; the row only stands in for it, and a
        // control with nothing to say (disabled) leaves the row inert.
        if ((event.target as HTMLElement).closest('.settings-control')) return;
        controlRef.current?.querySelector<HTMLButtonElement>('button:not([disabled])')?.click();
      }}
    >
      <RowText title={title} sentence={sentence} />
      <span className="settings-control" ref={controlRef}>
        {control}
      </span>
    </div>
  );
}

interface ActionProps {
  title: string;
  sentence: ReactNode;
  onPress: () => void;
  /** Sign out, which takes something away rather than leading somewhere. */
  danger?: boolean;
}

/** A row that is the action itself: the whole row is the button. */
export function SettingsActionRow({ title, sentence, onPress, danger }: ActionProps) {
  const sentenceId = useId();
  return (
    <button
      type="button"
      className="settings-row target"
      aria-label={title}
      aria-describedby={sentenceId}
      onClick={onPress}
    >
      <RowText title={title} sentence={sentence} sentenceId={sentenceId} danger={danger} />
      {danger ? null : <ChevronRight size={16} aria-hidden className="settings-row-chevron" />}
    </button>
  );
}

function RowText({
  title,
  sentence,
  sentenceId,
  danger,
}: {
  title: string;
  sentence: ReactNode;
  sentenceId?: string;
  danger?: boolean;
}) {
  return (
    <span className="settings-row-text">
      <span className={cx('settings-row-title', danger && 'danger')}>{title}</span>
      <span className="settings-row-sentence" id={sentenceId}>
        {sentence}
      </span>
    </span>
  );
}
