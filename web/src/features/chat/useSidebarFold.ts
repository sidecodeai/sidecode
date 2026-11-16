/**
 * The chat sidebar folded to its rail and opened again (`docs/DESIGN.md` §
 * "The chat sidebar"): by its toggle, by the rail's Search, and by ⌘. on an
 * Apple system or Ctrl+. elsewhere, which is Claude's shortcut, while the chat
 * page is open. Only a wide window draws the sidebar, so only there does the
 * shortcut do anything; it leaves an input method's composition alone, and so
 * a dialog or a menu that is open, and a held key folds once, not at the
 * key-repeat rate. The choice is the account's, in this browser
 * (`stores/settings.ts`). The width slides only for a fold the person asked
 * for: the stored choice an account's settings bring on load lands at once.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { useSettings } from '../../stores/settings';

/** The width from which the sidebar is drawn (`chat.css`). */
const WIDE = '(min-width: 1024px)';

/** Whether this system writes the command key as ⌘ rather than Ctrl. */
const onApple = (): boolean => /Mac|iPhone|iPad|iPod/.test(globalThis.navigator?.platform ?? '');

/** The shortcut as `aria-keyshortcuts` names it and as the toggle's tooltip prints it. */
export function sidebarShortcut(): { aria: string; label: string } {
  return onApple() ? { aria: 'Meta+.', label: '⌘.' } : { aria: 'Control+.', label: 'Ctrl+.' };
}

/** The platform's command key and a period; Shift may be held where a layout needs it for one. */
export function isSidebarShortcut(e: KeyboardEvent): boolean {
  const command = onApple() ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  return command && !e.altKey && e.key === '.';
}

/** Whether the window draws the sidebar; jsdom, which measures nothing, counts as wide. */
const sidebarDrawn = (): boolean => window.matchMedia?.(WIDE).matches ?? true;

/** A dialog or a menu over the page, whose keys are its own. */
const overlayOpen = (): boolean =>
  document.querySelector('[aria-modal="true"], .popover-panel') !== null;

export function useSidebarFold(): {
  collapsed: boolean;
  /** Whether a fold was asked for since the page opened, and the width may slide. */
  moving: boolean;
  setCollapsed: (collapsed: boolean) => void;
  /** The sidebar, and its toggle, which takes the focus a fold takes away. */
  aside: RefObject<HTMLElement | null>;
  toggle: RefObject<HTMLButtonElement | null>;
} {
  const collapsed = useSettings((s) => s.sidebarCollapsed);
  const [moving, setMoving] = useState(false);
  const aside = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  // What had focus inside the sidebar when the fold was asked for.
  const focused = useRef<Element | null>(null);

  const setCollapsed = useCallback((next: boolean) => {
    const active = document.activeElement;
    focused.current = active !== null && aside.current?.contains(active) === true ? active : null;
    setMoving(true);
    useSettings.getState().setSidebarCollapsed(next);
  }, []);

  // A fold takes away most of what the sidebar holds: the search field, the
  // rows and their More buttons, the group headers, the rail's Search. Focus
  // on any of it goes to the toggle, which both states draw, in the fold's own
  // commit and before the browser paints, rather than falling to the page —
  // whether the fold came from the shortcut or from a click that left focus
  // where it was, as Safari's does. Focus on what both states keep stays.
  useLayoutEffect(() => {
    const had = focused.current;
    focused.current = null;
    if (had !== null && !had.isConnected) toggle.current?.focus({ preventScroll: true });
  }, [collapsed]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (!isSidebarShortcut(e) || e.repeat || e.isComposing || e.keyCode === 229) return;
      if (!sidebarDrawn() || overlayOpen()) return;
      e.preventDefault();
      setCollapsed(!useSettings.getState().sidebarCollapsed);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [setCollapsed]);

  return { collapsed, moving, setCollapsed, aside, toggle };
}
