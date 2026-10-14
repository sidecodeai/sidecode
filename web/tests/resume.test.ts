/**
 * A35 — the words a pending resume and the device's own steps are drawn with.
 * `docs/DESIGN.md` § "Paused by the usage limit": the time is the viewer's, the
 * date joins it when it is not today, "about" says the device estimated it, and
 * the try is appended once a resume has run into the limit again.
 */
import { afterEach, describe, expect, it } from 'vitest';
import {
  RESUME_MAX_AHEAD_MS,
  RESUME_MIN_AHEAD_MS,
  fromLocalInputValue,
  limitEndText,
  resumeBoundError,
  resumeNoticeText,
  resumeRowText,
  timeText,
  toLocalInputValue,
} from '../src/features/chat/resume';
import { useSettings } from '../src/stores/settings';
import { en } from '../src/strings';
import { zhHans } from '../src/strings.zh-Hans';
import type { ResumeEvent, SessionResume } from '../src/protocol/types';
import { historyFor, sessions } from '../mock/fixtures';

/** A local instant, so the assertions read in whatever zone the test runs in. */
const at = (h: number, m: number, dayOffset = 0): number =>
  new Date(2026, 8, 17 + dayOffset, h, m).getTime();

const NOW = at(10, 0);

const pending = (partial: Partial<SessionResume> = {}): SessionResume => ({
  at: at(15, 50),
  estimated: false,
  attempts: 0,
  window_minutes: 300,
  ...partial,
});

const row = (partial: Partial<ResumeEvent>): ResumeEvent =>
  ({ seq: 41, ts: NOW, kind: 'resume', status: 'scheduled', ...partial }) as ResumeEvent;

afterEach(() => {
  useSettings.setState({ language: 'en' });
});

describe('the time a resume reads at', () => {
  it('is a clock alone when it falls today', () => {
    expect(timeText(at(15, 50), NOW)).toBe('3:50 PM');
  });

  it('carries the date when it does not', () => {
    expect(timeText(at(15, 50, 1), NOW)).toBe('Sep 18, 3:50 PM');
  });

  it('follows the interface language, never the browser', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(timeText(at(15, 50), NOW)).toBe('15:50');
    // The day and the clock are joined the Chinese way, with no Latin comma.
    expect(timeText(at(15, 50, 1), NOW)).toBe('9月18日 15:50');
  });
});

describe('the notice above the transcript', () => {
  it('says what happened and when it resumes', () => {
    expect(resumeNoticeText(pending(), NOW)).toBe('Paused by the usage limit · resumes 3:50 PM');
  });

  it('says "about" when the device estimated the time', () => {
    expect(resumeNoticeText(pending({ estimated: true }), NOW)).toBe(
      'Paused by the usage limit · resumes about 3:50 PM',
    );
  });

  it('appends the try once a resume has run into the limit again', () => {
    expect(resumeNoticeText(pending({ attempts: 1 }), NOW)).toContain(en.chat.resumeSecondTry);
    expect(resumeNoticeText(pending({ attempts: 2 }), NOW)).toContain(en.chat.resumeThirdTry);
  });

  it('is said in the interface language', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(resumeNoticeText(pending(), NOW)).toContain(zhHans.chat.pausedByLimit);
  });
});

describe('the rows the device writes', () => {
  it('names the time a resume was scheduled or moved to', () => {
    expect(resumeRowText(row({ status: 'scheduled', at: at(15, 50) }), NOW)).toBe(
      'Resume scheduled for 3:50 PM',
    );
    expect(resumeRowText(row({ status: 'rescheduled', at: at(16, 20) }), NOW)).toBe(
      'Resume moved to 4:20 PM',
    );
  });

  it('draws nothing at all for the moment of resuming', () => {
    expect(resumeRowText(row({ status: 'fired' }), NOW)).toBeNull();
  });

  it("puts the device's own one line of why after what happened", () => {
    expect(resumeRowText(row({ status: 'cancelled', reason: 'you sent a message' }), NOW)).toBe(
      'Resume cancelled · you sent a message',
    );
    expect(
      resumeRowText(row({ status: 'dropped', reason: 'the terminal was closed' }), NOW),
    ).toBe('Not resumed · the terminal was closed');
  });

  it('says what happened without a reason the device did not give', () => {
    expect(resumeRowText(row({ status: 'cancelled' }), NOW)).toBe(en.chat.resumeCancelled);
    expect(resumeRowText(row({ status: 'scheduled' }), NOW)).toBe(en.chat.resumeScheduled);
  });
});

describe('the turn the limit ended', () => {
  it('says when the limit resets', () => {
    expect(limitEndText({ window_minutes: 300, resets_at: at(15, 50) }, NOW)).toBe(
      'Ended at the usage limit · resets 3:50 PM',
    );
  });

  it('says only that it ended when the vendor named no time', () => {
    expect(limitEndText({ resets_at: null }, NOW)).toBe(en.chat.turnLimit);
  });
});

describe('the bounds the picker refuses outside of', () => {
  it('takes a time between a minute and eight days from now', () => {
    expect(resumeBoundError(NOW + RESUME_MIN_AHEAD_MS, NOW)).toBeNull();
    expect(resumeBoundError(NOW + RESUME_MAX_AHEAD_MS, NOW)).toBeNull();
  });

  it('refuses one too soon and one too far out', () => {
    expect(resumeBoundError(NOW + 30_000, NOW)).toBe(en.chat.resumeTooSoon);
    expect(resumeBoundError(NOW + RESUME_MAX_AHEAD_MS + 60_000, NOW)).toBe(en.chat.resumeTooFar);
  });
});

describe('the datetime-local field', () => {
  it('round-trips an instant through the zone the viewer is in', () => {
    const value = toLocalInputValue(at(15, 50));
    expect(value).toBe('2026-09-17T15:50');
    expect(fromLocalInputValue(value)).toBe(at(15, 50));
  });

  it('reads nothing from an empty field', () => {
    expect(fromLocalInputValue('')).toBeNull();
  });
});

/**
 * The scripted scenario the mock gateway serves, so the whole path can be seen
 * in `npm run dev:mock` without a device: a session Claude Code stopped at the
 * five-hour window, the resume the device scheduled, and the vendor's sentence
 * where it belongs.
 */
describe("the mock gateway's paused session", () => {
  it('carries a pending resume on its summary', () => {
    const paused = sessions.find((s) => s.session_id === 'ses-limit');
    expect(paused?.resume?.estimated).toBe(false);
    expect(paused?.resume?.window_minutes).toBe(300);
    // The dot is not changed by a pending resume: the session is idle.
    expect(paused?.state).toBe('idle');
  });

  it('ends its turn at the limit and schedules the resume a minute after', () => {
    const history = historyFor('ses-limit');
    const ended = history.find((event) => event.kind === 'turn_completed');
    const scheduled = history.find((event) => event.kind === 'resume');
    if (ended?.kind !== 'turn_completed' || scheduled?.kind !== 'resume') {
      throw new Error('the scenario lost its limit stop');
    }

    expect(ended.stop_reason).toBe('error');
    expect(ended.limit?.window_minutes).toBe(300);
    expect(scheduled.status).toBe('scheduled');
    expect(scheduled.at).toBe((ended.limit?.resets_at ?? 0) + 60_000);
  });

  it("sends the vendor's sentence as an error, never as the agent's words", () => {
    const history = historyFor('ses-limit');
    expect(history.some((event) => event.kind === 'error')).toBe(true);
    for (const event of history) {
      if (event.kind === 'assistant_text') {
        expect(event.text ?? '').not.toContain('session limit');
      }
    }
  });
});
