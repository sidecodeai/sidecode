/**
 * What opens a context menu: a right-click at the pointer, the ContextMenu key
 * or Shift+F10 under the focused row, or a button of the row's own — the chat
 * sidebar's More — under that button. The panel itself is `ContextMenu`.
 */
import {
  useCallback,
  useState,
  type HTMLAttributes,
  type KeyboardEvent,
  type MouseEvent,
} from 'react';
import type { Point } from './popoverPlacement';

/** How far in from a row's leading edge a menu opened by the keyboard sits. */
const KEY_INSET = 24;

/** How far under a button the menu it opens sits. */
const BUTTON_GAP = 4;

/** What an owner spreads on the element the menu belongs to. */
type Trigger = Pick<HTMLAttributes<HTMLElement>, 'onContextMenu' | 'onKeyDown'>;

/** Where an open menu is, and the row it was opened on. */
interface Opened {
  at: Point;
  anchor: HTMLElement;
}

/**
 * The state behind one owner's menu. `enabled` false leaves the browser's own
 * menu alone, and takes an open one away; the handlers are the owner's to
 * spread on the row, which is where the menu's portal also sits in the React
 * tree. `anchor` is that row, or the button `openFrom` opened it from: the menu
 * closes when something that holds it scrolls.
 */
export function useContextMenu(enabled: boolean): {
  at: Point | null;
  anchor: HTMLElement | null;
  close: () => void;
  openFrom: (button: HTMLElement) => void;
  triggerProps: Trigger;
} {
  const [opened, setOpened] = useState<Opened | null>(null);
  // An owner that stops offering the menu while it is open takes it away, and
  // it does not come back by itself when the owner offers it again.
  if (!enabled && opened !== null) setOpened(null);

  const close = useCallback(() => setOpened(null), []);

  // A button opens the menu under itself, from its leading edge.
  const openFrom = useCallback((button: HTMLElement) => {
    const box = button.getBoundingClientRect();
    setOpened({ at: { x: box.left, y: box.bottom + BUTTON_GAP }, anchor: button });
  }, []);

  if (!enabled) return { at: null, anchor: null, close, openFrom, triggerProps: {} };

  const onContextMenu = (e: MouseEvent<HTMLElement>): void => {
    e.preventDefault();
    setOpened({ at: { x: e.clientX, y: e.clientY }, anchor: e.currentTarget });
  };

  // The menu key and Shift+F10 name the focused row, not a pointer: the menu
  // opens under it. The panel is a portal, whose keys also bubble through this
  // handler in the React tree, so only a key from inside the row counts.
  const onKeyDown = (e: KeyboardEvent<HTMLElement>): void => {
    const asked = e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey);
    if (!asked || !(e.target instanceof Node) || !e.currentTarget.contains(e.target)) return;
    e.preventDefault();
    const row = e.currentTarget.getBoundingClientRect();
    setOpened({ at: { x: row.left + KEY_INSET, y: row.bottom }, anchor: e.currentTarget });
  };

  return {
    at: opened?.at ?? null,
    anchor: opened?.anchor ?? null,
    close,
    openFrom,
    triggerProps: { onContextMenu, onKeyDown },
  };
}
