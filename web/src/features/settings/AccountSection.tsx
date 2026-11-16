/**
 * The Account section: who is signed in and where, then how the account signs
 * in and signing out (`AccountGroup`).
 */
import { AccountGroup } from './AccountGroup';
import { IdentityHeader } from './IdentityHeader';

export function AccountSection() {
  return (
    <>
      <IdentityHeader />
      <AccountGroup />
    </>
  );
}
