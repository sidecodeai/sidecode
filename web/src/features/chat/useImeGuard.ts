import { useCallback, useEffect, useRef, type KeyboardEvent } from 'react';

/**
 * How long a plain Enter waits before it sends. Long enough for the
 * `compositionend` that follows an input method's confirming Enter in WebKit
 * to arrive and cancel it; far too short for a person to notice.
 */
export const SEND_DELAY_MS = 60;

/**
 * Tells an Enter that belongs to the input method from one meant for Send.
 *
 * Under a Chinese or Japanese IME the letters are composed first and Enter
 * confirms them; that Enter must not send. Browsers disagree on how that
 * Enter looks. Chrome and Firefox mark the keydown itself (`isComposing`,
 * keyCode 229) and the composition events bracket it. WebKit fires the
 * keydown and the `compositionend` of one key press as two events with
 * `isComposing` already false on the keydown, and the order between the two
 * has not been the same in every version (WebKit bug 165004). So the guard
 * asks three things: is a composition open or marked on the event; did one
 * end in this very task; and — for the order the first two cannot see, keydown
 * before compositionend — a plain Enter does not send at once but a moment
 * later, and a `compositionend` arriving in that moment cancels it.
 *
 * A57: it also keeps dictated words out of a composition. Writing the
 * field while the input method holds marked text would break what is being
 * composed, so a landing waits for the composition to end, committed or
 * cancelled, and lands in the next task, after the input that ends it.
 */
export function useImeGuard() {
  const composing = useRef(false);
  const endedThisTask = useRef(false);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Landings waiting for the composition to end, oldest first. */
  const held = useRef<(() => void)[]>([]);

  const cancelPending = useCallback(() => {
    if (pending.current !== null) {
      clearTimeout(pending.current);
      pending.current = null;
    }
  }, []);

  /** Land what waited, unless a new composition has begun since. */
  const release = useCallback((force: boolean) => {
    if (composing.current && !force) return;
    const landings = held.current;
    held.current = [];
    for (const land of landings) land();
  }, []);

  const onCompositionStart = useCallback(() => {
    composing.current = true;
    cancelPending();
  }, [cancelPending]);

  const onCompositionEnd = useCallback(() => {
    composing.current = false;
    endedThisTask.current = true;
    // An Enter that arrived just before this event was the input method's.
    cancelPending();
    setTimeout(() => {
      endedThisTask.current = false;
      release(false);
    }, 0);
  }, [cancelPending, release]);

  /** A57: run `land` now, or once the composition the field holds has ended. */
  const whenComposed = useCallback((land: () => void) => {
    if (composing.current) held.current.push(land);
    else land();
  }, []);

  const ownsEnter = useCallback(
    (e: KeyboardEvent) =>
      composing.current ||
      e.nativeEvent.isComposing ||
      e.nativeEvent.keyCode === 229 ||
      endedThisTask.current,
    [],
  );

  /** Run `send` after `SEND_DELAY_MS`, unless a composition ends first. */
  const sendUnlessComposing = useCallback(
    (send: () => void) => {
      cancelPending();
      pending.current = setTimeout(() => {
        pending.current = null;
        send();
      }, SEND_DELAY_MS);
    },
    [cancelPending],
  );

  // A send still waiting when the composer goes away belongs to nobody; words
  // still waiting belong to the draft, and land as the field goes.
  useEffect(
    () => () => {
      cancelPending();
      release(true);
    },
    [cancelPending, release],
  );

  return { onCompositionStart, onCompositionEnd, ownsEnter, sendUnlessComposing, whenComposed };
}
