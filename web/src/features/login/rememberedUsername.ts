/**
 * The username the last sign-in used, so the next one is the password alone
 * (`docs/DESIGN.md` § "Accounts"). `localStorage` is already scoped to the
 * gateway's origin, so one key remembers one name per gateway. It is a
 * convenience: a private window or a blocked store just means an empty field.
 */
const KEY = 'rc.username';

export function rememberedUsername(): string {
  try {
    return globalThis.localStorage?.getItem(KEY) ?? '';
  } catch {
    return '';
  }
}

export function rememberUsername(username: string): void {
  try {
    globalThis.localStorage?.setItem(KEY, username);
  } catch {
    /* preferences are optional; signing in is not */
  }
}
