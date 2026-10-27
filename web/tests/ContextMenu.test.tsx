/**
 * The context menu a row owns (A55's Rename on session rows and sidebar
 * items): opened by a right-click at the pointer, or by the ContextMenu key or
 * Shift+F10 under the focused row, drawn as a menu with the keyboard a menu
 * owes it, and closed by Escape, an outside click, Tab, a resize, or a scroll of
 * the page or of anything that holds the row. A pane beside the row that scrolls
 * on its own — a transcript following its tail — leaves it open.
 */
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ContextMenu } from '../src/components/ContextMenu';
import { useContextMenu } from '../src/components/useContextMenu';

/**
 * One row that owns a menu of two items, in a list, beside a pane that scrolls
 * on its own and something else that does not.
 */
function Harness({
  enabled = true,
  onSelect,
  onOther = () => undefined,
}: {
  enabled?: boolean;
  onSelect: () => void;
  onOther?: () => void;
}) {
  const menu = useContextMenu(enabled);
  return (
    <div>
      <ul data-testid="list">
        <li data-testid="row" {...menu.triggerProps}>
          <button type="button">Open</button>
          {menu.at && menu.anchor ? (
            <ContextMenu
              at={menu.at}
              anchor={menu.anchor}
              label="Fix flaky auth test"
              items={[
                { id: 'rename', label: 'Rename', onSelect },
                { id: 'other', label: 'Other', onSelect: onOther },
              ]}
              onClose={menu.close}
            />
          ) : null}
        </li>
      </ul>
      <div data-testid="transcript" />
      <button type="button">Elsewhere</button>
    </div>
  );
}

const row = (): HTMLElement => screen.getByTestId('row');
const menu = (): HTMLElement => screen.getByRole('menu', { name: 'Fix flaky auth test' });
const panel = (): HTMLElement => menu().closest('.popover-panel') as HTMLElement;
const items = (): string[] => screen.getAllByRole('menuitem').map((item) => item.textContent ?? '');

const rightClick = (x = 120, y = 80): boolean =>
  fireEvent.contextMenu(row(), { clientX: x, clientY: y });

