/**
 * A27 — the slash commands each session offers, keyed by `session_id`.
 *
 * The list is what the device says the agent offers *now*, so it is fetched
 * when a conversation opens and asked for again when `/` is typed, if the last
 * answer is older than a minute or was empty (PROTOCOL.md §6.3, "Slash
 * commands"). Anything else would either show a stale menu or make the panel
 * wait for a round trip at the one keystroke that has to feel instant.
 *
 * A session whose agent lacks capability `commands` never reaches this store:
 * the caller gates on the capability, so no request is ever sent for an agent
 * that would answer `unsupported`.
 */
import { create } from 'zustand';
import { rpc } from '../lib/gateway';
import type { Command } from '../protocol/types';

/** How long an answer stands before `/` asks the device again. */
export const COMMANDS_TTL_MS = 60_000;

export interface CommandsEntry {
  commands: Command[];
  /** When the answer arrived; 0 while nothing has been answered yet. */
  at: number;
  loading: boolean;
}

const EMPTY: CommandsEntry = { commands: [], at: 0, loading: false };

interface CommandsState {
  entries: Record<string, CommandsEntry>;
  /** Ask the device, unless a request for this session is already in flight. */
  fetch: (sessionId: string) => Promise<void>;
  /** The `/` keystroke: ask again only when the last answer is stale or empty. */
  refresh: (sessionId: string) => void;
  /** Fetch the first time a conversation opens; a fresh answer is kept. */
  open: (sessionId: string) => void;
  clear: (sessionId: string) => void;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

/** Stale, empty or never answered — the three cases that re-ask the device. */
function isStale(entry: CommandsEntry | undefined, now: number): boolean {
  if (!entry || entry.at === 0) return true;
  if (entry.commands.length === 0) return true;
  return now - entry.at >= COMMANDS_TTL_MS;
}

export const useCommands = create<CommandsState>((set, get) => ({
  entries: {},

  fetch: async (sessionId) => {
    const entry = get().entries[sessionId];
    if (entry?.loading) return;
    set((state) => ({
      entries: { ...state.entries, [sessionId]: { ...(entry ?? EMPTY), loading: true } },
    }));
    try {
      const result = await rpc('session.commands', { session_id: sessionId });
      set((state) => ({
        entries: {
          ...state.entries,
          [sessionId]: { commands: result.commands, at: Date.now(), loading: false },
        },
      }));
    } catch {
      // A refusal is not worth a banner: the panel simply does not appear, the
      // way it does not for an agent without the capability. The stamp keeps
      // the next keystroke from asking again immediately.
      set((state) => ({
        entries: {
          ...state.entries,
          [sessionId]: { commands: [], at: Date.now(), loading: false },
        },
      }));
    }
  },

  refresh: (sessionId) => {
    if (!isStale(get().entries[sessionId], Date.now())) return;
    void get().fetch(sessionId);
  },

  open: (sessionId) => {
    const entry = get().entries[sessionId];
    if (entry?.loading) return;
    if (entry && entry.at > 0 && Date.now() - entry.at < COMMANDS_TTL_MS) return;
    void get().fetch(sessionId);
  },

  clear: (sessionId) =>
    set((state) => {
      if (!state.entries[sessionId]) return {};
      const entries = { ...state.entries };
      delete entries[sessionId];
      return { entries };
    }),

  reset: () => set({ entries: {} }),
}));

export const commandsOf =
  (sessionId: string) =>
  (state: CommandsState): Command[] =>
    state.entries[sessionId]?.commands ?? EMPTY.commands;
