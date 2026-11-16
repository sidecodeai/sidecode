import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

const BOTTOM_THRESHOLD_PX = 56;
const TOP_TRIGGER_PX = 120;

/** The opening row's top sits this far below the top of the transcript. */
const ANCHOR_MARGIN_PX = 12;

/**
 * How long an opening is held while the rows above it settle — Markdown
 * arriving, a lazy part measured — unless the reader scrolls first.
 */
export const SETTLE_MS = 2_000;

/** Keys that scroll a page, which count as the reader scrolling the transcript. */
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);

/** Where the transcript opens: not decided yet, the tail, or a row to put at the top. */
export type Opening = { kind: 'wait' } | { kind: 'tail' } | { kind: 'row'; key: string };

const TAIL: Opening = { kind: 'tail' };

interface Options {
  /** Changes on every event, streaming and unconfirmed sends included. */
  revision: string | number;
  /**
   * Changes when the same blocks are drawn differently — the timeline's detail
   * level. A redraw is not new content: it counts nothing and moves no anchor.
   */
  redraw: string | number;
  /** Key of the first drawn row, so a prepended history page can be told from a tail append. */
  firstKey: string | null;
  /** Key of the last drawn row; a change means a genuinely new block arrived. */
  lastKey: string | null;
  onReachTop: () => void;
  /** Where the transcript opens; the tail when nothing says otherwise. */
  opening?: Opening;
}

/** The first box drawn for a row: the row's own element, its wrapper having none. */
function rowBox(scroller: HTMLElement, key: string): Element | null {
  for (const row of scroller.querySelectorAll<HTMLElement>('[data-row-key]')) {
    if (row.dataset.rowKey === key) return row.firstElementChild;
  }
  return null;
}

/**
 * Whether a key scrolls the transcript: a scrolling key pressed on the page or
 * in the transcript, not in a field or on a control that takes the key itself.
 */
function scrollsPage(event: KeyboardEvent, scroller: HTMLElement): boolean {
  if (!SCROLL_KEYS.has(event.key)) return false;
  const target = event.target;
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target.closest('input, textarea, select, button')) return false;
  return target === document.body || scroller.contains(target);
}

/**
 * Keeps the timeline pinned to the bottom while the user has not scrolled away,
 * counts new blocks missed otherwise, and preserves the scroll anchor when an
 * older history page is prepended.
 *
 * It also opens the transcript where `docs/DESIGN.md` § "Reading position"
 * says: the opening row's top at the top of the transcript, less a small
 * margin, or the tail when the rows from it to the end fit. The opening is
 * placed once it is decided and placed again as the rows settle, until the
 * reader scrolls, touches the transcript or jumps to the latest, or
 * `SETTLE_MS` after it was first placed. Older history is paged in from the top
 * only once the reader has scrolled themselves.
 */
