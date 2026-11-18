/**
 * The jump-to-latest control never covers a request that waits for the reader
 * (`docs/DESIGN.md` § "Reading position"). While any part of a pending
 * request's controls — an approval's buttons, a question's options, its
 * free-text field, its Submit — is inside the timeline's visible area, the
 * control is not drawn; once they have all left it, the usual rules apply
 * again, with the count of what arrived meanwhile.
 *
 * What is visible is read from layout: an IntersectionObserver whose root is
 * the timeline's scroller watches each control a pending card draws.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from 'react';

/** Starts watching one control; the function returned stops watching it. */
export type TrackControl = (control: Element) => () => void;

/** How a pending card hands its controls to the timeline it is drawn in. */
export const PendingControlContext = createContext<TrackControl | null>(null);

/** The part of an intersection entry that says whether a control is on screen. */
export type Sighting = Pick<IntersectionObserverEntry, 'target' | 'isIntersecting'>;

/**
 * The controls on screen once a batch of sightings is applied, in the order
 * the observer delivered them. A sighting of a control that is no longer
 * watched changes nothing: the observer can deliver one it queued before the
 * control went.
 */
export function controlsOnScreen(
  onScreen: ReadonlySet<Element>,
  sightings: readonly Sighting[],
  watched: ReadonlySet<Element>,
): Set<Element> {
  const next = new Set(onScreen);
  for (const { target, isIntersecting } of sightings) {
    if (isIntersecting && watched.has(target)) next.add(target);
    else next.delete(target);
  }
  return next;
}

/**
 * Whether the jump-to-latest control is drawn: only while the reader is away
 * from the tail and no pending control is on screen.
 */
export function showsBackToLatest(following: boolean, controlOnScreen: boolean): boolean {
  return !following && !controlOnScreen;
}

/**
 * Watches the pending controls drawn inside `scroller` and says whether any of
 * them is on screen. `track` goes to the cards through `PendingControlContext`.
 * A browser without IntersectionObserver never reports one, and the control
 * keeps to its other rules.
 */
export function usePendingControls(scroller: RefObject<HTMLElement | null>) {
  const [controlOnScreen, setControlOnScreen] = useState(false);
  const watched = useRef(new Set<Element>());
  const onScreen = useRef(new Set<Element>());
  const observer = useRef<IntersectionObserver | null>(null);

  const show = useCallback((next: Set<Element>) => {
    onScreen.current = next;
    setControlOnScreen(next.size > 0);
  }, []);

  const track = useCallback<TrackControl>(
    (control) => {
      watched.current.add(control);
      observer.current?.observe(control);
      return () => {
        watched.current.delete(control);
        observer.current?.unobserve(control);
        if (!onScreen.current.has(control)) return;
        const next = new Set(onScreen.current);
        next.delete(control);
        show(next);
      };
    },
    [show],
  );

  // Cards attach their controls before the timeline's effects run, so the
  // observer takes over every control already watched when it starts.
  useEffect(() => {
    const root = scroller.current;
    if (!root || typeof IntersectionObserver === 'undefined') return;
    const sightings = new IntersectionObserver(
      (entries) => show(controlsOnScreen(onScreen.current, entries, watched.current)),
      { root },
    );
    observer.current = sightings;
    for (const control of watched.current) sightings.observe(control);
    return () => {
      sightings.disconnect();
      observer.current = null;
      onScreen.current = new Set();
    };
  }, [scroller, show]);

  return { controlOnScreen, track };
}

/**
 * The ref a pending card puts on each of its controls, so the timeline around
 * it knows where they are. Outside a timeline it does nothing.
 */
export function usePendingControl(): (control: Element | null) => (() => void) | undefined {
  const track = useContext(PendingControlContext);
  return useCallback(
    (control: Element | null) => (control && track ? track(control) : undefined),
    [track],
  );
}
