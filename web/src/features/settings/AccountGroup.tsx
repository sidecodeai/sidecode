/**
 * A24 — the rows this account has. Users belongs to the admin role; a password
 * belongs to whoever has one, and the gateway refuses the change for the
 * built-in `admin` alone, whose password is its own `RC_PASSWORD`. A second
 * admin account therefore gets both rows. Sign out asks first on both apps
 * (`docs/DESIGN.md` § "The Settings screen").
 */
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { ConfirmDialog } from '../../components/Modal';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { ChangePasswordModal } from './ChangePasswordModal';
import { SettingsGroup } from './SettingsGroup';
import { SettingsActionRow } from './SettingsRow';

export function AccountGroup() {
  const username = useAuth((s) => s.username);
  const role = useAuth((s) => s.role);
  const logout = useAuth((s) => s.logout);
  const navigate = useNavigate();
  const [changingPassword, setChangingPassword] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  return (
    <>
      <SettingsGroup title={strings.settings.account}>
        {role === 'admin' ? (
          <SettingsActionRow
            title={strings.users.title}
            sentence={strings.settings.usersNote}
            onPress={() => navigate('/users')}
          />
        ) : null}
        {username !== null && username !== 'admin' ? (
          <SettingsActionRow
            title={strings.settings.changePassword}
            sentence={strings.settings.changePasswordNote}
            onPress={() => setChangingPassword(true)}
          />
        ) : null}
        <SettingsActionRow
          title={strings.settings.signOut}
          sentence={strings.settings.signOutNote}
          danger
          onPress={() => setSigningOut(true)}
        />
      </SettingsGroup>

      <ConfirmDialog
        open={signingOut}
        title={strings.settings.signOutConfirm}
        body={strings.settings.signOutNote}
        confirmLabel={strings.settings.signOut}
        danger
        onClose={() => setSigningOut(false)}
        onConfirm={async () => {
          await logout();
          navigate('/login', { replace: true });
        }}
      />

      {changingPassword ? (
        <ChangePasswordModal onClose={() => setChangingPassword(false)} />
      ) : null}
    </>
  );
}
