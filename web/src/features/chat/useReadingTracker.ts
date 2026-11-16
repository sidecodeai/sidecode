/**
 * Keeps the reading position of the conversation on screen (`docs/DESIGN.md`
 * § "Reading position") — the `seq` of the furthest row whose top edge has been
 * inside the transcript — while the person reads, at most once a second, and
 * when they leave it: another conversation, another page, the tab hidden or
 * closed.
 */
import { useLayoutEffect, type RefObject } from 'react';
import { savePosition } from './readingPosition';

/** How often reading moves the saved position, at most. */
const SAVE_EVERY_MS = 1_000;

/**
 * The `seq` of the last row whose top edge is above the bottom of the
 * transcript. Rows are drawn in order, so their tops only grow, and the search
 * halves. A send not yet confirmed carries no `seq` and is not counted.
 */
export function furthestSeen(scroller: HTMLElement): number | null {
  const rows = [...scroller.querySelectorAll<HTMLElement>('[data-row-seq]')].filter(
    (row) => row.firstElementChild !== null,
  );
  const bottom = scroller.getBoundingClientRect().bottom;
  let low = 0;
  let high = rows.length - 1;
  let found: HTMLElement | null = null;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const row = rows[middle] as HTMLElement;
    if ((row.firstElementChild as Element).getBoundingClientRect().top < bottom) {
      found = row;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  const seq = Number(found?.dataset.rowSeq);
  return Number.isSafeInteger(seq) ? seq : null;
}

export function useReadingTracker(
  ref: RefObject<HTMLElement | null>,
  account: string | null,
  sessionKey: string,
): void {
  // A layout effect, so that leaving measures the rows before they are taken
  // off the page.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || account === null) return;
    let furthest: number | null = null;
    let timer: number | null = null;
    const keep = () => {
      const seq = el.isConnected ? furthestSeen(el) : null;
      if (seq !== null && (furthest === null || seq > furthest)) furthest = seq;
      if (furthest !== null) savePosition(account, sessionKey, furthest);
    };
    const onScroll = () => {
      if (timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        keep();
      }, SAVE_EVERY_MS);
    };
    const onHidden = () => {
      if (document.visibilityState === 'hidden') keep();
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    document.addEventListener('visibilitychange', onHidden);
    window.addEventListener('pagehide', keep);
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      el.removeEventListener('scroll', onScroll);
      document.removeEventListener('visibilitychange', onHidden);
      window.removeEventListener('pagehide', keep);
      keep();
    };
  }, [ref, account, sessionKey]);
}
