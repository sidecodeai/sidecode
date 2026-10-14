import type { ReactNode } from 'react';
import { cx } from '../../lib/cx';
import { pairKey, type LabelPair } from './modelLabels';

/**
 * A box as wide as the widest thing it can ever hold. `docs/DESIGN.md`
 * § "The composer": the chip and the card's name row stay the width of the
 * widest model-and-effort combination the agent offers, so choosing a level or
 * a model never shifts what sits beside them.
 *
 * The visible content and every alternative share one grid cell, so the grid
 * takes the widest of them. The alternatives are laid out but not painted —
 * `visibility: hidden` keeps their size where `display: none` would not — and
 * they are out of the accessibility tree.
 */
export function SizedBox({
  className,
  alternatives,
  alternative,
  children,
}: {
  className?: string;
  alternatives: LabelPair[];
  /** Draws one alternative exactly the way the visible content is drawn. */
  alternative: (pair: LabelPair) => ReactNode;
  children: ReactNode;
}) {
  return (
    <span className={cx('sized-box', className)}>
      <span className="sized-box-shown">{children}</span>
      {alternatives.map((pair) => (
        <span className="sized-box-ghost" aria-hidden key={pairKey(pair)}>
          {alternative(pair)}
        </span>
      ))}
    </span>
  );
}
