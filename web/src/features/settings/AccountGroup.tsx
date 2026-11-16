/**
 * A24 — the Account section's two groups under the header. **Sign-in** is how
 * this account signs in, and nothing else: the password, which belongs to
 * whoever has one — the gateway refuses the change for the built-in `admin`
 * alone, whose password is its own `RC_PASSWORD`, so `admin` has no such group.
 * The next group has no caption: Users for the admin role, so a second admin
 * account gets the password and Users both, and Sign out, which asks first
 * (`docs/DESIGN.md` § "The Settings screen").
 */
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { SignOutDialog } from '../account-menu/SignOutDialog';
import { ChangePasswordModal } from './ChangePasswordModal';
import { SettingsGroup } from './SettingsGroup';
import { SettingsActionRow } from './SettingsRow';

export function AccountGroup() {
  const username = useAuth((s) => s.username);
  const role = useAuth((s) => s.role);
  const navigate = useNavigate();
  const [changingPassword, setChangingPassword] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  const ownAccount = username !== null && username !== 'admin';

  return (
    <>
      {ownAccount ? (
        <SettingsGroup title={strings.settings.signInGroup}>
          <SettingsActionRow
            title={strings.settings.changePassword}
            sentence={strings.settings.changePasswordNote}
            onPress={() => setChangingPassword(true)}
          />
        </SettingsGroup>
      ) : null}
      <SettingsGroup>
        {role === 'admin' ? (
          <SettingsActionRow
            title={strings.users.title}
            sentence={strings.settings.usersNote}
            onPress={() => navigate('/users')}
          />
        ) : null}
        <SettingsActionRow
          title={strings.settings.signOut}
          sentence={strings.settings.signOutNote}
          danger
          onPress={() => setSigningOut(true)}
        />
      </SettingsGroup>

      {/* The one Sign out dialog, which the account menu opens too. */}
      <SignOutDialog open={signingOut} onClose={() => setSigningOut(false)} />

      {changingPassword ? <ChangePasswordModal onClose={() => setChangingPassword(false)} /> : null}
    </>
  );
}
