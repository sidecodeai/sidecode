/**
 * What a menu owes the keyboard, for every panel of `role="menu"` the app
 * draws (`ContextMenu`, the account menu): it opens with focus on its first
 * item, the arrow keys move between the enabled items and round the ends, and
 * Home and End go to the first and the last. Leaving it — Escape, Tab — is the
 * owner's, which knows where focus goes back to.
 */

/** A menu's items that can take focus, links included. */
const MENU_ITEM = '[role="menuitem"]:not(:disabled)';

/** Puts focus on a menu's first item. */
export function focusFirstItem(panel: HTMLElement | null): void {
  panel?.querySelector<HTMLElement>(MENU_ITEM)?.focus({ preventScroll: true });
}

/** Moves focus for an arrow key, Home or End, and says whether the key was one of those. */
export function moveMenuFocus(
  e: { key: string; preventDefault: () => void },
  panel: HTMLElement | null,
): boolean {
  const items = [...(panel?.querySelectorAll<HTMLElement>(MENU_ITEM) ?? [])];
  if (items.length === 0) return false;
  const current = items.findIndex((item) => item === document.activeElement);
  let next: number;
  switch (e.key) {
    case 'ArrowDown':
      next = current < 0 ? 0 : (current + 1) % items.length;
      break;
    case 'ArrowUp':
      next = current <= 0 ? items.length - 1 : current - 1;
      break;
    case 'Home':
      next = 0;
      break;
    case 'End':
      next = items.length - 1;
      break;
    default:
      return false;
  }
  e.preventDefault();
  items[next]?.focus();
  return true;
}
