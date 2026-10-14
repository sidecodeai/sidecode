/**
 * A41 — the gateway half of the five Settings values: what changed goes up with
 * `PATCH /api/preferences`, and the object that comes back is what the account
 * holds.
 *
 * Nothing here writes into a store, and nothing here retries. The gateway
 * publishes the whole object to every app socket of the account on every
 * change, so a write this app lost is a write the socket brings back; a
 * refusal leaves the value where the reader put it rather than snapping the
 * control back under the finger.
 */
import { api } from '../lib/api';
import { patchFor, readPreferences, type SyncedKey, type SyncedSettings } from './preferenceFields';

/**
 * Whether the gateway holds the account's preferences at all. False until a
 * `hello` carries them: a gateway older than A35 has no endpoint to write to,
 * and on the login screen there is no account to write for.
 */
let held = false;

/** What `hello` said, and `false` again when the account signs out. */
export function setPreferencesHeld(value: boolean): void {
  held = value;
}

/**
 * Send the named fields and answer with what the reply carries, or `null` when
 * there was nothing to write to and when the gateway refused. A gateway older
 * than a field ignores it and answers an object without it, which leaves this
 * app's value alone.
 */
export async function sendPreferences(
  keys: SyncedKey[],
  values: SyncedSettings,
): Promise<Partial<SyncedSettings> | null> {
  if (!held || keys.length === 0) return null;
  try {
    const result = await api.setPreferences(patchFor(keys, values));
    return readPreferences(result.preferences);
  } catch {
    return null;
  }
}
