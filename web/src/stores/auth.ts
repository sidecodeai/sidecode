/** Session cookie state. The token itself never touches JavaScript. */
import { create } from 'zustand';
import { api, isUnauthorized, type ConfigResponse, type User, type UserRole } from '../lib/api';

type AuthStatus = 'unknown' | 'signed-in' | 'signed-out';

interface AuthState {
  status: AuthStatus;
  username: string | null;
  /** A24: what the accounts screen of 3.9 is gated on. */
  role: UserRole | null;
  config: ConfigResponse | null;
  version: string | null;
  protocol: number | null;
  check: () => Promise<void>;
  login: (username: string, password: string) => Promise<User>;
  register: (username: string, password: string) => Promise<User>;
  logout: () => Promise<void>;
  markSignedOut: () => void;
  loadConfig: () => Promise<void>;
}

export const useAuth = create<AuthState>((set, get) => ({
  status: 'unknown',
  username: null,
  role: null,
  config: null,
  version: null,
  protocol: null,

  loadConfig: async () => {
    try {
      const [config, health] = await Promise.all([api.config(), api.health()]);
      set({ config, version: health.version, protocol: health.protocol });
    } catch {
      /* about/diagnostics detail is optional for rendering */
    }
  },

  check: async () => {
    try {
      const session = await api.session();
      set({ status: 'signed-in', ...identity(session.user) });
      void get().loadConfig();
    } catch (err) {
      set({ status: 'signed-out', username: null, role: null });
      if (!isUnauthorized(err)) {
        // Network failure: the login screen surfaces "cannot reach the gateway".
        set({ config: null });
      }
    }
  },

  login: async (username, password) => {
    const res = await api.login(username, password);
    set({ status: 'signed-in', ...identity(res.user) });
    void get().loadConfig();
    return res.user;
  },

  // A24: registering is a sign-in, so the caller has nothing else to do.
  register: async (username, password) => {
    const res = await api.register(username, password);
    set({ status: 'signed-in', ...identity(res.user) });
    void get().loadConfig();
    return res.user;
  },

  logout: async () => {
    try {
      await api.logout();
    } finally {
      set({ status: 'signed-out', username: null, role: null });
    }
  },

  markSignedOut: () => set({ status: 'signed-out', username: null, role: null }),
}));

const identity = (user: User): { username: string; role: UserRole } => ({
  username: user.username,
  role: user.role,
});
