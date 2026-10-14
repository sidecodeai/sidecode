/**
 * A41 — the five Settings values the web keeps on the account, in the two
 * spellings they live under: the settings store's own names and the wire's.
 * `stt_language` is not one of them: since A44 it is the language an iPhone's
 * own recogniser listens for, and the web, which only transcribes on the
 * gateway, neither reads it nor writes it.
 *
 * Reading one off the wire checks it. The gateway is free to carry a word this
 * build has never heard of — a newer app of the same account may have written
 * it — and an unknown word must never reach a control, which would draw a
 * segmented button with nothing selected and write nonsense back.
 */
import type {
  InterfaceLanguage,
  PolishStrength,
  Preferences,
  TimelineDetail,
} from '../protocol/types';

/** The settings store's names for the five, and what each may hold. */
export interface SyncedSettings {
  /**
   * The app's own words, never applied to anything a device reported. English
   * until somebody asks for something else — never guessed from
   * `navigator.language`, because a developer whose system is Chinese still
   * reads the agent in English and a surprise translation at first launch
   * reads as a different product.
   */
  language: InterfaceLanguage;
  /**
   * A29: whether a finished dictation is passed through the gateway's polish
   * model. Off until the reader turns it on, and useless without a model.
   */
  polishEnabled: boolean;
  polishModel: string;
  polishStrength: PolishStrength;
  /** How much of a transcript is drawn. Simple by default. */
  timelineDetail: TimelineDetail;
}

export type SyncedKey = keyof SyncedSettings;

/** In the order Settings offers them; the first of each is the default. */
export const INTERFACE_LANGUAGES: InterfaceLanguage[] = ['en', 'zh-Hans'];
export const POLISH_STRENGTHS: PolishStrength[] = ['moderate', 'strong'];
export const TIMELINE_DETAILS: TimelineDetail[] = ['simple', 'detailed'];

/** What each is called on the wire (3.2). */
const WIRE: { [K in SyncedKey]: keyof Preferences } = {
  language: 'language',
  polishEnabled: 'polish_enabled',
  polishModel: 'polish_model',
  polishStrength: 'polish_strength',
  timelineDetail: 'timeline_detail',
};

export const SYNCED_KEYS = Object.keys(WIRE) as SyncedKey[];

const word = <T extends string>(value: unknown, words: readonly T[]): T | undefined =>
  typeof value === 'string' ? words.find((known) => known === value) : undefined;

const text = (value: unknown, max: number, empty: boolean): string | undefined =>
  typeof value === 'string' && value.length <= max && (empty || value.length > 0)
    ? value
    : undefined;

const flag = (value: unknown): boolean | undefined =>
  typeof value === 'boolean' ? value : undefined;

/** The lengths and the words of `objects.json#/$defs/Preferences`. */
const readers: { [K in SyncedKey]: (value: unknown) => SyncedSettings[K] | undefined } = {
  language: (value) => word(value, INTERFACE_LANGUAGES),
  polishEnabled: flag,
  polishModel: (value) => text(value, 128, true),
  polishStrength: (value) => word(value, POLISH_STRENGTHS),
  timelineDetail: (value) => word(value, TIMELINE_DETAILS),
};

const readInto = <K extends SyncedKey>(
  into: Partial<SyncedSettings>,
  preferences: Preferences,
  key: K,
): void => {
  const value = readers[key]((preferences as unknown as Record<string, unknown>)[WIRE[key]]);
  if (value !== undefined) into[key] = value;
};

/** Every field of an account's preferences that this build can read. */
export function readPreferences(preferences: Preferences): Partial<SyncedSettings> {
  const read: Partial<SyncedSettings> = {};
  for (const key of SYNCED_KEYS) readInto(read, preferences, key);
  return read;
}

/**
 * The fields nobody has set yet — what an app writes up once, so the account
 * keeps what its first app had rather than being handed the defaults.
 */
export function unsetKeys(preferences: Preferences): SyncedKey[] {
  const read = readPreferences(preferences);
  return SYNCED_KEYS.filter((key) => read[key] === undefined);
}

const writeInto = <K extends SyncedKey>(
  into: Record<string, unknown>,
  values: SyncedSettings,
  key: K,
): void => {
  into[WIRE[key]] = values[key];
};

/** Some of this app's values as `PATCH /api/preferences` spells them. */
export function patchFor(keys: SyncedKey[], values: SyncedSettings): Partial<Preferences> {
  const patch: Record<string, unknown> = {};
  for (const key of keys) writeInto(patch, values, key);
  return patch as Partial<Preferences>;
}

const copyIfDifferent = <K extends SyncedKey>(
  into: Partial<SyncedSettings>,
  read: Partial<SyncedSettings>,
  held: SyncedSettings,
  key: K,
): void => {
  const value = read[key];
  if (value !== undefined && value !== held[key]) into[key] = value;
};

/**
 * What of `read` the store does not already hold. A frame that carries the
 * value already on the screen must change nothing: that is what keeps this
 * app from answering the gateway's own echo with another write.
 */
export function changesFrom(
  read: Partial<SyncedSettings>,
  held: SyncedSettings,
): Partial<SyncedSettings> {
  const changes: Partial<SyncedSettings> = {};
  for (const key of SYNCED_KEYS) copyIfDifferent(changes, read, held, key);
  return changes;
}
