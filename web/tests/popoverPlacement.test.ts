/**
 * The geometry behind every popover. A jsdom rect is all zeros, so the flip and
 * the clamp are tested here rather than through a rendered menu.
 */
import { describe, expect, it } from 'vitest';
import { placementAt, placementFor } from '../src/components/popoverPlacement';

const VIEWPORT = { width: 1280, height: 800 };
const PANEL = { width: 200, height: 120 };

/** A trigger rect the way `getBoundingClientRect` reports one. */
const trigger = (left: number, top: number, width = 28, height = 28): DOMRect =>
  ({
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
    toJSON: () => ({}),
  }) as DOMRect;

describe('placementFor', () => {
  it('hangs the panel under the trigger with a gap', () => {
    const at = placementFor(trigger(400, 100), PANEL, VIEWPORT, 'start', 'bottom');

    expect(at).toEqual({ left: 400, top: 134, bottom: null });
  });

  it('lines the panel up with the right edge of the trigger', () => {
    const at = placementFor(trigger(400, 100), PANEL, VIEWPORT, 'end', 'bottom');

    expect(at.left).toBe(228);
  });

  it('flips above the trigger when the panel does not fit below', () => {
    const at = placementFor(trigger(400, 720), PANEL, VIEWPORT, 'start', 'bottom');

    expect(at.top).toBeNull();
    expect(at.bottom).toBe(86);
  });

  it('flips below when the side asked for is the one without room', () => {
    const at = placementFor(trigger(400, 20), PANEL, VIEWPORT, 'start', 'top');

    expect(at.bottom).toBeNull();
    expect(at.top).toBe(54);
  });

  it('stays on the asked-for side when neither side fits but it has more room', () => {
    const at = placementFor(trigger(400, 300), PANEL, { width: 1280, height: 420 }, 'start', 'bottom');

    expect(at.top).toBeNull();
    expect(at.bottom).toBe(126);
  });

  it('keeps the panel inside the viewport on both edges', () => {
    const right = placementFor(trigger(1240, 100), PANEL, VIEWPORT, 'start', 'bottom');
    expect(right.left).toBe(1072);

    const left = placementFor(trigger(4, 100), PANEL, VIEWPORT, 'end', 'bottom');
    expect(left.left).toBe(8);
  });
});

/** A55: a context menu opens with its corner at the pointer, as a native one does. */
describe('placementAt', () => {
  it('opens below and to the right of the pointer, corner on it', () => {
    expect(placementAt({ x: 400, y: 100 }, PANEL, VIEWPORT)).toEqual({
      left: 400,
      top: 100,
      bottom: null,
    });
  });

  it('flips above the pointer when the panel does not fit below', () => {
    const at = placementAt({ x: 400, y: 760 }, PANEL, VIEWPORT);

    expect(at.top).toBeNull();
    expect(at.bottom).toBe(40);
  });

  it('stays below when neither side fits but below has more room', () => {
    const at = placementAt({ x: 400, y: 30 }, PANEL, { width: 1280, height: 100 });

    expect(at.top).toBe(30);
    expect(at.bottom).toBeNull();
  });

  it('goes above when neither side fits but above has more room', () => {
    const at = placementAt({ x: 400, y: 70 }, PANEL, { width: 1280, height: 100 });

    expect(at.top).toBeNull();
    expect(at.bottom).toBe(30);
  });

  it('keeps the panel inside the viewport on both edges', () => {
    expect(placementAt({ x: 1270, y: 100 }, PANEL, VIEWPORT).left).toBe(1072);
    expect(placementAt({ x: 2, y: 100 }, PANEL, VIEWPORT).left).toBe(8);
  });

  it('puts a panel wider than the viewport at the edge rather than off it', () => {
    expect(placementAt({ x: 300, y: 100 }, { width: 500, height: 40 }, { width: 400, height: 800 }).left).toBe(
      8,
    );
  });
});
