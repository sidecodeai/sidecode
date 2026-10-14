/** Maps a rejected gateway request to a sentence the user can act on. */
import { strings } from '../strings';
import { RequestError } from './ws';

/**
 * Read when the failure happens, never at import: a table captured at module
 * scope would keep speaking the language the app booted in.
 */
function byCode(code: string): string | undefined {
  const sentences: Record<string, string> = {
    not_found: strings.errors.notFound,
    device_offline: strings.errors.deviceOffline,
    conflict: strings.errors.conflictTerminal,
    timeout: strings.errors.timeout,
    unsupported: strings.errors.unsupported,
    too_large: strings.errors.tooLarge,
  };
  return sentences[code];
}

/**
 * The code decides the sentence. A locally minted failure — the app socket is
 * not open, the gateway never answered — carries no message at all, so an empty
 * one falls through to the caller's own fallback rather than reaching the
 * screen as an empty line.
 */
export function errorText(error: unknown, fallback: string = strings.errors.generic): string {
  if (error instanceof RequestError) return byCode(error.code) || error.message || fallback;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

/**
 * A40: what `session.set` and `session.command` say when they are refused.
 * A `conflict` from either is the device explaining why it could not reach the
 * session — the terminal it types into is running a turn, or somebody is
 * typing there — and only the device knows which. Its sentence is shown as it
 * arrived; the canned one, about taking the session over, would be wrong.
 * Every other code keeps the app's own words.
 */
export function refusalText(error: unknown, fallback: string): string {
  if (error instanceof RequestError && error.code === 'conflict' && error.message) {
    return error.message;
  }
  return errorText(error, fallback);
}

/**
 * A43: what a refused `session.queue_remove` says. `not_found` means the device
 * delivered the message before the request reached it, for a Remove as for an
 * edit, so the sentence says that and never the bare code.
 */
export function queueRemoveText(error: unknown): string {
  if (error instanceof RequestError && error.code === 'not_found') {
    return strings.composer.alreadySent;
  }
  return errorText(error, strings.errors.queueRemoveFailed);
}
