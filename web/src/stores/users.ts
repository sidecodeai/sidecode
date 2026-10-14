/**
 * A24: the accounts on this gateway, as the admin sees them. Nothing pushes
 * them over the app socket, so the list is whatever the last `GET /api/users`
 * said and every write re-reads it.
 */
import { create } from 'zustand';
import { api } from '../lib/api';
import { userErrorText } from '../lib/accountErrors';
import type { UserRecord } from '../protocol/types';
import { strings } from '../strings';

interface UsersState {
  users: UserRecord[];
  registrationOpen: boolean;
  loaded: boolean;
  /** Why the last action that has no dialog of its own failed. */
  error: string | null;
  load: () => Promise<void>;
  openRegistration: (open: boolean) => Promise<void>;
  toggleState: (user: UserRecord) => Promise<void>;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

export const useUsers = create<UsersState>((set, get) => ({
  users: [],
  registrationOpen: false,
  loaded: false,
  error: null,

  load: async () => {
    try {
      const list = await api.users();
      set({
        users: list.users,
        registrationOpen: list.registration_open,
        loaded: true,
        error: null,
      });
    } catch {
      set({ loaded: true, error: strings.users.loadFailed });
    }
  },

  // The switch answers the tap and goes back if the gateway refuses it.
  openRegistration: async (open) => {
    set({ registrationOpen: open, error: null });
    try {
      const result = await api.setRegistration(open);
      set({ registrationOpen: result.open });
    } catch {
      set({ registrationOpen: !open, error: strings.users.registrationFailed });
    }
  },

  toggleState: async (user) => {
    set({ error: null });
    try {
      await api.patchUser(user.username, {
        state: user.state === 'disabled' ? 'active' : 'disabled',
      });
      await get().load();
    } catch (err) {
      set({ error: userErrorText(err, strings.account.notAllowed) });
    }
  },

  reset: () => set({ users: [], registrationOpen: false, loaded: false, error: null }),
}));
