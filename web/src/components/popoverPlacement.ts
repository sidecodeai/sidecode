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

/** A point in viewport coordinates: where a pointer was, or where a row ends. */
export interface Point {
  x: number;
  y: number;
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

/**
 * Opens a context menu's corner at `point`, as a native one does: below it and
 * to its right, flipping above only when the space below cannot hold the panel
 * and the space above holds more of it, and kept inside the viewport
 * horizontally.
 */
export function placementAt(
  point: Point,
  panel: { width: number; height: number },
  viewport: Viewport,
): Placement {
  const below = viewport.height - point.y - EDGE;
  const above = point.y - EDGE;
  const placeBelow = !(panel.height > below && above > below);

  const left = Math.min(
    Math.max(point.x, EDGE),
    Math.max(EDGE, viewport.width - panel.width - EDGE),
  );

  return {
    left: Math.round(left),
    top: placeBelow ? Math.round(point.y) : null,
    bottom: placeBelow ? null : Math.round(viewport.height - point.y),
  };
}
