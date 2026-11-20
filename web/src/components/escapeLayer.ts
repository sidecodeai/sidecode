import { useEffect, useRef } from 'react';

/**
 * Open overlays, innermost last: modals, drawers and the popovers opened over
 * them. Escape closes only the top one, so a list open in a drawer goes before
 * the drawer does. Every handler sits on `document`, where stopPropagation
 * cannot reorder them, so each asks whether it is the top one.
 */
const stack: symbol[] = [];

/**
 * While `open`, Escape calls `onEscape` unless something opened since is still
 * open. The latest callback is kept in a ref, so a parent that hands a new one
 * on every render, as an inline arrow does, re-arms nothing.
 */
export function useEscapeLayer(open: boolean, onEscape: () => void): void {
  const latest = useRef(onEscape);
  useEffect(() => {
    latest.current = onEscape;
  });

  useEffect(() => {
    if (!open) return;
    const token = Symbol('overlay');
    stack.push(token);
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || stack.at(-1) !== token) return;
      e.stopPropagation();
      latest.current();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      const index = stack.lastIndexOf(token);
      if (index >= 0) stack.splice(index, 1);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
}
