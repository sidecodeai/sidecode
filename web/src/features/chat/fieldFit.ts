/**
 * The composer's field grows with what it shows (`docs/DESIGN.md` § "The
 * composer"): its words, or while it has none its placeholder, up to eight
 * lines, past which it scrolls inside. A placeholder that wraps — "Message will
 * be queued…" beside four controls on a phone — is read whole, never cut to its
 * first line.
 */
import { useEffect, type RefObject } from 'react';

/** About eight lines: past this the field scrolls inside. */
export const FIELD_MAX_PX = 220;

/**
 * How tall the field's placeholder is at the field's width. Browsers disagree
 * on whether a placeholder counts in `scrollHeight`, so it is measured as the
 * words of a twin field — same class, same place, same width, no scroll bar —
 * that exists only for the measurement.
 */
function placeholderHeight(field: HTMLTextAreaElement): number {
  const twin = document.createElement('textarea');
  twin.className = field.className;
  twin.rows = field.rows;
  twin.value = field.placeholder;
  twin.tabIndex = -1;
  twin.setAttribute('aria-hidden', 'true');
  Object.assign(twin.style, {
    position: 'absolute',
    visibility: 'hidden',
    overflow: 'hidden',
    height: 'auto',
    width: getComputedStyle(field).width,
  });
  field.after(twin);
  const height = twin.scrollHeight;
  twin.remove();
  return height;
}

/** Sizes the field to its words, or with none to its placeholder. */
export function fitField(field: HTMLTextAreaElement): void {
  if (field.value === '' && field.placeholder !== '') {
    field.style.height = `${Math.min(placeholderHeight(field), FIELD_MAX_PX)}px`;
    return;
  }
  field.style.height = 'auto';
  field.style.height = `${Math.min(field.scrollHeight, FIELD_MAX_PX)}px`;
}

/**
 * Fits the field again when its width changes: the window, the controls beside
 * it, a dictation taking the row. The fit waits for the next frame, because
 * resizing the field inside the observer's own callback is a resize loop.
 */
export function useRefitOnResize(field: RefObject<HTMLTextAreaElement | null>): void {
  useEffect(() => {
    const el = field.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    let width = getComputedStyle(el).width;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      const next = getComputedStyle(el).width;
      if (next === width) return;
      width = next;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => fitField(el));
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [field]);
}