export function useScrollFollow({
  revision,
  redraw,
  firstKey,
  lastKey,
  onReachTop,
  opening = TAIL,
}: Options) {
  const ref = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const [missed, setMissed] = useState(0);
  // The opening is over — the reader took over, or the rows settled.
  const [settled, setSettled] = useState(false);
  const previousHeight = useRef(0);
  const previousFirstKey = useRef(firstKey);
  // The two effects below each need the redraw they last acted on, and the
  // anchor effect runs first, so they cannot share one.
  const anchoredRedraw = useRef(redraw);
  const countedRedraw = useRef(redraw);
  // The last row counted, or seen when the opening was first placed: rows on
  // screen when the transcript opened were not missed.
  const countedLastKey = useRef(lastKey);
  // Written only from event handlers and effects, never during render.
  const followingRef = useRef(true);
  const settledRef = useRef(false);
  const settleTimer = useRef<number | null>(null);
  const readerScrolled = useRef(false);

  const follow = useCallback((atBottom: boolean) => {
    followingRef.current = atBottom;
    setFollowing(atBottom);
    if (atBottom) setMissed(0);
  }, []);

  const settle = useCallback(() => {
    if (settledRef.current) return;
    settledRef.current = true;
    setSettled(true);
    if (settleTimer.current !== null) window.clearTimeout(settleTimer.current);
    settleTimer.current = null;
  }, []);

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior = 'auto') => {
      const el = ref.current;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight, behavior });
      follow(true);
    },
    [follow],
  );

  /** The jump-to-latest control: the reader's own choice, so the opening is over. */
  const jumpToLatest = useCallback(() => {
    settle();
    scrollToBottom('smooth');
  }, [settle, scrollToBottom]);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance <= BOTTOM_THRESHOLD_PX;
    followingRef.current = atBottom;
    setFollowing(atBottom);
    if (atBottom) setMissed(0);
    // An opening placed near the top is not the reader asking for more.
    if (el.scrollTop <= TOP_TRIGGER_PX && readerScrolled.current) onReachTop();
  }, [onReachTop]);

  /**
   * Puts the row's top at the top of the transcript, less the margin, or as
   * close as the end allows: a row with the rest of the transcript fitting
   * under it leaves the transcript at the bottom, following as before.
   */
  const place = useCallback(
    (key: string): boolean => {
      const el = ref.current;
      const box = el ? rowBox(el, key) : null;
      if (!el || !box) return false;
      const top = box.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
      const end = Math.max(0, el.scrollHeight - el.clientHeight);
      const target = Math.min(end, Math.max(0, Math.round(top - ANCHOR_MARGIN_PX)));
      if (Math.abs(el.scrollTop - target) >= 1) el.scrollTop = target;
      follow(end - target <= BOTTOM_THRESHOLD_PX);
      return true;
    },
    [follow],
  );

  const openingRef = useRef(opening);
  const lastKeyRef = useRef(lastKey);

  /** Places the opening while it is held; its first placement starts the settle window. */
  const applyOpening = useCallback(() => {
    if (settledRef.current) return;
    const current = openingRef.current;
    let placed = false;
    if (current.kind === 'tail') {
      scrollToBottom();
      placed = true;
    } else if (current.kind === 'row') {
      placed = place(current.key);
    }
    if (!placed || settleTimer.current !== null) return;
    countedLastKey.current = lastKeyRef.current;
    settleTimer.current = window.setTimeout(settle, SETTLE_MS);
  }, [place, scrollToBottom, settle]);

  // Only a prepended history page moves the anchor. Content appended below the
  // viewport must not drag the reader down, and neither must a redraw: the rows
  // around the reader changed because the reader asked for it.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const redrawn = redraw !== anchoredRedraw.current;
    const prepended = firstKey !== null && firstKey !== previousFirstKey.current;
    if (prepended && !redrawn && !followingRef.current) {
      const delta = el.scrollHeight - previousHeight.current;
      if (delta > 0) el.scrollTop += delta;
    }
    anchoredRedraw.current = redraw;
    previousFirstKey.current = firstKey;
    previousHeight.current = el.scrollHeight;
  }, [firstKey, revision, redraw]);

  // The opening, placed once it is decided and again with every change
  // to the rows while it is held. The caller keeps `opening` the same object
  // while it says the same thing.
  useLayoutEffect(() => {
    openingRef.current = opening;
    lastKeyRef.current = lastKey;
    applyOpening();
  }, [opening, lastKey, revision, redraw, applyOpening]);

  // Rows above the opening change height after they are drawn — the
  // Markdown renderer arrives, a part is measured — and the opening follows.
  useEffect(() => {
    const inner = ref.current?.firstElementChild;
    if (settled || !inner || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => applyOpening());
    observer.observe(inner);
    return () => observer.disconnect();
  }, [applyOpening, settled]);

  // The reader's own hand ends the opening. Scrolling it — a wheel, a
  // swipe, the keys, the scroll bar — also lets older history page in at the
  // top; a touch or a click on a row only stops the opening from moving.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const scrolled = () => {
      readerScrolled.current = true;
      settle();
    };
    const onKey = (event: KeyboardEvent) => {
      if (scrollsPage(event, el)) scrolled();
    };
    const onPointer = (event: PointerEvent) => {
      if (event.target === el) scrolled();
      else settle();
    };
    el.addEventListener('wheel', scrolled, { passive: true });
    el.addEventListener('touchmove', scrolled, { passive: true });
    el.addEventListener('touchstart', settle, { passive: true });
    el.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      el.removeEventListener('wheel', scrolled);
      el.removeEventListener('touchmove', scrolled);
      el.removeEventListener('touchstart', settle);
      el.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [settle]);

  useEffect(
    () => () => {
      if (settleTimer.current !== null) window.clearTimeout(settleTimer.current);
    },
    [],
  );

  useEffect(() => {
    if (followingRef.current) scrollToBottom();
  }, [revision, redraw, scrollToBottom]);

  // Count blocks, not streaming deltas: the badge should read "2 new" for two
  // messages, not once per 80 ms flush. A redraw starts the count again.
  useEffect(() => {
    if (redraw !== countedRedraw.current) {
      countedRedraw.current = redraw;
      countedLastKey.current = lastKey;
      setMissed(0);
      return;
    }
    if (lastKey === countedLastKey.current) return;
    countedLastKey.current = lastKey;
    if (lastKey === null) return;
    if (!followingRef.current) setMissed((n) => n + 1);
  }, [lastKey, redraw]);

  return { ref, following, missed, settled, jumpToLatest, onScroll };
}
