/**
 * A short label beside an icon button (`TooltipButton`), drawn on the body so
 * no pane clips it: after the mouse pointer rests on the button, at once when
 * the keyboard brings focus to it, never for a touch, and gone when the pointer
 * leaves, on a press, on blur, on Escape and on a scroll that moves the button.
 * A pane beside the button that scrolls on its own — a transcript following a
 * streaming turn — leaves it be. It says what the button's `aria-label` already
 * says, so it is hidden from assistive technology, which would otherwise hear
 * the name twice; a shortcut it shows is the button's `aria-keyshortcuts`.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FocusEvent,
  type PointerEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { placementBeside, type TooltipSide } from './popoverPlacement';
import './tooltip.css';

/** How long the pointer rests before the label shows: long enough not to flash as it passes. */
export const TOOLTIP_REST_MS = 450;

export interface TooltipHandlers {
  onPointerEnter: (e: PointerEvent<HTMLElement>) => void;
  onPointerLeave: () => void;
  onPointerDown: () => void;
  onFocus: (e: FocusEvent<HTMLElement>) => void;
  onBlur: () => void;
}

export function useTooltip(
  text: string,
  side: TooltipSide,
  shortcut?: string,
): { triggerProps: TooltipHandlers; tooltip: ReactNode } {
  // The button the pointer rests on, and the one the label is shown against.
  const [resting, setResting] = useState<HTMLElement | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const tip = useRef<HTMLDivElement>(null);

  const hide = useCallback(() => {
    setResting(null);
    setAnchor(null);
  }, []);

  useEffect(() => {
    if (!resting) return;
    const timer = window.setTimeout(() => setAnchor(resting), TOOLTIP_REST_MS);
    return () => window.clearTimeout(timer);
  }, [resting]);

  // Placed before the browser paints, and again when the words change.
  useLayoutEffect(() => {
    const el = tip.current;
    if (!anchor || !el) return;
    const at = placementBeside(
      anchor.getBoundingClientRect(),
      el.getBoundingClientRect(),
      { width: window.innerWidth, height: window.innerHeight },
      side,
    );
    el.style.left = `${at.left}px`;
    el.style.top = `${at.top}px`;
  }, [anchor, side, text, shortcut]);

  useEffect(() => {
    if (!anchor) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') hide();
    };
    // The window's own scroll, the page's (the document's) and that of
    // anything holding the button move it; any other pane's scroll is its own.
    const onScroll = (e: Event): void => {
      const target = e.target;
      if (target === e.currentTarget || (target instanceof Node && target.contains(anchor))) hide();
    };
    document.addEventListener('keydown', onKey);
    // Capture, since a scroll does not bubble.
    window.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [anchor, hide]);

  const triggerProps: TooltipHandlers = {
    onPointerEnter: (e) => {
      if (e.pointerType === 'mouse') setResting(e.currentTarget);
    },
    onPointerLeave: hide,
    onPointerDown: hide,
    onFocus: (e) => {
      if (focusVisible(e.currentTarget)) setAnchor(e.currentTarget);
    },
    onBlur: hide,
  };

  const tooltip = anchor
    ? createPortal(
        <div ref={tip} className="tooltip" aria-hidden>
          <span>{text}</span>
          {shortcut ? <kbd className="tooltip-key">{shortcut}</kbd> : null}
        </div>,
        document.body,
      )
    : null;

  return { triggerProps, tooltip };
}

/** Whether focus came from the keyboard; a browser without the selector shows it on every focus. */
function focusVisible(el: HTMLElement): boolean {
  try {
    return el.matches(':focus-visible');
  } catch {
    return true;
  }
}
