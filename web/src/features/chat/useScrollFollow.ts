import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

const BOTTOM_THRESHOLD_PX = 56;
const TOP_TRIGGER_PX = 120;

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
}

/**
 * Keeps the timeline pinned to the bottom while the user has not scrolled away,
 * counts new blocks missed otherwise, and preserves the scroll anchor when an
 * older history page is prepended.
 */
export function useScrollFollow({ revision, redraw, firstKey, lastKey, onReachTop }: Options) {
  const ref = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const [missed, setMissed] = useState(0);
  const previousHeight = useRef(0);
  const previousFirstKey = useRef(firstKey);
  // The two effects below each need the redraw they last acted on, and the
  // anchor effect runs first, so they cannot share one.
  const anchoredRedraw = useRef(redraw);
  const countedRedraw = useRef(redraw);
  // Written only from event handlers and effects, never during render.
  const followingRef = useRef(true);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const el = ref.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    followingRef.current = true;
    setFollowing(true);
    setMissed(0);
  }, []);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance <= BOTTOM_THRESHOLD_PX;
    followingRef.current = atBottom;
    setFollowing(atBottom);
    if (atBottom) setMissed(0);
    if (el.scrollTop <= TOP_TRIGGER_PX) onReachTop();
  }, [onReachTop]);

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

  useEffect(() => {
    if (followingRef.current) scrollToBottom();
  }, [revision, redraw, scrollToBottom]);

  // Count blocks, not streaming deltas: the badge should read "2 new" for two
  // messages, not once per 80 ms flush. A redraw starts the count again.
  useEffect(() => {
    if (redraw !== countedRedraw.current) {
      countedRedraw.current = redraw;
      setMissed(0);
      return;
    }
    if (lastKey === null) return;
    if (!followingRef.current) setMissed((n) => n + 1);
  }, [lastKey, redraw]);

  return { ref, following, missed, scrollToBottom, onScroll };
}
