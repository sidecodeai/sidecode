/**
 * The last line of the footer caption, and not a group. The browser has no
 * version of its own, so the line is the gateway's and the protocol's, both
 * read from the running code rather than typed (`docs/DESIGN.md` § "The
 * Settings screen"). `hello` answers first; `GET /api/config` is what a browser
 * that has not connected yet has. The line is drawn as its parts, each kept
 * whole, so a narrow column breaks it only between them and the dot that parts
 * them is the stylesheet's (`settings.css`).
 */
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';

/** What the string tables put between the parts of the line, in both languages. */
const SEPARATOR = ' · ';

export function VersionsLine() {
  const gatewayVersion = useConnection((s) => s.gatewayVersion);
  const socketProtocol = useConnection((s) => s.protocol);
  const configVersion = useAuth((s) => s.version);
  const configProtocol = useAuth((s) => s.protocol);

  const gateway = gatewayVersion ?? configVersion ?? '—';
  const protocol = socketProtocol ?? configProtocol;

  const line = strings.settings.versions(gateway, protocol === null ? '—' : `v${protocol}`);
  return (
    <p className="settings-versions">
      {line.split(SEPARATOR).map((part) => (
        <span key={part} className="settings-versions-part">
          {part}
        </span>
      ))}
    </p>
  );
}
