/**
 * The overlays' focus handling (`Modal.tsx`, shared by `Modal`, `Drawer` and
 * `ConfirmDialog`): focus goes to the first field when one opens and back to
 * where it was when it closes, Escape closes only the top one, and a parent
 * that re-renders — handing a new `onClose` every time, as any inline arrow
 * does — moves nothing. An effect that depended on `onClose` would be torn down
 * and set up again by each of those renders, giving focus back to the opener and
 * then to the first field in the middle of what the person is typing, and ending
 * an input method's composition, in every dialog.
 */
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfirmDialog, Drawer, Modal } from '../src/components/Modal';

afterEach(() => {
  vi.restoreAllMocks();
});

const field = (): HTMLInputElement => screen.getByRole('textbox', { name: 'Field' });

describe('opening and closing', () => {
  /** A page with a button that opens a dialog around one field. */
  function Page() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
        <Modal open={open} onClose={() => setOpen(false)} title="Dialog">
          <input aria-label="Field" />
        </Modal>
      </>
    );
  }

  it('puts focus in the first field, and gives it back where it was when it closes', async () => {
    render(<Page />);
    const opener = screen.getByRole('button', { name: 'Open' });

    await userEvent.click(opener);
    expect(field()).toHaveFocus();
    expect(document.body.style.overflow).toBe('hidden');

    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(document.body.style.overflow).toBe('');
  });
});

describe('a parent that re-renders', () => {
  const dialog = (onClose: () => void) => (
    <Modal open onClose={onClose} title="Dialog">
      <input aria-label="Field" />
    </Modal>
  );

  it('does not move focus out of the field, or the caret in it, however often it does', async () => {
    const { rerender } = render(dialog(() => undefined));
    await userEvent.type(field(), 'abcdef');
    field().setSelectionRange(2, 2);
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');

    // Each one is a new function, as an inline arrow is on every render.
    for (let i = 0; i < 3; i += 1) rerender(dialog(() => undefined));

    expect(focus).not.toHaveBeenCalled();
    expect(field()).toHaveFocus();
    expect(field()).toHaveValue('abcdef');
    expect(field().selectionStart).toBe(2);
    expect(field().selectionEnd).toBe(2);
  });

  it('does the same for a drawer', async () => {
    const drawer = (onClose: () => void) => (
      <Drawer open onClose={onClose} title="Drawer">
        <input aria-label="Field" />
      </Drawer>
    );
    const { rerender } = render(drawer(() => undefined));
    await userEvent.type(field(), 'abc');
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');

    rerender(drawer(() => undefined));

    expect(focus).not.toHaveBeenCalled();
    expect(field()).toHaveFocus();
  });

  it('does the same for a confirmation, which is a modal around two buttons', async () => {
    const confirm = (onClose: () => void) => (
      <ConfirmDialog
        open
        title="Sure?"
        body="Really."
        confirmLabel="Yes"
        onConfirm={() => undefined}
        onClose={onClose}
      />
    );
    const { rerender } = render(confirm(() => undefined));
    const focused = document.activeElement;
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');

    rerender(confirm(() => undefined));

    expect(focus).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(focused);
  });

  it('is closed by Escape through the latest callback it handed over, and not an earlier one', async () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(dialog(first));

    rerender(dialog(second));
    await userEvent.keyboard('{Escape}');

    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });

  it('keeps the focus it was to give back when the dialog closes', async () => {
    function Page({ tick }: { tick: number }) {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Open {tick}
          </button>
          <Modal open={open} onClose={() => setOpen(false)} title="Dialog">
            <input aria-label="Field" />
          </Modal>
        </>
      );
    }
    const { rerender } = render(<Page tick={0} />);
    const opener = screen.getByRole('button', { name: 'Open 0' });
    await userEvent.click(opener);

    rerender(<Page tick={1} />);
    await userEvent.keyboard('{Escape}');

    // Not the first field it moved to, and not the body.
    expect(screen.getByRole('button', { name: 'Open 1' })).toHaveFocus();
  });
});

describe('several overlays', () => {
  it('closes only the top one on Escape, and the next one after it', async () => {
    const outer = vi.fn();
    const inner = vi.fn();
    const { rerender } = render(
      <Modal open onClose={outer} title="Outer">
        <p>Outer body</p>
      </Modal>,
    );
    // Opened later, so on top of the first.
    rerender(
      <>
        <Modal open onClose={outer} title="Outer">
          <p>Outer body</p>
        </Modal>
        <ConfirmDialog
          open
          title="Inner"
          body="Inner body"
          confirmLabel="Yes"
          onConfirm={() => undefined}
          onClose={inner}
        />
      </>,
    );

    await userEvent.keyboard('{Escape}');
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();

    rerender(
      <Modal open onClose={outer} title="Outer">
        <p>Outer body</p>
      </Modal>,
    );
    await userEvent.keyboard('{Escape}');
    expect(outer).toHaveBeenCalledTimes(1);
  });
});
