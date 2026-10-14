import { MoreHorizontal } from 'lucide-react';
import { Popover } from '../../components/Popover';
import { cx } from '../../lib/cx';
import { relativeAgo } from '../../lib/format';
import type { UserRecord } from '../../protocol/types';
import { roleLabel, strings, userStateLabel } from '../../strings';

interface Props {
  user: UserRecord;
  /** `admin` is the operator: it has no actions, so it is given no menu. */
  actionable: boolean;
  onResetPassword: () => void;
  onToggleState: () => void;
  onDelete: () => void;
}

export function UserRow({ user, actionable, onResetPassword, onToggleState, onDelete }: Props) {
  const disabled = user.state === 'disabled';
  const lastSignIn = strings.users.lastSignIn(
    user.last_login_at === null ? strings.users.never : relativeAgo(user.last_login_at),
  );

  return (
    <li className={cx('account-row', disabled && 'disabled')}>
      <div className="account-main">
        <div className="account-name">{user.username}</div>
        <div className="account-meta">
          <span>{strings.users.meta(roleLabel(user.role), userStateLabel(user.state))}</span>
          <span>
            {strings.users.deviceCount(user.devices)} · {lastSignIn}
          </span>
        </div>
      </div>

      {actionable ? (
        <Popover
          chevron={false}
          ariaLabel={strings.a11y.openMenu}
          align="end"
          triggerClassName="account-menu-trigger"
          label={<MoreHorizontal size={16} aria-hidden />}
        >
          {(close) => (
            <ul className="menu" role="menu">
              <li>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  onClick={() => {
                    close();
                    onResetPassword();
                  }}
                >
                  <span className="menu-label">{strings.users.resetPassword}</span>
                </button>
              </li>
              <li>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  onClick={() => {
                    close();
                    onToggleState();
                  }}
                >
                  <span className="menu-label">
                    {disabled ? strings.users.enable : strings.users.disable}
                  </span>
                </button>
              </li>
              <li>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item danger-text"
                  onClick={() => {
                    close();
                    onDelete();
                  }}
                >
                  <span className="menu-label">{strings.users.deleteAction}</span>
                </button>
              </li>
            </ul>
          )}
        </Popover>
      ) : null}
    </li>
  );
}
