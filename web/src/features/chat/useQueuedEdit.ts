import { useCallback, useEffect, useRef, type RefObject } from 'react';
import { errorText, queueRemoveText } from '../../lib/errors';
import { strings } from '../../strings';
import { useDrafts, type QueuedEdit } from '../../stores/drafts';
import type { SendMode } from '../../protocol/frames';
import type { QueuedMessage } from '../../protocol/types';
import type { AttachmentDraft } from './attachments';

export type Send = (
  text: string,
  attachments: AttachmentDraft[],
  mode: SendMode,
  queueTs?: number,
) => Promise<void>;

interface Options {
  /** The session key the draft, and therefore the edit, belongs to. */
  draftKey: string;
  editing: QueuedEdit | null;
  field: RefObject<HTMLTextAreaElement | null>;
  /** Take the message out of the line; rejects with the device's refusal. */
  onTake: ((queuedId: string) => Promise<void>) | undefined;
  onSend: Send;
  /** The composer's line under the field: what went wrong, or nothing. */
  onErrors: (messages: string[]) => void;
  /** What else lets go of the field when it takes a message: a dictation, a polish note. */
  onBegin: () => void;
}

/**
 * A43 — a queued message taken back into the field and put back again
 * (`docs/DESIGN.md` § "Up next"). The edit itself is the session's and lives in
 * the drafts store; this hook runs the two round trips around it.
 */
export function useQueuedEdit({
  draftKey,
  editing,
  field,
  onTake,
  onSend,
  onErrors,
  onBegin,
}: Options) {
  const taking = useRef(false);
  const caretToEnd = useRef(false);

  // The words arrive with the caret after them and the field in hand.
  useEffect(() => {
    if (!caretToEnd.current || !editing) return;
    caretToEnd.current = false;
    const el = field.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [editing, field]);

  /**
   * The message leaves the line before the field takes it, so the device
   * cannot deliver words that are still changing. `not_found` means the device
   * took it first: nothing opens, and one line says so.
   */
  const begin = useCallback(
    async (item: QueuedMessage) => {
      if (!onTake || taking.current || useDrafts.getState().drafts[draftKey]?.editing) return;
      taking.current = true;
      try {
        await onTake(item.id);
      } catch (err) {
        onErrors([queueRemoveText(err)]);
        return;
      } finally {
        taking.current = false;
      }
      onBegin();
      onErrors([]);
      caretToEnd.current = true;
      useDrafts.getState().beginEdit(draftKey, { ts: item.ts, text: item.text });
    },
    [draftKey, onTake, onErrors, onBegin],
  );

  /**
   * The words go back as `mode: "queue"` under the entry's `ts`, which puts
   * them where they were even behind a steering agent — or, from the ⋯ menu,
   * as an interrupt, which has no place in the line to keep. The field holds
   * them until the gateway answers: accepted or uncertain, the edit ends and
   * the draft that waited aside comes back; refused, the words stay where they
   * are and the edit goes on.
   */
  const putBack = useCallback(
    (text: string, files: AttachmentDraft[], mode: 'queue' | 'interrupt') => {
      const drafts = useDrafts.getState();
      const edit = drafts.drafts[draftKey]?.editing;
      if (!edit || edit.sending) return;
      drafts.setEditSending(draftKey, true);
      onErrors([]);
      const sent =
        mode === 'queue' ? onSend(text, files, mode, edit.ts) : onSend(text, files, mode);
      sent
        .then(() => useDrafts.getState().endEdit(draftKey))
        .catch((err: unknown) => {
          useDrafts.getState().setEditSending(draftKey, false);
          onErrors([errorText(err, strings.composer.sendFailed)]);
        });
    },
    [draftKey, onSend, onErrors],
  );

  /** Cancel is a put-back of the words as they were queued, and nothing else. */
  const cancel = useCallback(() => {
    const edit = useDrafts.getState().drafts[draftKey]?.editing;
    if (edit) putBack(edit.original, [], 'queue');
  }, [draftKey, putBack]);

  return { begin, putBack, cancel };
}
