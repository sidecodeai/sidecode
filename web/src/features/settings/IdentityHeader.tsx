/**
 * Who is signed in and where, as the screen's first element and on the canvas
 * rather than on a surface (`docs/DESIGN.md` § "The Settings screen"). It
 * replaces the rows Signed in as, Connection, Gateway and Gateway version.
 */
import { cx } from '../../lib/cx';
import { gatewayHost, initials } from '../../lib/identity';
import { roleLabel } from '../../strings';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';
import { connectionTone, connectionWord } from './connectionTone';

/** How much of a long host is kept at its end, so the port always survives. */
const TAIL = 8;

export function IdentityHeader() {
  const username = useAuth((s) => s.username);
  const role = useAuth((s) => s.role);
  const config = useAuth((s) => s.config);
  const status = useConnection((s) => s.status);

  const name = username ?? '—';
  const host = gatewayHost(config?.public_origin ?? window.location.host);
  const word = connectionWord(status);

  return (
    <header className="settings-identity">
      <span className="avatar" aria-hidden>
        {initials(name)}
      </span>
      <div className="settings-identity-text">
        <h2 className="settings-identity-name">{name}</h2>
        <p className="settings-identity-meta">
          {role ? <span className="settings-identity-role">{roleLabel(role)}</span> : null}
          {role ? <span aria-hidden>·</span> : null}
          <span
            className={cx('dot', connectionTone(status))}
            role="img"
            aria-label={word}
            title={word}
          />
          <MiddleTruncated text={host} />
        </p>
      </div>
    </header>
  );
}

/**
 * A host that does not fit loses its middle, not its end: `rc.example…:8443`
 * still says which port. Two spans and no measuring — the tail is fixed and the
 * head is what the row can spare.
 */
function MiddleTruncated({ text }: { text: string }) {
  const cut = Math.max(text.length - TAIL, 0);
  return (
    <span className="settings-identity-host" title={text}>
      <span className="head">{text.slice(0, cut)}</span>
      <span className="tail">{text.slice(cut)}</span>
    </span>
  );
}
