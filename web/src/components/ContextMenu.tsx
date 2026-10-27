/**
 * The panel of a context menu, drawn like every other menu (`popover.css`) and
 * keyboard-complete as a menu owes it: the first item has focus, the arrow keys
 * and Home and End move between items, Enter or Space takes one, and Escape, an
 * outside click, Tab, a resize or a scroll of the page or of anything that holds
 * the row closes it. A pane beside the row that scrolls on its own, a transcript
 * following its tail, leaves the menu where it is. A menu closed from the
 * keyboard hands focus back to where it was. `useContextMenu` is what opens it.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { placementAt, type Point } from './popoverPlacement';
import './popover.css';

export interface ContextMenuItem {
  id: string;
  label: string;
  /**
   * Runs once the menu is closed and focus is back on the row, so a dialog it
   * opens finds it there.
   */
  onSelect: () => void;
}

interface ContextMenuProps {
  /** The corner the menu opens at, in viewport coordinates. */
  at: Point;
  /** The row the menu was opened on: what it is anchored to while it is open. */
  anchor: HTMLElement;
  /** What the menu is of: its accessible name. */
  label: string;
  items: ContextMenuItem[];
  onClose: () => void;
}

export function ContextMenu({ at, anchor, label, items, onClose }: ContextMenuProps) {
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  // The panel is a portal on the body, so it is placed against the viewport
  // before the browser paints, the way a popover is.
  useLayoutEffect(() => {
    const el = panel.current;
    if (!el) return;
    const place = placementAt(at, el.getBoundingClientRect(), {
      width: window.innerWidth,
      height: window.innerHeight,
    });
    el.style.left = `${place.left}px`;
    el.style.top = place.top === null ? '' : `${place.top}px`;
    el.style.bottom = place.bottom === null ? '' : `${place.bottom}px`;
  }, [at]);

  // Where focus was is kept once: a development double run of this effect
  // would otherwise take the menu's own first item for it.
  useEffect(() => {
    opener.current ??=
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus({ preventScroll: true });
  }, []);

  const dismiss = useCallback(
    (returnFocus: boolean) => {
      onClose();
      if (returnFocus) opener.current?.focus({ preventScroll: true });
    },
    [onClose],
  );

  useEffect(() => {
    const inside = (target: EventTarget | null): boolean =>
      target instanceof Node && panel.current?.contains(target) === true;
    const onDown = (e: globalThis.MouseEvent): void => {
      if (!inside(e.target)) onClose();
    };
    const onKey = (e: globalThis.KeyboardEvent): void => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      dismiss(true);
    };
    // The row has moved when the page scrolls or anything that holds it does,
    // and the menu with it goes. Any other scroll is another pane's own.
    const onScroll = (e: Event): void => {
      const target = e.target;
      // The window itself is the target of a scroll dispatched at it; the page's
      // own scroll is the document's, which holds the row like any ancestor.
      if (target === e.currentTarget || (target instanceof Node && target.contains(anchor))) {
        onClose();
      }
    };
    const onAway = (): void => onClose();
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    // Capture, since a scroll does not bubble.
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onAway);
    window.addEventListener('blur', onAway);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onAway);
      window.removeEventListener('blur', onAway);
    };
  }, [anchor, onClose, dismiss]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    const buttons = [
      ...(panel.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)') ?? []),
    ];
    if (buttons.length === 0) return;
    const current = buttons.findIndex((button) => button === document.activeElement);
    let next: number;
    switch (e.key) {
      case 'ArrowDown':
        next = current < 0 ? 0 : (current + 1) % buttons.length;
        break;
      case 'ArrowUp':
        next = current <= 0 ? buttons.length - 1 : current - 1;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = buttons.length - 1;
        break;
      case 'Tab':
        // A menu is not a tab stop: leaving it closes it, where focus was.
        e.preventDefault();
        dismiss(true);
        return;
      default:
        return;
    }
    e.preventDefault();
    buttons[next]?.focus();
  };

  return createPortal(
    <div
      className="popover-panel"
      ref={panel}
      onKeyDown={onKeyDown}
      // The portal's events bubble through the row in the React tree, where a
      // right-click would open the menu again; the browser's own menu has no
      // business over ours either.
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
    >
      <ul className="menu" role="menu" aria-label={label}>
        {items.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              role="menuitem"
              className="menu-item"
              onClick={() => {
                dismiss(true);
                item.onSelect();
              }}
            >
              <span className="menu-label">{item.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>,
    document.body,
  );
}
