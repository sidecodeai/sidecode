/**
 * The jump-to-latest control is not drawn while any control of a
 * pending request is on screen (`src/features/chat/pendingControls.ts`).
 */
import { describe, expect, it } from 'vitest';
import {
  controlsOnScreen,
  showsBackToLatest,
  type Sighting,
} from '../src/features/chat/pendingControls';

const options = document.createElement('div');
const field = document.createElement('input');
const submit = document.createElement('div');
const watched = new Set<Element>([options, field, submit]);

const seen = (target: Element, isIntersecting: boolean): Sighting => ({ target, isIntersecting });

describe('controlsOnScreen', () => {
  it('adds a watched control that comes into view and drops one that leaves', () => {
    const entered = controlsOnScreen(new Set(), [seen(options, true), seen(field, true)], watched);
    expect([...entered]).toEqual([options, field]);

    const left = controlsOnScreen(entered, [seen(options, false)], watched);
    expect([...left]).toEqual([field]);
  });

  it('applies several sightings of one control in the order they came', () => {
    expect(controlsOnScreen(new Set(), [seen(submit, true), seen(submit, false)], watched).size).toBe(
      0,
    );
    expect([...controlsOnScreen(new Set(), [seen(submit, false), seen(submit, true)], watched)]).toEqual(
      [submit],
    );
  });

  it('ignores a sighting of a control that is no longer watched', () => {
    const gone = document.createElement('div');
    expect(controlsOnScreen(new Set(), [seen(gone, true)], watched).size).toBe(0);
  });

  it('leaves the set it was given untouched', () => {
    const before = new Set<Element>([options]);
    controlsOnScreen(before, [seen(options, false), seen(field, true)], watched);
    expect([...before]).toEqual([options]);
  });
});

describe('showsBackToLatest', () => {
  it('is drawn only away from the tail with no pending control on screen', () => {
    expect(showsBackToLatest(false, false)).toBe(true);
    expect(showsBackToLatest(false, true)).toBe(false);
    expect(showsBackToLatest(true, false)).toBe(false);
    expect(showsBackToLatest(true, true)).toBe(false);
  });
});
