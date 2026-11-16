/**
 * The tooltip an icon button carries (`TooltipButton`, `useTooltip`): after the
 * mouse pointer rests on the button, at once on keyboard focus, never for a
 * touch; gone when the pointer leaves, on a press, on blur, on Escape and on a
 * scroll; on the body, beside the button and inside the window; and hidden
 * from assistive technology, since the button's `aria-label` already says it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TooltipButton } from '../src/components/TooltipButton';
import { placementBeside } from '../src/components/popoverPlacement';
import { TOOLTIP_REST_MS } from '../src/components/useTooltip';

const tip = (): Element | null => document.querySelector('.tooltip');

function show(): HTMLElement {
  render(
    <>
      <TooltipButton label="Close sidebar" shortcut="Ctrl+." onClick={vi.fn()}>
        <svg aria-hidden />
      </TooltipButton>
      <button type="button">next</button>
    </>,
  );
  return screen.getByRole('button', { name: 'Close sidebar' });
}

const box = (left: number, top: number, width: number, height: number): DOMRect =>
  ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top }) as DOMRect;

describe('where it goes', () => {
  const viewport = { width: 1280, height: 800 };

  it('sits to the right of the button, centred on it', () => {
    expect(placementBeside(box(8, 12, 36, 36), { width: 100, height: 26 }, viewport, 'right')).toEqual({
      left: 50,
      top: 17,
    });
  });

  it('sits under the button, centred, and slides back inside the window at either edge', () => {
    const tip = { width: 120, height: 26 };
    expect(placementBeside(box(400, 12, 36, 36), tip, viewport, 'bottom')).toEqual({ left: 358, top: 54 });
    expect(placementBeside(box(8, 12, 36, 36), tip, viewport, 'bottom')).toEqual({ left: 8, top: 54 });
    expect(placementBeside(box(1240, 12, 36, 36), tip, viewport, 'bottom')).toEqual({ left: 1152, top: 54 });
  });
});

describe('when it shows', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const rest = (button: HTMLElement, pointerType = 'mouse'): void => {
    fireEvent.pointerEnter(button, { pointerType });
    act(() => {
      vi.advanceTimersByTime(TOOLTIP_REST_MS);
    });
  };

  it('shows once the mouse pointer rests, with the shortcut, hidden from assistive technology', () => {
    const button = show();
    fireEvent.pointerEnter(button, { pointerType: 'mouse' });
    act(() => {
      vi.advanceTimersByTime(TOOLTIP_REST_MS - 1);
    });
    expect(tip()).toBeNull();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(tip()).toHaveTextContent('Close sidebarCtrl+.');
    expect(tip()).toHaveAttribute('aria-hidden', 'true');
    expect(tip()?.parentElement).toBe(document.body);
    // The name is the label's alone, and nothing describes the button twice.
    expect(button).toHaveAccessibleName('Close sidebar');
    expect(button).not.toHaveAttribute('aria-describedby');
  });

  it('does not show for a pointer that passes without resting', () => {
    const button = show();
    fireEvent.pointerEnter(button, { pointerType: 'mouse' });
    act(() => {
      vi.advanceTimersByTime(TOOLTIP_REST_MS / 2);
    });
    fireEvent.pointerLeave(button, { pointerType: 'mouse' });
    act(() => {
      vi.advanceTimersByTime(TOOLTIP_REST_MS * 2);
    });

    expect(tip()).toBeNull();
  });

  it('never shows for a touch', () => {
    const button = show();
    rest(button, 'touch');

    expect(tip()).toBeNull();
  });

  it('goes when the pointer leaves, on a press, on Escape and on a scroll of the window or the page', () => {
    const button = show();
    const closers: (() => void)[] = [
      () => fireEvent.pointerLeave(button, { pointerType: 'mouse' }),
      () => fireEvent.pointerDown(button, { pointerType: 'mouse' }),
      () => fireEvent.keyDown(document, { key: 'Escape' }),
      () => fireEvent.scroll(window),
      () => fireEvent.scroll(document),
    ];
    for (const close of closers) {
      rest(button);
      expect(tip()).not.toBeNull();
      close();
      expect(tip()).toBeNull();
    }
  });

  /**
   * A transcript following a streaming turn scrolls on every delta beside the
   * sidebar; the label on the sidebar's toggle stays while it does. A scroll of
   * what holds the button has moved the button, and takes the label.
   */
  it('stays while a pane beside the button scrolls, and goes when what holds it scrolls', () => {
    render(
      <>
        <div data-testid="head">
          <TooltipButton label="New session" onClick={vi.fn()}>
            <svg aria-hidden />
          </TooltipButton>
        </div>
        <div data-testid="transcript" />
      </>,
    );
    const button = screen.getByRole('button', { name: 'New session' });
    rest(button);

    for (let i = 0; i < 5; i += 1) fireEvent.scroll(screen.getByTestId('transcript'));
    expect(tip()).toHaveTextContent('New session');

    fireEvent.scroll(screen.getByTestId('head'));
    expect(tip()).toBeNull();
  });
});

describe('from the keyboard', () => {
  it('shows at once when Tab brings focus to the button, and goes when focus leaves', async () => {
    const button = show();

    await userEvent.tab();
    expect(button).toHaveFocus();
    expect(tip()).toHaveTextContent('Close sidebar');

    await userEvent.tab();
    expect(tip()).toBeNull();
  });
});
