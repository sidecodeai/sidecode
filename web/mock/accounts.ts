/**
 * A24: the accounts the mock gateway knows. One admin, one member, and a
 * registration switch, so every screen the amendment adds can be driven with no
 * real gateway. Passwords are compared in the clear: this is a development
 * fixture and never sees a real one.
 */
export type Role = 'admin' | 'member';
export type State = 'active' | 'disabled';

export interface Account {
  username: string;
  role: Role;
  state: State;
  password: string;
  created_at: number;
  last_login_at: number | null;
}

/** The gateway's rules (PROTOCOL.md §3.1), written once here as there. */
const USERNAME = /^[a-z0-9][a-z0-9._-]{2,31}$/;

export const validUsername = (username: string): boolean => USERNAME.test(username);

export const validPassword = (password: string): boolean =>
  password.length >= 8 && password.length <= 128;

const accounts = new Map<string, Account>();
let registrationOpen = false;

/** The member the mock ships with, so a second account exists from the start. */
export const MEMBER_USERNAME = 'alice';
export const MEMBER_PASSWORD = 'devdevdev';

export function seedAccounts(adminPassword: string): void {
  const now = Date.now();
  accounts.clear();
  registrationOpen = false;
  accounts.set('admin', {
    username: 'admin',
    role: 'admin',
    state: 'active',
    password: adminPassword,
    created_at: now - 30 * 86_400_000,
    last_login_at: null,
  });
  accounts.set(MEMBER_USERNAME, {
    username: MEMBER_USERNAME,
    role: 'member',
    state: 'active',
    password: MEMBER_PASSWORD,
    created_at: now - 3 * 86_400_000,
    last_login_at: now - 4 * 3_600_000,
  });
}

export const getAccount = (username: string): Account | undefined =>
  accounts.get(username.toLowerCase());

/** Oldest first, the order `GET /api/users` promises. */
export const listAccounts = (): Account[] =>
  [...accounts.values()].sort((a, b) => a.created_at - b.created_at);

export function createAccount(username: string, password: string, role: Role): Account {
  const account: Account = {
    username: username.toLowerCase(),
    role,
    state: 'active',
    password,
    created_at: Date.now(),
    last_login_at: null,
  };
  accounts.set(account.username, account);
  return account;
}

export const deleteAccount = (username: string): void => void accounts.delete(username);

export const isRegistrationOpen = (): boolean => registrationOpen;

export const setRegistrationOpen = (open: boolean): void => void (registrationOpen = open);

/** The `User` of 4.10: what a sign-in and the app `hello` carry. */
export const userView = (account: Account): { username: string; role: Role } => ({
  username: account.username,
  role: account.role,
});

/** The `UserRecord` of 4.10: one account as the admin lists it. */
export const recordView = (
  account: Account,
  devices: number,
): {
  username: string;
  role: Role;
  state: State;
  created_at: number;
  last_login_at: number | null;
  devices: number;
} => ({ ...userView(account), state: account.state, created_at: account.created_at, last_login_at: account.last_login_at, devices });