describe('opening by pointer', () => {
  it('opens at the pointer, as a menu named for the row, on the body', () => {
    render(<Harness onSelect={vi.fn()} />);

    rightClick(120, 80);

    expect(items()).toEqual(['Rename', 'Other']);
    expect(panel().parentElement).toBe(document.body);
    expect(panel().style.left).toBe('120px');
    expect(panel().style.top).toBe('80px');
  });

  it('keeps the browser’s own menu away', () => {
    render(<Harness onSelect={vi.fn()} />);

    expect(rightClick()).toBe(false);
  });

  it('opens where a second right-click lands', () => {
    render(<Harness onSelect={vi.fn()} />);

    rightClick(120, 80);
    rightClick(300, 200);

    expect(screen.getAllByRole('menu')).toHaveLength(1);
    expect(panel().style.left).toBe('300px');
    expect(panel().style.top).toBe('200px');
  });

  it('is not there while the owner offers nothing, and leaves the browser’s menu alone', () => {
    render(<Harness enabled={false} onSelect={vi.fn()} />);

    expect(rightClick()).toBe(true);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('is taken away when the owner stops offering it, and does not return by itself', () => {
    const { rerender } = render(<Harness onSelect={vi.fn()} />);
    rightClick();
    expect(screen.getByRole('menu')).toBeInTheDocument();

    rerender(<Harness enabled={false} onSelect={vi.fn()} />);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    rerender(<Harness onSelect={vi.fn()} />);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('does not take a right-click inside the menu for another opening', () => {
    render(<Harness onSelect={vi.fn()} />);
    rightClick(120, 80);

    const proceed = fireEvent.contextMenu(screen.getByRole('menuitem', { name: 'Rename' }), {
      clientX: 500,
      clientY: 400,
    });

    expect(proceed).toBe(false);
    expect(screen.getAllByRole('menu')).toHaveLength(1);
    expect(panel().style.left).toBe('120px');
  });
});

describe('opening by keyboard', () => {
  it('opens on the ContextMenu key, under the focused row', async () => {
    render(<Harness onSelect={vi.fn()} />);
    const open = screen.getByRole('button', { name: 'Open' });
    vi.spyOn(row(), 'getBoundingClientRect').mockReturnValue({
      left: 100,
      right: 600,
      top: 150,
      bottom: 234,
      width: 500,
      height: 84,
      x: 100,
      y: 150,
      toJSON: () => ({}),
    });
    open.focus();

    await userEvent.keyboard('{ContextMenu}');

    expect(items()).toEqual(['Rename', 'Other']);
    expect(panel().style.left).toBe('124px');
    expect(panel().style.top).toBe('234px');
  });

  it('opens on Shift+F10, and not on F10 alone', async () => {
    render(<Harness onSelect={vi.fn()} />);
    screen.getByRole('button', { name: 'Open' }).focus();

    await userEvent.keyboard('{F10}');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    await userEvent.keyboard('{Shift>}{F10}{/Shift}');
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('does not take a key that came from inside the menu for another opening', async () => {
    render(<Harness onSelect={vi.fn()} />);
    screen.getByRole('button', { name: 'Open' }).focus();
    await userEvent.keyboard('{ContextMenu}');

    // The panel is a portal: its keys bubble through the row in the React tree.
    const proceed = fireEvent.keyDown(screen.getByRole('menuitem', { name: 'Rename' }), {
      key: 'ContextMenu',
    });

    expect(proceed).toBe(true);
    expect(screen.getAllByRole('menu')).toHaveLength(1);
  });
});

describe('the menu’s keyboard', () => {
  const opened = async () => {
    render(<Harness onSelect={vi.fn()} />);
    screen.getByRole('button', { name: 'Open' }).focus();
    await userEvent.keyboard('{ContextMenu}');
  };

  it('puts focus on the first item', async () => {
    await opened();

    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
  });

  it('moves between items with the arrow keys, round the ends, and with Home and End', async () => {
    await opened();

    await userEvent.keyboard('{ArrowDown}');
    expect(screen.getByRole('menuitem', { name: 'Other' })).toHaveFocus();
    await userEvent.keyboard('{ArrowDown}');
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
    await userEvent.keyboard('{ArrowUp}');
    expect(screen.getByRole('menuitem', { name: 'Other' })).toHaveFocus();
    await userEvent.keyboard('{Home}');
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
    await userEvent.keyboard('{End}');
    expect(screen.getByRole('menuitem', { name: 'Other' })).toHaveFocus();
  });

  it('takes the focused item on Enter, closed and with focus back on the row', async () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    const open = screen.getByRole('button', { name: 'Open' });
    open.focus();
    await userEvent.keyboard('{ContextMenu}');

    await userEvent.keyboard('{Enter}');

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(open).toHaveFocus();
  });

  it('takes an item with a click, and runs only that one', async () => {
    const onSelect = vi.fn();
    const onOther = vi.fn();
    render(<Harness onSelect={onSelect} onOther={onOther} />);
    rightClick();

    await userEvent.click(screen.getByRole('menuitem', { name: 'Other' }));

    expect(onOther).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes on Escape, with focus back where it was', async () => {
    render(<Harness onSelect={vi.fn()} />);
    const open = screen.getByRole('button', { name: 'Open' });
    open.focus();
    await userEvent.keyboard('{ContextMenu}');

    await userEvent.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(open).toHaveFocus();
  });

  it('closes on Tab rather than being a tab stop, with focus back where it was', async () => {
    render(<Harness onSelect={vi.fn()} />);
    const open = screen.getByRole('button', { name: 'Open' });
    open.focus();
    await userEvent.keyboard('{ContextMenu}');

    await userEvent.keyboard('{Tab}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(open).toHaveFocus();
  });
});

describe('closing', () => {
  it('closes on a press outside, and not on one inside', async () => {
    render(<Harness onSelect={vi.fn()} />);
    rightClick();

    fireEvent.mouseDown(screen.getByRole('menuitem', { name: 'Rename' }));
    expect(screen.getByRole('menu')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Elsewhere' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes when the page scrolls', () => {
    render(<Harness onSelect={vi.fn()} />);

    rightClick();
    fireEvent.scroll(document);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    rightClick();
    act(() => {
      window.dispatchEvent(new Event('scroll'));
    });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes when whatever holds the row scrolls, which moves the row from under it', () => {
    render(<Harness onSelect={vi.fn()} />);
    rightClick();

    fireEvent.scroll(screen.getByTestId('list'));

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes on the keyboard-opened menu too when the row’s list scrolls', async () => {
    render(<Harness onSelect={vi.fn()} />);
    screen.getByRole('button', { name: 'Open' }).focus();
    await userEvent.keyboard('{ContextMenu}');

    fireEvent.scroll(screen.getByTestId('list'));

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('stays open while a pane beside the row scrolls, as a transcript does following its tail', () => {
    render(<Harness onSelect={vi.fn()} />);
    rightClick();

    // Every flush of a streaming turn scrolls the transcript to its end.
    for (let i = 0; i < 5; i += 1) fireEvent.scroll(screen.getByTestId('transcript'));

    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('stays open while the menu itself scrolls', () => {
    render(<Harness onSelect={vi.fn()} />);
    rightClick();

    fireEvent.scroll(menu());

    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('closes when the window is resized or loses focus', () => {
    render(<Harness onSelect={vi.fn()} />);

    rightClick();
    act(() => {
      window.dispatchEvent(new Event('resize'));
    });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    rightClick();
    act(() => {
      window.dispatchEvent(new Event('blur'));
    });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
