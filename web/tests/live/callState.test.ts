import { describe, expect, it } from 'vitest';
import {
  announcement,
  blockEnd,
  blockOf,
  draftState,
  endReason,
  initialView,
  isBlock,
  reduce,
  refusalCode,
  type CallAction,
  type CallView,
} from '../../src/features/live/callState';
import type { ServerMessage } from '../../src/features/live/liveSocket';
import { READY } from './audioFakes';

function apply(view: CallView, ...actions: CallAction[]): CallView {
  return actions.reduce(reduce, view);
}

const message = (m: unknown): CallAction => ({ type: 'message', message: m as ServerMessage });

function live(): CallView {
  return apply(initialView, { type: 'connecting' }, message(READY));
}

describe('call state', () => {
  it('goes connecting, then live with the time left', () => {
    expect(apply(initialView, { type: 'connecting' }).phase).toBe('connecting');
    expect(live()).toMatchObject({ phase: 'live', secondsLeft: 1200 });
  });

  it('follows who is speaking and the time left as the gateway says', () => {
    const view = apply(
      live(),
      message({ type: 'state', speaker: 'user' }),
      message({ type: 'time', seconds_left: 1197 }),
    );
    expect(view).toMatchObject({ speaker: 'user', secondsLeft: 1197 });
  });

  it('keeps the whole conversation, joining deltas of one speaker into one turn', () => {
    const say = (speaker: 'user' | 'assistant', delta: string) =>
      message({ type: 'caption', speaker, delta });
    const view = apply(
      live(),
      say('user', 'What is '),
      say('user', 'running?'),
      say('assistant', 'Two sessions.'),
      say('user', 'Thanks'),
    );
    expect(view.transcript).toEqual([
      { kind: 'turn', speaker: 'user', text: 'What is running?' },
      { kind: 'turn', speaker: 'assistant', text: 'Two sessions.' },
      { kind: 'turn', speaker: 'user', text: 'Thanks' },
    ]);
  });

  it('does not cut a long turn short', () => {
    const view = apply(live(), message({ type: 'caption', speaker: 'user', delta: 'x'.repeat(500) }));
    expect(view.transcript[0]).toMatchObject({ text: 'x'.repeat(500) });
  });

  it('starts every call with an empty transcript and the microphone open', () => {
    const talked = apply(
      live(),
      message({ type: 'caption', speaker: 'user', delta: 'Hello' }),
      { type: 'muted', on: true },
      { type: 'closed', ended: 'idle' },
    );
    // The record of the call that ended stays for the panel to read until the next one starts.
    expect(talked.transcript).toHaveLength(1);
    expect(talked.muted).toBe(false);
    const next = apply(talked, { type: 'connecting' });
    expect(next).toMatchObject({ transcript: [], muted: false });
  });

  it('mutes and unmutes while the call is live, and forgets the mute when it ends', () => {
    const muted = apply(live(), { type: 'muted', on: true });
    expect(muted.muted).toBe(true);
    expect(apply(muted, { type: 'muted', on: false }).muted).toBe(false);
    expect(apply(muted, { type: 'ending' }, { type: 'closed', ended: 'hangup' }).muted).toBe(false);
  });

  it('adds what was sent to the transcript when the gateway says a Send press worked', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Skip the migration tests' };
    const view = apply(
      live(),
      message({ type: 'caption', speaker: 'assistant', delta: 'Ready to send.' }),
      message({ type: 'draft', draft }),
      { type: 'drafting', id: 'd1' },
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
      message({ type: 'draft', draft: null }),
    );
    expect(view.transcript).toEqual([
      { kind: 'turn', speaker: 'assistant', text: 'Ready to send.' },
      { kind: 'sent', session: 'Billing', text: 'Skip the migration tests' },
    ]);
    expect(view.recent).toEqual([{ draft, sent: true }]);
  });

  describe('a send that worked, however it was asked for and in whichever order the gateway says it', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Go ahead' };
    const result = message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' });
    const cleared = message({ type: 'draft', draft: null });
    const sentEntry = { kind: 'sent', session: 'Billing', text: 'Go ahead' };

    it.each([
      ['the result, then the draft is cleared', [result, cleared]],
      ['the draft is cleared, then the result', [cleared, result]],
    ])('a press of Send: %s', (_order, tail) => {
      const view = apply(live(), message({ type: 'draft', draft }), { type: 'drafting', id: 'd1' }, ...tail);
      expect(view.transcript).toEqual([sentEntry]);
      expect(view.outcome).toEqual({ id: 'd1', state: 'sent' });
    });

    it.each([
      ['the result, then the draft is cleared', [result, cleared]],
      ['the draft is cleared, then the result', [cleared, result]],
    ])('a spoken yes, no press: %s', (_order, tail) => {
      const view = apply(live(), message({ type: 'draft', draft }), ...tail);
      expect(view.transcript).toEqual([sentEntry]);
      // The card says it was sent, as it does after a press.
      expect(draftState(view)).toBe('sent');
    });

    it('a spoken yes while the draft is still on the card', () => {
      const view = apply(live(), message({ type: 'draft', draft }), result);
      expect(view.transcript).toEqual([sentEntry]);
      expect(view.draft).toEqual(draft);
    });
  });

  it('names the session as the gateway does, and falls back to the draft when it names none', () => {
    const draft = { id: 'd1', session: 'Billing (draft title)', text: 'Go' };
    const named = apply(
      live(),
      message({ type: 'draft', draft }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
    );
    expect(named.transcript[0]).toMatchObject({ session: 'Billing' });
    const unnamed = apply(
      live(),
      message({ type: 'draft', draft }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true }),
    );
    expect(unnamed.transcript[0]).toMatchObject({ session: 'Billing (draft title)' });
  });

  it('takes a null session, which is what the gateway sends when it names none, as none', () => {
    const draft = { id: 'd1', session: 'Billing (draft title)', text: 'Go' };
    const view = apply(
      live(),
      message({ type: 'draft', draft }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: null }),
    );
    expect(view.transcript).toEqual([{ kind: 'sent', session: 'Billing (draft title)', text: 'Go' }]);
  });

  it('adds nothing for a failed send, a result already counted, or a draft never shown', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Go' };
    const failed = apply(
      live(),
      message({ type: 'draft', draft }),
      { type: 'drafting', id: 'd1' },
      message({ type: 'draft.result', draft_id: 'd1', ok: false, error: 'send_failed' }),
    );
    expect(failed.transcript).toEqual([]);
    expect(failed.outcome).toEqual({ id: 'd1', state: 'failed' });
    // A failed press can be tried again, and the one that works is the one that is recorded.
    const retried = apply(
      failed,
      { type: 'drafting', id: 'd1' },
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
    );
    expect(retried.transcript).toHaveLength(1);

    const twice = apply(
      live(),
      message({ type: 'draft', draft }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
      message({ type: 'draft', draft: null }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
    );
    expect(twice.transcript).toHaveLength(1);

    const stranger = apply(
      live(),
      message({ type: 'draft.result', draft_id: 'never-shown', ok: true, session: 'Billing' }),
    );
    expect(stranger.transcript).toEqual([]);
  });

  it('holds the last five drafts by id, so a result can still say what was sent', () => {
    const drafts = Array.from({ length: 7 }, (_, i) => ({
      id: `d${i + 1}`,
      session: 'Billing',
      text: `message ${i + 1}`,
    }));
    const shown = apply(live(), ...drafts.map((draft) => message({ type: 'draft', draft })));
    expect(shown.recent.map((entry) => entry.draft.id)).toEqual(['d3', 'd4', 'd5', 'd6', 'd7']);

    const late = apply(
      shown,
      message({ type: 'draft', draft: null }),
      message({ type: 'draft.result', draft_id: 'd4', ok: true, session: 'Billing' }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
    );
    // d4 is held; d1 fell out long ago and has nothing to say what it was.
    expect(late.transcript).toEqual([{ kind: 'sent', session: 'Billing', text: 'message 4' }]);
  });

  it('keeps one entry for a draft shown again, with its latest words', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'First words' };
    const view = apply(
      live(),
      message({ type: 'draft', draft }),
      message({ type: 'draft', draft: { ...draft, text: 'Better words' } }),
      message({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' }),
    );
    expect(view.recent).toHaveLength(1);
    expect(view.transcript).toEqual([{ kind: 'sent', session: 'Billing', text: 'Better words' }]);
  });

  it('forgets the drafts when the call ends, and starts the next call with none', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Go' };
    const ended = apply(live(), message({ type: 'draft', draft }), { type: 'closed', ended: 'idle' });
    expect(ended.recent).toEqual([]);
    expect(apply(live(), message({ type: 'draft', draft }), { type: 'connecting' }).recent).toEqual([]);
  });

  it('follows whether the assistant is checking something, and forgets it when the call ends', () => {
    const working = apply(live(), message({ type: 'working', on: true }));
    expect(working.working).toBe(true);
    expect(apply(working, message({ type: 'working', on: false })).working).toBe(false);
    expect(apply(working, { type: 'closed', ended: 'idle' }).working).toBe(false);
    expect(apply(working, { type: 'ending' }, { type: 'closed', ended: 'hangup' }).working).toBe(false);
    expect(apply(working, { type: 'connecting' }).working).toBe(false);
    expect(apply(working, { type: 'failed', code: 'mic' }).working).toBe(false);
    expect(apply(initialView, { type: 'connecting' }, { type: 'cancelled' }).working).toBe(false);
  });

  it('shows the draft the gateway reports, and clears it when it says none', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Skip the tests' };
    const shown = apply(live(), message({ type: 'draft', draft }));
    expect(shown.draft).toEqual(draft);
    expect(apply(shown, message({ type: 'draft', draft: null })).draft).toBeNull();
  });

  it('tracks what became of the draft the person sent', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Go' };
    let view = apply(live(), message({ type: 'draft', draft }));
    expect(draftState(view)).toBe('ready');
    view = apply(view, { type: 'drafting', id: 'd1' });
    expect(draftState(view)).toBe('sending');
    view = apply(view, message({ type: 'draft.result', draft_id: 'd1', ok: false, error: 'x' }));
    expect(draftState(view)).toBe('failed');
    view = apply(view, { type: 'drafting', id: 'd1' }, message({ type: 'draft.result', draft_id: 'd1', ok: true }));
    view = apply(view, message({ type: 'draft', draft: null }));
    expect(draftState(view)).toBe('sent');
  });

  it('does not carry a result over to a different draft', () => {
    const next = { id: 'd2', session: 'Billing', text: 'Another' };
    let view = apply(live(), { type: 'drafting', id: 'd1' }, message({ type: 'draft.result', draft_id: 'd1', ok: false }));
    view = apply(view, message({ type: 'draft', draft: next }));
    expect(draftState(view)).toBe('ready');
  });

  it('a notice from the voice service is shown, then expires, and the call goes on', () => {
    const noticed = apply(live(), message({ type: 'notice', code: 'server_error' }));
    expect(noticed).toMatchObject({ phase: 'live', notice: { code: 'server_error' } });
    // Every notice is a new object, so one that repeats the last still starts its own timer.
    const again = apply(noticed, message({ type: 'notice', code: 'server_error' }));
    expect(again.notice).not.toBe(noticed.notice);
    expect(apply(noticed, { type: 'noticeExpired' }).notice).toBeNull();
    expect(apply(noticed, { type: 'closed', ended: 'idle' }).notice).toBeNull();
  });

  it('giving up while connecting leaves a quiet idle panel, not a refusal', () => {
    const view = apply(initialView, { type: 'connecting' }, { type: 'cancelled' });
    expect(view).toEqual(initialView);
  });

  it('flush and ended change nothing in the view', () => {
    const view = live();
    expect(apply(view, message({ type: 'flush' }))).toBe(view);
    expect(apply(view, message({ type: 'ended', reason: 'idle' }))).toBe(view);
  });

  it('ignores a message it does not know, as a page older than its gateway must', () => {
    const view = live();
    expect(apply(view, message({ type: 'from_the_future', value: 1 }))).toBe(view);
  });

  it('a call that has started ends with the reason the gateway gave', () => {
    const idle = apply(live(), { type: 'closed', ended: 'idle' });
    expect(idle).toMatchObject({ phase: 'idle', endedBy: 'idle', secondsLeft: null, draft: null });
    expect(apply(live(), { type: 'closed', ended: 'max_duration' }).endedBy).toBe('limit');
    expect(apply(live(), { type: 'closed', ended: 'expired' }).endedBy).toBe('limit');
    expect(apply(live(), { type: 'closed', ended: 'daily_limit' }).endedBy).toBe('daily');
    expect(apply(live(), { type: 'closed', ended: 'upstream_lost' }).endedBy).toBe('lost');
    expect(apply(live(), { type: 'closed', ended: null }).endedBy).toBe('lost');
    expect(apply(live(), { type: 'closed', ended: 'session_revoked' }).endedBy).toBe('other');
  });

  it('a hang-up ends quietly', () => {
    const view = apply(live(), { type: 'ending' }, { type: 'closed', ended: 'hangup' });
    expect(view).toMatchObject({ phase: 'idle', endedBy: null });
  });

  it('a close before the call began is a refusal, in the panel words', () => {
    const refused = (reason: string | null) =>
      apply(initialView, { type: 'connecting' }, { type: 'closed', ended: reason });
    expect(refused('call_active')).toMatchObject({ phase: 'error', error: 'busy' });
    expect(refused('cooldown').error).toBe('cooldown');
    expect(refused('too_busy').error).toBe('tooBusy');
    expect(refused('daily_limit').error).toBe('daily');
    expect(refused('not_allowed').error).toBe('notAllowed');
    expect(refused('upstream_refused').error).toBe('service');
    expect(refused(null).error).toBe('service');
  });

  it('words what the gateway says about the account itself the same way, before a call and at its end', () => {
    const refused = (reason: string) =>
      apply(initialView, { type: 'connecting' }, { type: 'closed', ended: reason });
    const ended = (reason: string) => apply(live(), { type: 'closed', ended: reason });
    expect(refused('subscription_required')).toMatchObject({ phase: 'error', error: 'subscription' });
    expect(refused('quota_exceeded')).toMatchObject({ phase: 'error', error: 'quota' });
    expect(refused('billing_not_configured')).toMatchObject({ phase: 'error', error: 'billing' });
    expect(ended('subscription_required')).toMatchObject({ phase: 'idle', endedBy: 'subscription' });
    expect(ended('quota_exceeded')).toMatchObject({ phase: 'idle', endedBy: 'quota' });
    expect(ended('billing_not_configured')).toMatchObject({ phase: 'idle', endedBy: 'billing' });
  });

  it('keeps what was said when a call ends for the account itself', () => {
    const said = apply(live(), message({ type: 'caption', speaker: 'user', delta: 'What is running?' }));
    const ended = apply(said, { type: 'closed', ended: 'quota_exceeded' });
    expect(ended.transcript).toEqual([{ kind: 'turn', speaker: 'user', text: 'What is running?' }]);
  });

  it('says which call a refusal or an end is of, and nothing before one is over', () => {
    expect(initialView.endedCall).toBeNull();
    expect(apply(live(), { type: 'closed', ended: 'idle', call: 3 }).endedCall).toBe(3);
    const refused = apply(initialView, { type: 'connecting' }, { type: 'closed', ended: 'cooldown', call: 4 });
    expect(refused.endedCall).toBe(4);
    expect(apply(live(), { type: 'closed', ended: 'idle' }).endedCall).toBeNull();
    // The next call starts without it.
    const ended = apply(live(), { type: 'closed', ended: 'quota_exceeded', call: 3 });
    expect(apply(ended, { type: 'connecting' }).endedCall).toBeNull();
  });

  it('keys what waits for the person: a draft by its id, an end the account made by which call it was', () => {
    const draft = { id: 'd1', session: 'Billing', text: 'Go' };
    const closedBy = (reason: string, call?: number, before: CallView = live()) =>
      apply(before, { type: 'closed', ended: reason, call });
    expect(announcement(initialView)).toBeNull();
    expect(announcement(apply(live(), message({ type: 'draft', draft })))).toBe('d1');
    expect(announcement(closedBy('quota_exceeded', 3))).toBe('quota:3');
    expect(announcement(closedBy('subscription_required', 4))).toBe('subscription:4');
    const refused = apply(initialView, { type: 'connecting' });
    expect(announcement(closedBy('billing_not_configured', 5, refused))).toBe('billing:5');
    // Not about the account, or not numbered: nothing waits.
    expect(announcement(closedBy('idle', 6))).toBeNull();
    expect(announcement(closedBy('cooldown', 7, refused))).toBeNull();
    expect(announcement(closedBy('quota_exceeded'))).toBeNull();
    // Only the account's own words are an end to ask the gateway about: a draft is not.
    expect(blockEnd(apply(live(), message({ type: 'draft', draft })))).toBeNull();
    expect(blockEnd(closedBy('quota_exceeded', 3))).toBe('quota:3');
  });

  it('knows which words are about the account itself', () => {
    expect(blockOf('subscription_required')).toBe('subscription');
    expect(blockOf('quota_exceeded')).toBe('quota');
    expect(blockOf('billing_not_configured')).toBe('billing');
    for (const other of ['not_allowed', 'daily_limit', 'constructor', '', null]) {
      expect(blockOf(other)).toBeNull();
    }
    expect(['subscription', 'quota', 'billing'].every(isBlock)).toBe(true);
    for (const other of ['notAllowed', 'daily', 'idle', '', null]) expect(isBlock(other)).toBe(false);
  });

  it('a new start forgets the last call', () => {
    const ended = apply(live(), { type: 'closed', ended: 'idle' });
    expect(apply(ended, { type: 'connecting' })).toMatchObject({ phase: 'connecting', endedBy: null });
    const failed = apply(initialView, { type: 'failed', code: 'mic' });
    expect(failed).toMatchObject({ phase: 'error', error: 'mic' });
    expect(apply(failed, { type: 'connecting' }).error).toBeNull();
  });

  it('names every reason a call can end for', () => {
    expect(refusalCode('mystery')).toBe('service');
    expect(endReason('hangup')).toBe('other');
    expect(endReason('protocol_error')).toBe('other');
    expect(endReason('gateway_stopping')).toBe('lost');
    expect(endReason('browser_silent')).toBe('lost');
    expect(endReason('content')).toBe('lost');
    expect(endReason('upstream_errors')).toBe('lost');
    expect(endReason('daily_limit')).toBe('daily');
  });
});
