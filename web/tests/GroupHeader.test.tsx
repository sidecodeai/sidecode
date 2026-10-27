/**
 * A group header is a disclosure button, and while a search or a status is
 * showing its group whatever the user folded it is locked: the click would
 * change nothing on screen and only flip the stored choice, so the header takes
 * none, says so to assistive technology, and gives no hint of the click in its
 * look. `tests/SessionsPage.test.tsx` and `tests/Sidebar.test.tsx` hold the two
 * lists to it; this suite is the component and its stylesheet.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ArchiveGroupHeader, DeviceGroupHeader } from '../src/components/GroupHeader';
import { strings } from '../src/strings';

const css = readFileSync(join(process.cwd(), 'src', 'components', 'ui.css'), 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
);

describe('the device group header', () => {
  const show = (locked?: boolean) => {
    const onToggle = vi.fn();
    render(
      <DeviceGroupHeader name="mac-studio" online expanded onToggle={onToggle} locked={locked} />,
    );
    return { onToggle, header: screen.getByRole('button', { name: /mac-studio/ }) };
  };

  it('toggles on a click, and is not marked inert', async () => {
    const { onToggle, header } = show();

    expect(header).not.toHaveAttribute('aria-disabled');
    await userEvent.click(header);

    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('takes no click while it is locked, and says so', async () => {
    const { onToggle, header } = show(true);

    expect(header).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(header);
    header.focus();
    await userEvent.keyboard('{Enter} ');

    expect(onToggle).not.toHaveBeenCalled();
    // Still the group's disclosure control, open as the list shows it.
    expect(header).toHaveAttribute('aria-expanded', 'true');
  });

  it('is an ordinary header again once it is unlocked', () => {
    const onToggle = vi.fn();
    const { rerender } = render(
      <DeviceGroupHeader name="mac-studio" online expanded onToggle={onToggle} locked />,
    );
    rerender(<DeviceGroupHeader name="mac-studio" online expanded onToggle={onToggle} />);

    expect(screen.getByRole('button', { name: /mac-studio/ })).not.toHaveAttribute('aria-disabled');
  });
});

describe('the Archive group header', () => {
  const show = (locked?: boolean) => {
    const onToggle = vi.fn();
    render(<ArchiveGroupHeader count={3} expanded onToggle={onToggle} locked={locked} />);
    return { onToggle, header: screen.getByRole('button', { name: strings.sessions.archiveGroup(3) }) };
  };

  it('toggles on a click, and is not marked inert', async () => {
    const { onToggle, header } = show();

    expect(header).not.toHaveAttribute('aria-disabled');
    await userEvent.click(header);

    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('takes no click while it is locked, and says so', async () => {
    const { onToggle, header } = show(true);

    expect(header).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(header);

    expect(onToggle).not.toHaveBeenCalled();
    expect(header).toHaveAttribute('aria-expanded', 'true');
  });
});

describe('the group header stylesheet', () => {
  it('draws a locked header without the pointer', () => {
    expect(css).toMatch(/\.group-head\[aria-disabled='true'\]\s*\{[^}]*cursor: default;/);
  });

  it('gives the hover ink to a header that can be clicked, and to no other', () => {
    expect(css).toMatch(
      /\.group-head:not\(\[aria-disabled='true'\]\):hover\s*\{[^}]*color: var\(--ink-secondary\);/,
    );
    expect(css).not.toMatch(/\.group-head:hover\s*\{/);
  });

  it('changes nothing else about a locked header: no dimming', () => {
    const locked = /\.group-head\[aria-disabled='true'\]\s*\{([^}]*)\}/.exec(css)?.[1] ?? '';

    expect(locked).not.toMatch(/opacity|color|filter/);
  });
});
