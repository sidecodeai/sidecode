/**
 * Who is signed in and where (`docs/DESIGN.md` § "The Settings screen"): the
 * initials in a circle, the name, and `role · host` with the connection's dot
 * before the host. It opens the Account section, on the canvas rather than on a
 * surface, and on a narrow screen it is also the first row of the section list,
 * where it leads to that section.
 */
import { ChevronRight } from 'lucide-react';
import { Link } from 'react-router';
import { cx } from '../../lib/cx';
import { gatewayHost, initials } from '../../lib/identity';
import { roleLabel } from '../../strings';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';
import { connectionTone, connectionWord } from './connectionTone';
import { sectionPath } from './sections';

/** How much of a long host is kept at its end, so the port always survives. */
const TAIL = 8;

export function IdentityHeader() {
  return (
    <header className="settings-identity">
      <Identity />
    </header>
  );
}

/** The same, as the narrow list's first row: on a surface of its own, and a link to Account. */
export function IdentityRow() {
  return (
    <div className="surface">
      <Link to={sectionPath('account')} className="settings-identity target">
        <Identity />
        <ChevronRight size={16} aria-hidden className="settings-row-chevron" />
      </Link>
    </div>
  );
}

function Identity() {
  const username = useAuth((s) => s.username);
  const role = useAuth((s) => s.role);
  const config = useAuth((s) => s.config);
  const status = useConnection((s) => s.status);

  const name = username ?? '—';
  const host = gatewayHost(config?.public_origin ?? window.location.host);
  const word = connectionWord(status);

  return (
    <>
      <span className="avatar" aria-hidden>
        {initials(name)}
      </span>
      <span className="settings-identity-text">
        <span className="settings-identity-name">{name}</span>
        <span className="settings-identity-meta">
          {role ? <span className="settings-identity-role">{roleLabel(role)}</span> : null}
          {role ? <span aria-hidden>·</span> : null}
          <span
            className={cx('dot', connectionTone(status))}
            role="img"
            aria-label={word}
            title={word}
          />
          <MiddleTruncated text={host} />
        </span>
      </span>
    </>
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
