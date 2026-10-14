import { NavLink, Outlet } from 'react-router';
import { cx } from '../lib/cx';
import { gatewayHost, initials } from '../lib/identity';
import { strings } from '../strings';
import { useAuth } from '../stores/auth';
import { useConnection } from '../stores/connection';
import { Mark } from './Mark';
import './layout.css';

/** Built on every render, so the tabs follow the interface language. */
function tabs(): { to: string; label: string }[] {
  return [
    { to: '/devices', label: strings.nav.devices },
    { to: '/sessions', label: strings.nav.sessions },
    { to: '/settings', label: strings.nav.settings },
  ];
}

export function AppLayout() {
  const config = useAuth((s) => s.config);
  const username = useAuth((s) => s.username);
  const status = useConnection((s) => s.status);
  const origin = config?.public_origin ?? window.location.host;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <Mark />
            <span>{strings.productName}</span>
          </div>
          <nav className="tabs" aria-label={strings.nav.primary}>
            {tabs().map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                className={({ isActive }) => cx('tab', isActive && 'active')}
              >
                {tab.label}
              </NavLink>
            ))}
          </nav>
          <div className="topbar-right">
            {status !== 'open' ? (
              <span className="conn-chip" title={strings.connection.reconnecting}>
                <span className="spinner" aria-hidden />
                <span className="sr-only">{strings.connection.reconnecting}</span>
              </span>
            ) : null}
            <span className="origin mono" title={origin}>
              {gatewayHost(origin)}
            </span>
            <span className="avatar" aria-hidden>
              {initials(username ?? '?')}
            </span>
          </div>
        </div>
      </header>
      <main className="page scroll-thin">
        <Outlet />
      </main>
    </div>
  );
}
