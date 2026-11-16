import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router';
import { ConfirmDialog } from '../../components/Modal';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * Sign out, asked first: the gateway ends the session, then the login screen.
 * The account menu and the Settings Sign out row both open this one dialog, so
 * the two ask in the same words and do the same thing. On the body, because
 * the menu's buttons sit in a pane hidden below 1024 px and in a top bar whose
 * blur would hold a fixed dialog inside it.
 */
export function SignOutDialog({ open, onClose }: Props) {
  const logout = useAuth((s) => s.logout);
  const navigate = useNavigate();
  if (!open) return null;
  return createPortal(
    <ConfirmDialog
      open
      title={strings.settings.signOutConfirm}
      body={strings.settings.signOutNote}
      confirmLabel={strings.settings.signOut}
      danger
      onClose={onClose}
      onConfirm={async () => {
        await logout();
        navigate('/login', { replace: true });
      }}
    />,
    document.body,
  );
}
