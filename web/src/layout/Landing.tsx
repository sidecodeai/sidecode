import { Navigate } from 'react-router';
import { useDevices } from '../stores/devices';

/**
 * Where an open lands. `docs/DESIGN.md` § "The three screens": Sessions when
 * the account has at least one device and Devices when it has none, because a
 * new account's first job is enrolling a machine and everyone else's is the
 * conversation.
 *
 * The choice is made once per open and is not remembered. This element renders
 * on `/` and on a path no route claims, so it is unmounted by the navigation it
 * performs: someone who then opens Devices on an account with no devices stays
 * there, and a device arriving later moves nobody.
 */
export function Landing() {
  const loaded = useDevices((s) => s.loaded);
  const hasDevice = useDevices((s) => s.devices.length > 0);

  // The socket's snapshot is what fills the list, so deciding before it has
  // synced would send every account to Devices for a round trip.
  if (!loaded) return <div className="boot" aria-busy="true" />;

  return <Navigate to={hasDevice ? '/sessions' : '/devices'} replace />;
}
