/**
 * Who receives what each AI feature sends, as the gateway names them for the
 * permissions the app asks (A65): the dictation (speech) provider, the polish
 * model's and the task board's. `hello` says it on every connect, so a name the
 * operator changed is seen as soon as the socket is back; before the first
 * `hello` the configuration read after sign-in says it. A gateway that names
 * none leaves each permission in its unnamed words. The voice assistant's
 * provider comes with its own status (`features/live/api.ts`).
 */
import { useMemo } from 'react';
import type { ConfigResponse } from '../../lib/api';
import { providerName } from '../../lib/providerName';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';

export interface ProviderNames {
  dictation: string | null;
  polish: string | null;
  board: string | null;
}

/** The names in the configuration, read as data: a part it lacks names nobody. */
export function providerNamesOf(config: ConfigResponse | null): ProviderNames {
  return {
    dictation: providerName(config?.stt?.provider_name),
    polish: providerName(config?.polish?.provider_name),
    board: providerName(config?.board?.provider_name),
  };
}

/** The names the last `hello` gave, or the configuration's before one came. */
export function useProviderNames(): ProviderNames {
  const config = useAuth((s) => s.config);
  const greeted = useConnection((s) => s.helloAt !== null);
  const stt = useConnection((s) => s.stt.provider_name);
  const polish = useConnection((s) => s.polish.provider_name);
  const board = useConnection((s) => s.board?.provider_name);
  return useMemo(
    () =>
      greeted
        ? { dictation: providerName(stt), polish: providerName(polish), board: providerName(board) }
        : providerNamesOf(config),
    [greeted, stt, polish, board, config],
  );
}
