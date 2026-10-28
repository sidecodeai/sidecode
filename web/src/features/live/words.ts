/** The panel's sentences for what the call state holds: why a call did not start, why it ended, who is speaking. */
import type { LiveStatus } from './api';
import { blockOf, isBlock, type Block, type CallView, type EndReason } from './callState';
import { liveStrings as t } from './strings';

export function errorText(code: string): string {
  switch (code) {
    case 'mic':
      return t.errorMic;
    case 'micUnavailable':
      return t.errorMicUnavailable;
    // A refusal for want of a subscription reads as the assistant not being offered to the account.
    case 'notAllowed':
    case 'subscription':
      return t.errorNotAllowed;
    case 'quota':
      return t.paused;
    case 'billing':
      return t.unavailableNow;
    case 'busy':
      return t.errorBusy;
    case 'tooBusy':
      return t.errorTooBusy;
    case 'cooldown':
      return t.errorCooldown;
    case 'daily':
      return t.errorDaily;
    case 'service':
      return t.errorService;
    default:
      return t.errorGeneric;
  }
}

export function endedText(reason: EndReason): string {
  if (reason === 'subscription') return t.endedNotAllowed;
  if (reason === 'quota') return t.endedPaused;
  if (reason === 'billing') return t.endedUnavailable;
  if (reason === 'idle') return t.endedIdle;
  if (reason === 'limit') return t.endedLimit;
  if (reason === 'daily') return t.endedDaily;
  if (reason === 'lost') return t.endedLost;
  return t.ended;
}

/** Connecting, ending, the assistant speaking, hearing you, checking, then muted or listening. */
export function statusText({ phase, speaker, working, muted }: CallView): string {
  if (phase === 'connecting') return t.connecting;
  if (phase === 'ending') return t.ending;
  if (speaker === 'assistant') return t.speaking;
  if (speaker === 'user') return t.hearingYou;
  if (working) return t.checking;
  return muted ? t.muted : t.listening;
}

/**
 * The gateway's word about the account that the panel tells with no call on: its status first (a
 * reason this page does not know reads as `notAllowed`), then the last refusal, then the way the
 * last call ended.
 */
export function blockShown(view: CallView, status: LiveStatus): Block | 'notAllowed' | null {
  if (!status.available) return blockOf(status.reason) ?? 'notAllowed';
  if (isBlock(view.error)) return view.error;
  return isBlock(view.endedBy) ? view.endedBy : null;
}

/**
 * The line under the title with no call on: the `block` that `blockShown` found, else the last
 * refusal, else the way the last call ended, else what the assistant is for.
 */
export function noteText(view: CallView, block: Block | 'notAllowed' | null): string {
  if (block) return errorText(block);
  if (view.error) return errorText(view.error);
  if (view.endedBy) return endedText(view.endedBy);
  return t.intro;
}
