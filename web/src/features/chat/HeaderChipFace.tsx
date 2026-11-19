import type { ReactNode } from 'react';

interface Props {
  icon: ReactNode;
  /** What the chip reads where there is room: "Todos 1/4", "Tasks 3". */
  words: string;
  /** What it reads on a phone beside its icon, "1/4" or "3"; null for the icon alone. */
  count: string | null;
}

/**
 * The face of a conversation header chip, Todos or Tasks. On a phone the words
 * and the chevron give way and the icon and number stay (`chat.css`), so the
 * conversation's title keeps its room. The chip names itself with its own
 * `aria-label`, so its accessible name is the same at every width.
 */
export function HeaderChipFace({ icon, words, count }: Props) {
  return (
    <>
      {icon}
      <span className="header-chip-words">{words}</span>
      {count === null ? null : (
        <span className="header-chip-count" aria-hidden>
          {count}
        </span>
      )}
    </>
  );
}
