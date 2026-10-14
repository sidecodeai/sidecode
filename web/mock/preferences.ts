/**
 * A35, A41 §3.2 — the account's preferences as the mock keeps them: a `PATCH`
 * sets the fields it carries, each checked the way the gateway checks it, and
 * an unknown field is ignored. The whole object is what the reply carries and
 * what goes out as `preferences.updated`, so two tabs of the mock read the
 * same values within the round trip.
 */
import type { Preferences } from '../src/protocol/types';

/** An account that has chosen nothing: the switch off and no field set. */
export const emptyPreferences = (): Preferences => ({ resume_after_limit: false });

export type PatchOutcome = { preferences: Preferences } | { error: string };

type Check = (value: unknown) => boolean;

const isBoolean: Check = (value) => typeof value === 'boolean';

const isWord =
  (...words: string[]): Check =>
  (value) =>
    typeof value === 'string' && words.includes(value);

const isText =
  (max: number, empty: boolean): Check =>
  (value) =>
    typeof value === 'string' && value.length <= max && (empty || value.length > 0);

/** Every field of `objects.json#/$defs/Preferences`, with what it may hold. */
const FIELDS: Record<string, Check> = {
  resume_after_limit: isBoolean,
  language: isWord('en', 'zh-Hans'),
  // A44: an iPhone's own recogniser language. The web never writes it, but the
  // gateway keeps what a phone of the same account does.
  stt_language: isText(32, false),
  polish_enabled: isBoolean,
  polish_model: isText(128, true),
  polish_strength: isWord('moderate', 'strong'),
  timeline_detail: isWord('simple', 'detailed'),
};

/**
 * The account's preferences after a `PATCH`, or the name of the field that
 * was refused — a wrong type or a word the contract does not know is a
 * `bad_request`, while a field the contract has never heard of is ignored.
 */
export function patchPreferences(
  current: Preferences,
  body: Record<string, unknown>,
): PatchOutcome {
  const next: Record<string, unknown> = { ...current };
  for (const [field, value] of Object.entries(body)) {
    const check = FIELDS[field];
    if (check === undefined) continue;
    if (!check(value)) return { error: field };
    next[field] = value;
  }
  return { preferences: next as unknown as Preferences };
}
