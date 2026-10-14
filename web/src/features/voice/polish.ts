/**
 * A29 — the pure half of dictation polish: what the model is told, and how its
 * answer meets the draft.
 *
 * The composer owns the request; everything here is a value in and a value out,
 * so the rules the contract fixes — twenty messages, oldest first, four thousand
 * characters each, and a replacement that touches the dictated words alone —
 * are testable without a field, a socket or a gateway.
 */
import type { PolishContextItem, PolishRequest } from '../../protocol/types';
import type { TimelineState } from '../../stores/timeline';
import { mergeDraft } from './draft';

/** `PolishRequest.context` takes at most this many messages (schema: maxItems). */
export const CONTEXT_LIMIT = 20;
/** Each context message is trimmed by the app to this length (schema: maxLength). */
export const CONTEXT_TEXT_LIMIT = 4000;
/** `PolishRequest.text` (schema: maxLength). A longer dictation is not polished. */
export const TEXT_LIMIT = 8192;

/** What a dictation left in the field: the draft it started from and its words. */
export interface PolishSpan {
  /** The draft the mic button was pressed on. Never touched by the answer. */
  base: string;
  /** The words the recogniser produced, which are in the field now. */
  dictated: string;
}

/** The draft as the recogniser left it. */
export const dictatedDraft = (span: PolishSpan): string => mergeDraft(span.base, span.dictated);

/** The same draft with the dictated span replaced by what the model returned. */
export const polishedDraft = (span: PolishSpan, polished: string): string =>
  mergeDraft(span.base, polished);

/**
 * Whether these words can be sent at all: an empty dictation has nothing to
 * polish, and one past the contract's limit would only earn a `bad_request`.
 */
export const canPolish = (text: string): boolean =>
  text.trim().length > 0 && text.length <= TEXT_LIMIT;

/**
 * The conversation the model is given: the messages the app already shows, user
 * and assistant alike, oldest first, the last twenty of them, each trimmed.
 * Unconfirmed sends (A12) count — they are on screen, and the newest of them is
 * usually what the dictation is answering.
 */
export function polishContext(timeline: TimelineState): PolishContextItem[] {
  const items: PolishContextItem[] = [];
  for (const key of timeline.order) {
    const item = timeline.items[key];
    if (!item) continue;
    const event = item.event;
    if (event.kind === 'user_message') items.push({ role: 'user', text: event.text });
    else if (event.kind === 'assistant_text') items.push({ role: 'assistant', text: event.text ?? '' });
  }
  for (const block of timeline.optimistic) items.push({ role: 'user', text: block.text });
  return items
    .map((item) => ({ role: item.role, text: item.text.trim().slice(0, CONTEXT_TEXT_LIMIT) }))
    .filter((item) => item.text.length > 0)
    .slice(-CONTEXT_LIMIT);
}

/**
 * The body of `POST /api/polish` for one dictation. A44: the web's words were
 * always transcribed by the gateway, whose provider detected their language,
 * so the model's hint is `auto`.
 */
export function polishRequest(
  span: PolishSpan,
  options: { model: string; strength: PolishRequest['strength'] },
  context: PolishContextItem[],
): PolishRequest {
  return {
    text: span.dictated,
    model: options.model,
    strength: options.strength,
    language: 'auto',
    context,
  };
}

/**
 * The draft the answer produces, or null when the field has moved on: the
 * person typed, sent, or started dictating again, and their words win. The
 * polished text is trimmed of the whitespace a model likes to add; an answer
 * that is empty is no answer, and reads as a failure.
 */
export function applyPolished(current: string, span: PolishSpan, polished: string): string | null {
  const text = polished.trim();
  if (text.length === 0) return null;
  if (current !== dictatedDraft(span)) return null;
  const next = polishedDraft(span, text);
  return next === current ? null : next;
}

/**
 * Undo: the words as they were dictated, or null when the field no longer holds
 * what the model wrote, in which case there is nothing to put back.
 */
export function undoPolished(current: string, span: PolishSpan, polished: string): string | null {
  if (current !== polishedDraft(span, polished)) return null;
  return dictatedDraft(span);
}
