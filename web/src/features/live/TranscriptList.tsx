import { useLayoutEffect, useRef } from 'react';
import { cx } from '../../lib/cx';
import { liveStrings as t } from './strings';
import type { Entry } from './transcript';

/** Within this many pixels of the end, a reader counts as at the end. */
const PINNED_PX = 24;

interface Props {
  entries: Entry[];
  /** A running call: the list stays at its end while the person has not scrolled up. */
  follow?: boolean;
  className?: string;
}

/** The conversation, turn after turn. A message the assistant sent shows as a quiet card. */
export function TranscriptList({ entries, follow = false, className }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  // After every render, not only a new line: the box also changes size (a draft card appears
  // under it, the card gets shorter) and a reader at the end should still be at the end.
  useLayoutEffect(() => {
    const el = box.current;
    if (follow && el && pinned.current) el.scrollTop = el.scrollHeight;
  });

  const onScroll = (): void => {
    const el = box.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight <= PINNED_PX;
  };

  return (
    <div
      ref={box}
      className={cx('live-transcript', className)}
      role="log"
      aria-label={t.conversation}
      aria-live="off"
      tabIndex={0}
      onScroll={onScroll}
    >
      {entries.length === 0 ? <p className="live-transcript-empty">{t.transcriptEmpty}</p> : null}
      {entries.map((entry, index) =>
        entry.kind === 'turn' ? (
          <p key={index} className={`live-turn live-turn-${entry.speaker}`}>
            <span className="live-who">{entry.speaker === 'user' ? t.you : t.assistant}</span>
            {entry.text}
          </p>
        ) : (
          <div key={index} className="live-sent-entry">
            <span className="live-who">{t.sentTo(entry.session)}</span>
            <p>{entry.text}</p>
          </div>
        ),
      )}
    </div>
  );
}
