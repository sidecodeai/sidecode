/** The panel's state as a pure reducer: what each message from the gateway does to the view. */
import type { LiveDraft } from './api';
import type { ServerMessage, Speaker } from './liveSocket';
import { appendDelta, appendSent, type Entry } from './transcript';

export type Phase = 'idle' | 'connecting' | 'live' | 'ending' | 'error';
/**
 * What the gateway says about the account itself, rather than about one call: in its status, as a
 * refusal before a call, or as the end of one. The panel tells each the same way in all three.
 */
export type Block = 'subscription' | 'quota' | 'billing';
/** Why a call that had started is over, for the panel to word. */
export type EndReason = 'idle' | 'limit' | 'daily' | 'lost' | Block | 'other';
export type DraftState = 'ready' | 'sending' | 'sent' | 'failed';

/** A draft the panel has shown, kept after it is gone so a result that follows it can say what was sent. */
export interface RecentDraft {
  draft: LiveDraft;
  /** The transcript already has this draft as a sent message. */
  sent: boolean;
}

/** How many are kept: a call sends a few, and a result comes close behind its draft. */
const RECENT_DRAFTS = 5;

export interface CallView {
  phase: Phase;
  /** What kept a call from starting: a code the panel words. Cleared by the next start. */
  error: string | null;
  endedBy: EndReason | null;
  /** Which call the refusal or the end above is of, as the page numbers its calls; null before one is over. */
  endedCall: number | null;
  speaker: Speaker;
  /** What was said and what was sent since the call went live, in order. */
  transcript: Entry[];
  /** The microphone is replaced by silence: the call goes on and nothing of the person is heard. */
  muted: boolean;
  /** The assistant is checking something; off when a call ends. */
  working: boolean;
  secondsLeft: number | null;
  /** The Live API reported an error but the call goes on; each one is a new object. */
  notice: { code: string } | null;
  draft: LiveDraft | null;
  /** The last few drafts shown, oldest first, by id. */
  recent: RecentDraft[];
  /** What became of the draft a result last spoke of, by its id. */
  outcome: { id: string; state: Exclude<DraftState, 'ready'> } | null;
}

export type CallAction =
  | { type: 'connecting' }
  | { type: 'message'; message: ServerMessage }
  | { type: 'drafting'; id: string }
  | { type: 'muted'; on: boolean }
  | { type: 'ending' }
  | { type: 'cancelled' }
  | { type: 'noticeExpired' }
  /** `call` is the call that closed, which tells one ended call from the next. */
  | { type: 'closed'; ended: string | null; call?: number }
  | { type: 'failed'; code: string };

export const initialView: CallView = {
  phase: 'idle',
  error: null,
  endedBy: null,
  endedCall: null,
  speaker: 'none',
  transcript: [],
  muted: false,
  working: false,
  secondsLeft: null,
  notice: null,
  draft: null,
  recent: [],
  outcome: null,
};

/** What a refusal before the call started is called in the panel's words. */
const REFUSALS: Record<string, string> = {
  not_allowed: 'notAllowed',
  call_active: 'busy',
  cooldown: 'cooldown',
  too_busy: 'tooBusy',
  daily_limit: 'daily',
  upstream_unavailable: 'service',
  upstream_refused: 'service',
  upstream_malformed: 'service',
  internal_error: 'generic',
};

/** The reasons the gateway gives about the account itself, in the panel's word for each. */
export function blockOf(reason: string | null): Block | null {
  switch (reason) {
    case 'subscription_required':
      return 'subscription';
    case 'quota_exceeded':
      return 'quota';
    case 'billing_not_configured':
      return 'billing';
    default:
      return null;
  }
}

/** Whether a refusal's code or a call's end is one of those words. */
export const isBlock = (value: string | null): value is Block =>
  value === 'subscription' || value === 'quota' || value === 'billing';

export function refusalCode(reason: string | null): string {
  const block = blockOf(reason);
  if (block) return block;
  return (reason && REFUSALS[reason]) || 'service';
}

/** How a call that ended after it had started is described. */
export function endReason(reason: string | null): EndReason {
  const block = blockOf(reason);
  if (block) return block;
  if (reason === 'idle') return 'idle';
  if (reason === 'max_duration' || reason === 'expired') return 'limit';
  if (reason === 'daily_limit') return 'daily';
  if (reason === null || reason.startsWith('upstream_') || reason === 'gateway_stopping') {
    return 'lost';
  }
  if (reason === 'browser_silent' || reason === 'content') return 'lost';
  return 'other';
}

