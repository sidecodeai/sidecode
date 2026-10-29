/**
 * Whether the files this tab was loaded from are gone. A deploy replaces the
 * build's hashed chunks, and a tab opened before it keeps its old entry file:
 * the first chunk it asks for afterwards is a 404, which Vite reports as
 * `vite:preloadError` and React as a rejected lazy import. Reloading is the
 * only cure, so this only says so — nothing reloads by itself, because the
 * person may have a message typed, and drafts live in memory
 * (`docs/DESIGN.md` § "A tab older than the server").
 */
import { create } from 'zustand';

interface StaleBuildState {
  stale: boolean;
  mark: () => void;
}

export const useStaleBuild = create<StaleBuildState>((set) => ({
  stale: false,
  mark: () => set({ stale: true }),
}));

/**
 * What each engine says when a dynamic import cannot be fetched: Chrome and
 * Edge "Failed to fetch dynamically imported module", Firefox "error loading
 * dynamically imported module", Safari "Importing a module script failed", and
 * Vite's own "Unable to preload CSS".
 */
const CHUNK_LOAD_FAILURE =
  /dynamically imported module|importing a module script failed|unable to preload/i;

export function isChunkLoadError(error: unknown): boolean {
  return error instanceof Error && CHUNK_LOAD_FAILURE.test(error.message);
}

/**
 * Vite fires `vite:preloadError` when a chunk or its stylesheet cannot be
 * fetched, and rethrows the error unless the handler prevents the default. The
 * error is left to travel on to the nearest `ChunkBoundary`, which keeps the
 * rest of the page standing, so this only records that the tab is out of date.
 * It does so for a chunk that could not be loaded, and not for one that loaded
 * and then threw while it ran: Vite reports both through this event, but the
 * second is a fault in the page, which a reload would not cure and the notice
 * would wrongly blame on an update.
 */
export function watchForStaleBuild(): () => void {
  const onPreloadError = (event: Event) => {
    const { payload } = event as Event & { payload?: unknown };
    if (isChunkLoadError(payload)) useStaleBuild.getState().mark();
  };
  window.addEventListener('vite:preloadError', onPreloadError);
  return () => window.removeEventListener('vite:preloadError', onPreloadError);
}
