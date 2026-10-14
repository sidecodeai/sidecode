/**
 * What has been typed and attached for a conversation, keyed by session key.
 *
 * `docs/DESIGN.md` § "The composer" — **A draft belongs to its session**: words
 * typed and files attached for one session stay with it when another is opened,
 * and are found again on return. The composer used to keep both in its own
 * component state, which the router reused across a session switch, so A's
 * words were sent to B. They live here instead: the composer reads the entry
 * its session names and writes back to the same one.
 *
 * In memory for the tab's life, and emptied on sign-out with every other store
 * (`signOut`). Nothing is persisted: a draft is not worth a round trip, and a
 * shared browser must not hand it to the next account.
 */
import { create } from 'zustand';
import { MAX_ATTACHMENTS, type AttachmentDraft } from '../features/chat/attachments';

/**
 * A43 — a queued message taken out of the line to be edited (`docs/DESIGN.md`
 * § "Up next"). The field holds its words; what the field held when the tap
 * came waits aside, and comes back the moment the edited words are back in the
 * line. It is part of the draft, so it survives a switch to another
 * conversation the way the words do.
 */
export interface QueuedEdit {
  /** The entry's `ts`, sent back as `queue_ts` so the words keep their place. */
  ts: number;
  /** The words as they were queued, which Cancel puts back. */
  original: string;
  /** What the field held when the edit began. */
  aside: { text: string; attachments: AttachmentDraft[] };
  /** The words are on their way back, so nothing is sent a second time meanwhile. */
  sending: boolean;
}

export interface Draft {
  text: string;
  attachments: AttachmentDraft[];
  /** A43: the queued message the field is editing, when it is editing one. */
  editing: QueuedEdit | null;
}

/** Stable identity, so a session nobody has typed into does not redraw. */
export const EMPTY_DRAFT: Draft = { text: '', attachments: [], editing: null };

interface DraftsState {
  drafts: Record<string, Draft>;
  setText: (key: string, text: string) => void;
  /**
   * Append files to a draft, never past the contract's cap. Two attach
   * operations can be in flight at once — a paste and a file dialog — and each
   * computed its budget from the count it read before it started, so the cap is
   * enforced here, where the list is written. Returns how many were dropped.
   */
  addAttachments: (key: string, files: readonly AttachmentDraft[]) => number;
  removeAttachment: (key: string, index: number) => void;
  /** Hand back the files a refused send took, unless newer ones are there. */
  restoreAttachments: (key: string, files: readonly AttachmentDraft[]) => void;
  /**
   * A43: the device has let go of a queued message, so the field takes its
   * words and sets aside what it held. One edit at a time: the list offers no
   * second one while a message is being edited.
   */
  beginEdit: (key: string, entry: { ts: number; text: string }) => void;
  /** A43: the edited words left for the device, or came back refused. */
  setEditSending: (key: string, sending: boolean) => void;
  /** A43: the words are back in the line, so the field takes back what it held. */
  endEdit: (key: string) => void;
  clear: (key: string) => void;
  reset: () => void;
}

function write(
  state: DraftsState,
  key: string,
  update: (draft: Draft) => Draft,
): Partial<DraftsState> {
  const previous = state.drafts[key] ?? EMPTY_DRAFT;
  const next = update(previous);
  if (next === previous) return {};
  if (next.text.length === 0 && next.attachments.length === 0 && next.editing === null) {
    if (!state.drafts[key]) return {};
    const drafts = { ...state.drafts };
    delete drafts[key];
    return { drafts };
  }
  return { drafts: { ...state.drafts, [key]: next } };
}

export const useDrafts = create<DraftsState>((set, get) => ({
  drafts: {},

  setText: (key, text) =>
    set((state) => write(state, key, (draft) => (draft.text === text ? draft : { ...draft, text }))),

  addAttachments: (key, files) => {
    const before = (get().drafts[key] ?? EMPTY_DRAFT).attachments.length;
    const dropped = Math.max(0, before + files.length - MAX_ATTACHMENTS);
    if (files.length === 0) return 0;
    set((state) =>
      write(state, key, (draft) => ({
        ...draft,
        attachments: [...draft.attachments, ...files].slice(0, MAX_ATTACHMENTS),
      })),
    );
    return dropped;
  },

  removeAttachment: (key, index) =>
    set((state) =>
      write(state, key, (draft) => ({
        ...draft,
        attachments: draft.attachments.filter((_, i) => i !== index),
      })),
    ),

  restoreAttachments: (key, files) =>
    set((state) =>
      write(state, key, (draft) =>
        draft.attachments.length === 0 ? { ...draft, attachments: [...files] } : draft,
      ),
    ),

  beginEdit: (key, entry) =>
    set((state) =>
      write(state, key, (draft) => ({
        text: entry.text,
        attachments: [],
        editing: {
          ts: entry.ts,
          original: entry.text,
          aside: { text: draft.text, attachments: draft.attachments },
          sending: false,
        },
      })),
    ),

  setEditSending: (key, sending) =>
    set((state) =>
      write(state, key, (draft) =>
        draft.editing && draft.editing.sending !== sending
          ? { ...draft, editing: { ...draft.editing, sending } }
          : draft,
      ),
    ),

  endEdit: (key) =>
    set((state) =>
      write(state, key, (draft) =>
        draft.editing
          ? {
              text: draft.editing.aside.text,
              attachments: draft.editing.aside.attachments,
              editing: null,
            }
          : draft,
      ),
    ),

  clear: (key) =>
    set((state) => {
      if (!state.drafts[key]) return {};
      const drafts = { ...state.drafts };
      delete drafts[key];
      return { drafts };
    }),

  reset: () => set({ drafts: {} }),
}));

export const draftOf =
  (key: string) =>
  (state: DraftsState): Draft =>
    state.drafts[key] ?? EMPTY_DRAFT;
