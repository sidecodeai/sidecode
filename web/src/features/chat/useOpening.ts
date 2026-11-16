/**
 * Where a conversation opens (`docs/DESIGN.md` § "Reading position"), read
 * from the rows this app holds and the reading position saved for it
 * (`anchor.ts`), and the older pages the anchor is looked for in.
 */
import { useEffect, useMemo, useState } from 'react';
import type { TimelineState } from '../../stores/timeline';
import { chooseAnchor, ownMessages } from './anchor';
import { readPosition } from './readingPosition';
import type { Opening } from './useScrollFollow';

/** The pages an open looks for its anchor in: the newest and up to two older ones. */
export const SEARCH_PAGES = 3;

const WAIT: Opening = { kind: 'wait' };
const TAIL: Opening = { kind: 'tail' };

interface Input {
  /** Who is signed in, whose positions are read; null reads none. */
  account: string | null;
  /** The conversation, as `<device>/<session>`. */
  sessionKey: string;
  timeline: TimelineState;
  /**
   * Whether the rows are this open's: its subscribe has answered and the
   * newest page was read. Nothing is placed before, so a copy from an earlier
   * visit that the open is about to read again never decides where it opens.
   */
  current: boolean;
  historyHasMore: boolean;
  /** How many pages of the rows held were asked for, the newest included. */
  pagesAsked: number;
}

/**
 * The opening, and whether it waits for an older page (rule 4: none of the
 * person's messages among the rows held, and a page left to look in). The
 * position is the one saved before this open: reading moves it on from here,
 * and the opening goes by where the person was when they came.
 */
export function useOpening({
  account,
  sessionKey,
  timeline,
  current,
  historyHasMore,
  pagesAsked,
}: Input): { opening: Opening; looking: boolean } {
  const [position] = useState(() => (account === null ? null : readPosition(account, sessionKey)));
  const choice = useMemo(
    () =>
      current
        ? chooseAnchor({
            messages: ownMessages(timeline),
            position,
            from: timeline.oldestSeq,
            canLookOlder: historyHasMore && pagesAsked < SEARCH_PAGES,
          })
        : null,
    [current, timeline, position, historyHasMore, pagesAsked],
  );
  const kind = choice === null || choice.kind === 'older' ? 'wait' : choice.kind;
  const key = choice?.kind === 'row' ? choice.key : null;
  const opening = useMemo<Opening>(() => {
    if (kind === 'row' && key !== null) return { kind: 'row', key };
    return kind === 'tail' ? TAIL : WAIT;
  }, [kind, key]);
  return { opening, looking: choice?.kind === 'older' };
}

/**
 * Reads the older page an opening is waiting for, one at a time, while the
 * opening is still held: once the reader has scrolled, they look for
 * themselves.
 */
export function useOlderSearch(looking: boolean, historyLoading: boolean, loadOlder: () => void): void {
  useEffect(() => {
    if (looking && !historyLoading) loadOlder();
  }, [looking, historyLoading, loadOlder]);
}
