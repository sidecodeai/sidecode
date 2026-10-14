/**
 * Where a popover panel goes, in viewport coordinates.
 *
 * The panel is rendered in a portal on `document.body`, so it is placed against
 * the viewport rather than against its trigger. That is what keeps it out of
 * every `overflow: hidden` list surface and every scrolling pane it sits in.
 */

export type Align = 'start' | 'end';
export type Side = 'top' | 'bottom';

/** Gap between the trigger and the panel, and the margin kept to the viewport. */
const GAP = 6;
const EDGE = 8;

export interface Placement {
  left: number;
  /** Exactly one of the two is a number; the other side is left unset. */
  top: number | null;
  bottom: number | null;
}

export interface Viewport {
  width: number;
  height: number;
}

/**
 * Anchors the panel to `trigger`, flipping to the other side only when the
 * asked-for one cannot hold the panel and the other one can hold more of it,
 * and keeping the panel inside the viewport horizontally.
 */
export function placementFor(
  trigger: DOMRect,
  panel: { width: number; height: number },
  viewport: Viewport,
  align: Align,
  side: Side,
): Placement {
  const below = viewport.height - trigger.bottom - GAP - EDGE;
  const above = trigger.top - GAP - EDGE;

  let placeBelow = side === 'bottom';
  if (placeBelow && panel.height > below && above > below) placeBelow = false;
  else if (!placeBelow && panel.height > above && below > above) placeBelow = true;

  const left = Math.min(
    Math.max(align === 'start' ? trigger.left : trigger.right - panel.width, EDGE),
    Math.max(EDGE, viewport.width - panel.width - EDGE),
  );

  return {
    left: Math.round(left),
    top: placeBelow ? Math.round(trigger.bottom + GAP) : null,
    bottom: placeBelow ? null : Math.round(viewport.height - trigger.top + GAP),
  };
}
