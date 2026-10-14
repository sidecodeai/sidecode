/**
 * A33 — the words a device page puts on an account and its quota windows.
 *
 * Everything here is a pure function of what the device reported. The vendor's
 * name comes from the small table in `strings.ts`; the plan word, the tier, the
 * email and the third-party host are printed exactly as they arrived, in both
 * interface languages, because they are data rather than the app's own words.
 * `docs/DESIGN.md` § "A device has a page" and § "Quota is a meter".
 */
import { strings, vendorLabel } from '../../strings';
import type { AgentAccount, AgentLimit } from '../../protocol/types';

/** The plan reads as a word rather than as an id: `max` is drawn "Max". */
function planWord(plan: string): string {
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

/**
 * The one line under an agent's name. Both methods lead with the vendor:
 * "Anthropic account · Max · Max 5x · me@example.com", "Anthropic API key", or
 * "OpenAI API key · api.relay.example". Nothing is drawn for what the device
 * did not report.
 */
export function signInLine(account: AgentAccount): string {
  const vendor = vendorLabel(account.provider);
  if (account.method === 'api_key') {
    const key = strings.devicePage.apiKeyOf(vendor);
    return account.endpoint ? `${key} · ${account.endpoint}` : key;
  }
  const parts = [strings.devicePage.accountOf(vendor)];
  if (account.plan) parts.push(planWord(account.plan));
  if (account.tier) parts.push(account.tier);
  if (account.email) parts.push(account.email);
  return parts.join(' · ');
}

/**
 * A window's length in the reader's units: 300 minutes is "5-hour", 1440 is
 * "24-hour", 10080 is "7-day". A day is only reached above a day's worth of
 * hours, so a window of exactly 24 hours stays an hour window.
 */
function windowLength(minutes: number): string {
  if (minutes % 60 !== 0) return strings.devicePage.windowMinutes(minutes);
  const hours = minutes / 60;
  if (hours > 24 && minutes % 1440 === 0) return strings.devicePage.windowDays(minutes / 1440);
  return strings.devicePage.windowHours(hours);
}

/** The window's name, with what it is confined to after it: "7-day · Fable". */
export function windowName(limit: AgentLimit): string {
  const length = windowLength(limit.window_minutes);
  return limit.scope ? `${length} · ${limit.scope}` : length;
}

/**
 * The band the meter's fill is drawn in: the ink colour up to 80 % of the
 * window, the warning colour past it, the danger colour once it is spent.
 */
export type MeterTone = 'ink' | 'warn' | 'danger';

export function meterTone(usedPercent: number): MeterTone {
  if (usedPercent >= 100) return 'danger';
  if (usedPercent > 80) return 'warn';
  return 'ink';
}

/** The share of the window that is drawn and printed, clamped to the meter. */
export function usedPercent(limit: AgentLimit): number {
  return Math.min(100, Math.max(0, Math.round(limit.used_percent)));
}

/**
 * When the window resets, in the reader's own words: "resets 15:40" for a reset
 * later today, "resets Tue 22:00" for one on another day.
 */
export function resetsText(resetsAt: number, now: number = Date.now()): string {
  const when = new Date(resetsAt);
  const locale = strings.format.dateLocale;
  const time = when.toLocaleTimeString(locale, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const today = new Date(now);
  const sameDay =
    when.getFullYear() === today.getFullYear() &&
    when.getMonth() === today.getMonth() &&
    when.getDate() === today.getDate();
  if (sameDay) return strings.devicePage.resets(time);
  return strings.devicePage.resets(`${when.toLocaleDateString(locale, { weekday: 'short' })} ${time}`);
}