/**
 * The word about the account itself that a call's refusal or end gave, with which call it was: a
 * key that differs from one such end to the next, so each is told once.
 */
export function blockEnd(view: CallView): string | null {
  const block = isBlock(view.error) ? view.error : isBlock(view.endedBy) ? view.endedBy : null;
  return block !== null && view.endedCall !== null ? `${block}:${view.endedCall}` : null;
}

/**
 * What waits for the person, by a key that differs from one such thing to the next, so the panel
 * is brought up once for each: a draft needing a yes, or a call the account itself ended.
 */
export function announcement(view: CallView): string | null {
  return view.draft?.id ?? blockEnd(view);
}

/** The draft just shown, among the last few: a draft shown again (the same id) is not a second one. */
function remember(recent: RecentDraft[], draft: LiveDraft): RecentDraft[] {
  const known = recent.find((entry) => entry.draft.id === draft.id);
  if (known) return recent.map((entry) => (entry === known ? { ...entry, draft } : entry));
  return [...recent, { draft, sent: false }].slice(-RECENT_DRAFTS);
}

/**
 * A result for a send that worked, whoever asked for it (a press of Send, or a spoken yes): the
 * draft it is for, by id, is put in the transcript as sent, once, named as the gateway names the
 * session. A result for a draft this page never showed has nothing to say what was sent.
 */
function sent(view: CallView, result: Extract<ServerMessage, { type: 'draft.result' }>): CallView {
  const outcome = { id: result.draft_id, state: 'sent' } as const;
  const held = view.recent.find((entry) => entry.draft.id === result.draft_id);
  if (!held || held.sent) return { ...view, outcome };
  return {
    ...view,
    outcome,
    recent: view.recent.map((entry) => (entry === held ? { ...entry, sent: true } : entry)),
    transcript: appendSent(view.transcript, result.session || held.draft.session, held.draft.text),
  };
}

function message(view: CallView, msg: ServerMessage): CallView {
  switch (msg.type) {
    case 'ready':
      return { ...view, phase: 'live', secondsLeft: msg.seconds_left };
    case 'state':
      return { ...view, speaker: msg.speaker };
    case 'working':
      return { ...view, working: msg.on };
    case 'caption':
      return { ...view, transcript: appendDelta(view.transcript, msg.speaker, msg.delta) };
    case 'draft':
      return {
        ...view,
        draft: msg.draft,
        recent: msg.draft ? remember(view.recent, msg.draft) : view.recent,
      };
    case 'draft.result':
      return msg.ok ? sent(view, msg) : { ...view, outcome: { id: msg.draft_id, state: 'failed' } };
    case 'time':
      return { ...view, secondsLeft: msg.seconds_left };
    case 'notice':
      return { ...view, notice: { code: msg.code } };
    case 'flush':
    case 'ended':
      return view;
    default:
      // A message from a gateway newer than this page: nothing here draws it.
      return view;
  }
}

export function reduce(view: CallView, action: CallAction): CallView {
  switch (action.type) {
    case 'connecting':
      return { ...initialView, phase: 'connecting' };
    case 'message':
      return message(view, action.message);
    case 'drafting':
      return { ...view, outcome: { id: action.id, state: 'sending' } };
    case 'muted':
      return { ...view, muted: action.on };
    case 'ending':
      return { ...view, phase: 'ending' };
    case 'cancelled':
      return initialView;
    case 'noticeExpired':
      return { ...view, notice: null };
    case 'closed':
      // A refusal comes before `ready`; after it, a call has simply ended.
      if (view.phase === 'connecting') {
        return {
          ...initialView,
          phase: 'error',
          error: refusalCode(action.ended),
          endedCall: action.call ?? null,
        };
      }
      return {
        ...view,
        phase: 'idle',
        speaker: 'none',
        muted: false,
        working: false,
        secondsLeft: null,
        notice: null,
        draft: null,
        recent: [],
        endedBy: view.phase === 'ending' ? null : endReason(action.ended),
        endedCall: action.call ?? null,
      };
    case 'failed':
      return { ...initialView, phase: 'error', error: action.code };
  }
}

/** The state of the draft card for the draft now shown (or the one just sent, once it is gone). */
export function draftState(view: CallView): DraftState {
  const { draft, outcome } = view;
  if (draft === null) return outcome?.state === 'sent' ? 'sent' : 'ready';
  return outcome?.id === draft.id ? outcome.state : 'ready';
}
