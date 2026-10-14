import { useEffect, useState } from 'react';
import { Navigate } from 'react-router';
import { Plus } from 'lucide-react';
import { Button } from '../../components/Button';
import { Switch } from '../../components/Switch';
import type { UserRecord } from '../../protocol/types';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { useUsers } from '../../stores/users';
import { AddUserModal } from './AddUserModal';
import { DeleteUserModal } from './DeleteUserModal';
import { ResetPasswordModal } from './ResetPasswordModal';
import { UserRow } from './UserRow';
import './users.css';

/**
 * A24: the admin's accounts screen. The gateway answers `403` to everyone else,
 * so a member who types the address is sent to Sessions rather than shown an
 * empty page that failed.
 */
export function UsersPage() {
  const role = useAuth((s) => s.role);
  if (role !== 'admin') return <Navigate to="/sessions" replace />;
  return <Users />;
}

function Users() {
  const users = useUsers((s) => s.users);
  const registrationOpen = useUsers((s) => s.registrationOpen);
  const loaded = useUsers((s) => s.loaded);
  const error = useUsers((s) => s.error);
  const load = useUsers((s) => s.load);
  const openRegistration = useUsers((s) => s.openRegistration);
  const toggleState = useUsers((s) => s.toggleState);

  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<UserRecord | null>(null);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <div className="page-head">
        <h1>{strings.users.title}</h1>
        <Button variant="primary" onClick={() => setAdding(true)}>
          <Plus size={15} aria-hidden />
          {strings.users.add}
        </Button>
      </div>

      <div className="users">
        <div className="users-registration surface">
          <div className="users-registration-row">
            <span>{strings.users.registration}</span>
            <Switch
              checked={registrationOpen}
              onChange={(next) => void openRegistration(next)}
              label={strings.users.registration}
            />
          </div>
          <p className="users-registration-caption">{strings.users.registrationCaption}</p>
        </div>

        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}

        {loaded ? (
          <ul className="account-list surface">
            {users.map((user) => (
              <UserRow
                key={user.username}
                user={user}
                actionable={user.username !== 'admin'}
                onResetPassword={() => setResetting(user.username)}
                onToggleState={() => void toggleState(user)}
                onDelete={() => setDeleting(user)}
              />
            ))}
          </ul>
        ) : null}
      </div>

      {adding ? <AddUserModal onClose={() => setAdding(false)} onDone={load} /> : null}

      {resetting !== null ? (
        <ResetPasswordModal
          username={resetting}
          onClose={() => setResetting(null)}
          onDone={load}
        />
      ) : null}

      {deleting !== null ? (
        <DeleteUserModal user={deleting} onClose={() => setDeleting(null)} onDone={load} />
      ) : null}
    </>
  );
}
