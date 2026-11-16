/**
 * Who is signed in, as the account button and its menu say it: the name, its
 * initials, and a line under the name — the gateway's host on the sidebar's
 * button, and the role before the host at the head of the menu.
 */
import { gatewayHost, initials } from '../../lib/identity';
import { roleLabel, strings } from '../../strings';
import { useAuth } from '../../stores/auth';

export interface AccountSummary {
  name: string;
  initials: string;
  /** The line under the name on the sidebar's button. */
  secondary: string;
  /** The line under the name at the head of the menu. */
  detail: string;
}

export function useAccountSummary(): AccountSummary {
  const name = useAuth((s) => s.username) ?? '';
  const role = useAuth((s) => s.role);
  const origin = useAuth((s) => s.config?.public_origin) ?? window.location.host;

  const host = gatewayHost(origin);
  return {
    name,
    initials: initials(name),
    secondary: host,
    detail: role ? strings.accountMenu.roleAtHost(roleLabel(role), host) : host,
  };
}
