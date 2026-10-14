/**
 * A35 — the words a pending resume is drawn with, the words the device's own
 * steps read as, and the bounds the time picker refuses outside of.
 * `docs/DESIGN.md` § "Paused by the usage limit"; PROTOCOL.md §6.3 and §7.2.
 *
 * Everything here is pure: a timestamp and the clock go in, the reader's own
 * words come out. The time is always the viewer's, because the gateway has no
 * idea what zone the browser is in.
 */
import { strings } from '../../strings';
import type { LimitStop, ResumeEvent, SessionResume } from '../../protocol/types';

/** §6.3: the soonest and the latest a resume may be set for. */
export const RESUME_MIN_AHEAD_MS = 60_000;
export const RESUME_MAX_AHEAD_MS = 8 * 24 * 60 * 60_000;

/**
 * A time in the viewer's own zone: "3:50 PM" when it falls today, "Sep 18,
 * 3:50 PM" on any other day. The clock face is the reader's language's, so 中文
 * reads 15:50 where English reads 3:50 PM.
 */
export function timeText(at: number, now: number = Date.now()): string {
  const when = new Date(at);
  const locale = strings.format.dateLocale;
  const time = when.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' });
  const today = new Date(now);
  const sameDay =
    when.getFullYear() === today.getFullYear() &&
    when.getMonth() === today.getMonth() &&
    when.getDate() === today.getDate();
  if (sameDay) return time;
  const day = when.toLocaleDateString(locale, { month: 'short', day: 'numeric' });
  return strings.format.dateTime(day, time);
}

/** The try a resume is on, once one has already run into the limit again. */
function attemptWord(attempts: number): string | null {
  if (attempts <= 0) return null;
  return attempts === 1 ? strings.chat.resumeSecondTry : strings.chat.resumeThirdTry;
}

/** "Paused by the usage limit · resumes 3:50 PM · second try". */
export function resumeNoticeText(resume: SessionResume, now: number = Date.now()): string {
  const when = timeText(resume.at, now);
  const parts = [
    strings.chat.pausedByLimit,
    resume.estimated ? strings.chat.resumesAbout(when) : strings.chat.resumesAt(when),
  ];
  const attempt = attemptWord(resume.attempts);
  if (attempt !== null) parts.push(attempt);
  return parts.join(' · ');
}

/**
 * How a turn the usage limit ended closes, with the reset time when the vendor
 * named one and without it when `resets_at` is null.
 */
export function limitEndText(limit: LimitStop, now: number = Date.now()): string {
  if (limit.resets_at === null || limit.resets_at === undefined) return strings.chat.turnLimit;
  return strings.chat.turnLimitResets(timeText(limit.resets_at, now));
}

/**
 * One of the device's steps as a timeline row, or null for `fired`: the moment
 * of resuming is the prompt in the person's bubble, not a row of its own.
 */
export function resumeRowText(event: ResumeEvent, now: number = Date.now()): string | null {
  switch (event.status) {
    case 'fired':
      return null;
    case 'scheduled':
      return event.at === undefined
        ? strings.chat.resumeScheduled
        : strings.chat.resumeScheduledAt(timeText(event.at, now));
    case 'rescheduled':
      return event.at === undefined
        ? strings.chat.resumeMoved
        : strings.chat.resumeMovedTo(timeText(event.at, now));
    case 'cancelled':
      return withReason(strings.chat.resumeCancelled, event.reason);
    case 'dropped':
      return withReason(strings.chat.resumeDropped, event.reason);
    default:
      return null;
  }
}

/** The device's one line of why, after the app's own word for what happened. */
function withReason(label: string, reason: string | undefined): string {
  return reason ? `${label} · ${reason}` : label;
}

/** Why a time the reader picked cannot be sent, or null when it can. */
export function resumeBoundError(at: number, now: number = Date.now()): string | null {
  if (at < now + RESUME_MIN_AHEAD_MS) return strings.chat.resumeTooSoon;
  if (at > now + RESUME_MAX_AHEAD_MS) return strings.chat.resumeTooFar;
  return null;
}

const pad = (n: number): string => String(n).padStart(2, '0');

/**
 * `YYYY-MM-DDTHH:mm` in the viewer's own zone, which is what a `datetime-local`
 * field reads and writes. `toISOString` would be UTC and pick the wrong minute.
 */
export function toLocalInputValue(at: number): string {
  const d = new Date(at);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The instant such a field names, or null when it names none. */
export function fromLocalInputValue(value: string): number | null {
  if (value.length === 0) return null;
  const at = new Date(value).getTime();
  return Number.isFinite(at) ? at : null;
}
