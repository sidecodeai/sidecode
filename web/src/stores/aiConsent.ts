/**
 * The person's permission, in this browser, for the AI features that send what
 * they say or write to a third-party provider: dictation, polish and the voice
 * assistant. Each feature asks before its first use; what the person agreed to
 * is kept here per account and feature as `<text version>|<provider name>`,
 * the name empty when the gateway named none. A new text, or a provider other
 * than the one agreed to, asks again: what is stored no longer matches.
 *
 * It is this browser's alone, like the app's other per-account conveniences:
 * nothing goes to the gateway. Signing out removes the account's permissions,
 * and so does deleting the account in this browser; a session the gateway
 * ended keeps them. The task board's permission is the account preference
 * `task_board` instead (A62), which the gateway holds for every app.
 *
 * Polish has one more thing kept beside them: Not now said at a dictation,
 * when another device turned polish on for the account. It is kept for the text
 * and the provider it was said to, like a permission, and the browser does not
 * ask at a dictation again until polish is agreed to here or either changes.
 */
import { useSyncExternalStore } from 'react';
import { readStored, storage } from './browserStorage';

export type ConsentFeature = 'dictation' | 'polish' | 'assistant';

/** Each feature's permission text, by version: a new version asks again. */
export const CONSENT_VERSIONS: Record<ConsentFeature, number> = {
  dictation: 1,
  polish: 1,
  assistant: 2,
};

const FEATURES = Object.keys(CONSENT_VERSIONS) as ConsentFeature[];

const keyOf = (username: string, feature: ConsentFeature): string => `rc.consent.${feature}.${username}`;

const declinedKey = (username: string): string => `rc.consent.declined.polish.${username}`;

/** What agreeing to the present text naming `provider` stores. */
export const consentValue = (feature: ConsentFeature, provider: string | null): string =>
  `${CONSENT_VERSIONS[feature]}|${provider ?? ''}`;

const listeners = new Set<() => void>();
const notify = (): void => listeners.forEach((listener) => listener());

/** Every change made here, and one another tab of this browser made. */
function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener('storage', listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', listener);
  };
}

/** What the account agreed to for the feature, as stored, or null when it never did. */
export function storedConsent(username: string, feature: ConsentFeature): string | null {
  return readStored(keyOf(username, feature));
}

/** Whether the account agreed to the feature's present text, naming this provider. */
export const hasConsent = (username: string, feature: ConsentFeature, provider: string | null): boolean =>
  storedConsent(username, feature) === consentValue(feature, provider);

/**
 * The person agreed. A browser that cannot keep it is asked again next time,
 * which is the cost of not keeping it and nothing more.
 */
export function grantConsent(username: string, feature: ConsentFeature, provider: string | null): void {
  try {
    void storage.setItem(keyOf(username, feature), consentValue(feature, provider));
  } catch {
    // Nothing kept: the next use asks again.
  }
  if (feature === 'polish') void storage.removeItem(declinedKey(username));
  notify();
}

/** Not now, at a dictation another device's polish setting would have polished. */
export function declinePolish(username: string, provider: string | null): void {
  try {
    void storage.setItem(declinedKey(username), consentValue('polish', provider));
  } catch {
    // Nothing kept: the next dictation asks again.
  }
  notify();
}

/** Whether Not now was said at a dictation to the present polish text naming this provider. */
export const polishDeclined = (username: string, provider: string | null): boolean =>
  readStored(declinedKey(username)) === consentValue('polish', provider);

/** The person withdrew it, in Settings. */
export function withdrawConsent(username: string, feature: ConsentFeature): void {
  void storage.removeItem(keyOf(username, feature));
  notify();
}

/** Every permission of the account goes: it signed out, or was deleted, in this browser. */
export function forgetConsents(username: string): void {
  for (const feature of FEATURES) void storage.removeItem(keyOf(username, feature));
  void storage.removeItem(declinedKey(username));
  notify();
}

/**
 * Whether the account agreed to the feature's present text naming `provider`,
 * following every change; never for nobody signed in.
 */
export function useConsent(username: string | null, feature: ConsentFeature, provider: string | null): boolean {
  const stored = useSyncExternalStore(subscribe, () => (username ? storedConsent(username, feature) : null));
  return stored !== null && stored === consentValue(feature, provider);
}

/** Whether Not now holds for polish at a dictation, following every change. */
export function usePolishDeclined(username: string | null, provider: string | null): boolean {
  const stored = useSyncExternalStore(subscribe, () => (username ? readStored(declinedKey(username)) : null));
  return stored !== null && stored === consentValue('polish', provider);
}
