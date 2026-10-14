/**
 * A24: what the account routes of 3.9 refuse, in words, read from `error.code`
 * so the sentence follows the gateway rather than the status line alone.
 */
import { ApiError } from './api';
import { strings } from '../strings';

/**
 * `conflict` means two different things on these routes — a taken username on
 * `POST /api/users`, and `admin` refusing to be changed elsewhere — so the
 * caller supplies the sentence for its own route.
 */
export function userErrorText(err: unknown, conflict: string): string {
  if (!(err instanceof ApiError)) return strings.errors.generic;
  switch (err.code) {
    case 'conflict':
      return conflict;
    case 'bad_request':
      return strings.account.rules;
    case 'forbidden':
      return strings.account.notAllowed;
    case 'not_found':
      return strings.account.gone;
    default:
      return err.message || strings.errors.generic;
  }
}
