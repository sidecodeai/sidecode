/**
 * The caption that closes the screen, and not a group. The browser has no
 * version of its own, so the line is the gateway's and the protocol's, both
 * read from the running code rather than typed (`docs/DESIGN.md` § "The
 * Settings screen"). `hello` answers first; `GET /api/config` is what a browser
 * that has not connected yet has.
 */
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';

export function VersionsLine() {
  const gatewayVersion = useConnection((s) => s.gatewayVersion);
  const socketProtocol = useConnection((s) => s.protocol);
  const configVersion = useAuth((s) => s.version);
  const configProtocol = useAuth((s) => s.protocol);

  const gateway = gatewayVersion ?? configVersion ?? '—';
  const protocol = socketProtocol ?? configProtocol;

  return (
    <p className="settings-versions">
      {strings.settings.versions(gateway, protocol === null ? '—' : `v${protocol}`)}
    </p>
  );
}
