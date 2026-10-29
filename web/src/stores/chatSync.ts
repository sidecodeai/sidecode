/**
 * What the chat store needs to keep a transcript whole: what a subscribe leaves
 * to be fetched from the device, how a page of what the device holds is folded
 * in, and when the rows start again (`docs/DESIGN.md` § "Keeping a transcript
 * whole").
 */
import type { SessionEvent } from '../protocol/types';
import type { ChatSession } from './chat';
import { foldPage } from './chatFold';
import { hasGap, keepOptimistic, mergeMissed, type TimelineState } from './timeline';

/**
 * How many pages one repair walks forward before giving up on joining. A device
 * that holds more than this after the cursor ran for a long while with nobody
 * watching, and the newest page serves the reader better than a transcript
 * rebuilt row by row.
 */
export const REPAIR_PAGES = 5;

/**
 * A page of what the device holds for a stretch the transcript missed, folded
 * in. The todos snapshot it may carry is taken if it is newer than the one
 * held; the usage it may carry only while no live event has arrived since the
 * page was asked for, because that figure has no version.
 */
export function absorbMissed(chat: ChatSession, events: readonly SessionEvent[], quiet: boolean): ChatSession {
  const folded = foldPage(chat, events, quiet);
  return adopt(folded, mergeMissed(folded.timeline, events));
}

/**
 * The conversation with a timeline that was just merged into. The cap drops the
 * oldest rows once the timeline is past it, and that is older history again,
 * even where the last page said there was none.
 */
export function adopt(chat: ChatSession, timeline: TimelineState): ChatSession {
  return {
    ...chat,
    timeline,
    historyHasMore: chat.historyHasMore || timeline.dropped > chat.timeline.dropped,
  };
}

/**
 * The rows start again from the newest page. Unconfirmed sends stay, and so do
 * the todos, the queue and the usage: they are not rows, and the page does not
 * carry them all. A page of older history that is on its way was asked for the
 * rows that are gone, so the generation moves on and its answer is dropped, and
 * the read it was is no longer under way.
 */
export function restart(chat: ChatSession): ChatSession {
  return {
    ...chat,
    timeline: keepOptimistic(chat.timeline),
    historyHasMore: true,
    historyLoaded: false,
    historyLoading: false,
    generation: chat.generation + 1,
    error: null,
  };
}

/**
 * What a subscribe reply still owes from the device: the newest page when none
 * has been read (a `resync` of a conversation with no rows puts it back to
 * that), or the repair of a stretch the gateway could not serve (a `resync`) or
 * a hole that was open before the reconnect — the cursor this subscribe sent is
 * past it, so the gateway cannot know. A conversation that was read and has no
 * rows owes nothing by being empty: an empty read has no newest page to ask for
 * again, and a failure that nothing asks about could never be cleared.
 */
export function owedAfterSubscribe(chat: ChatSession, resync: boolean): 'newest' | 'repair' | null {
  if (!chat.historyLoaded) return 'newest';
  return resync || hasGap(chat.timeline) ? 'repair' : null;
}
