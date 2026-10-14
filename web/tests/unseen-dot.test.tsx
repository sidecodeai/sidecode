/**
 * A47 (`docs/DESIGN.md` § "A red dot for a session that stopped and waits for
 * you"): a session whose `unseen` is true carries a red dot on every row that
 * draws it — the Sessions page and the chat sidebar — in the row's leading
 * gutter on the title's line, out of the flow so it moves nothing, and a screen
 * reader hears "not yet opened". The conversation in front of the person draws
 * none: it is being looked at, and its `session.seen` is on the way.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { SessionsPage } from '../src/features/sessions/SessionsPage';
import { Sidebar } from '../src/features/chat/Sidebar';
import { useDevices } from '../src/stores/devices';
import { useFront } from '../src/stores/front';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { stringTables, strings } from '../src/strings';
import { devices, sessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

const MARKED = 'Migrate web to Vite 6';
const QUIET = 'Fix flaky auth test';

beforeEach(() => {
  useDevices.setState({ devices, loaded: true, error: null });
  useSessions.setState({ sessions: index(sessions), loaded: true, agentFilter: null });
  useSettings.setState({ collapsedDevices: [], archiveExpanded: [], language: 'en' });
  useFront.setState({ key: null });
});

afterEach(() => {
  useSettings.setState({ language: 'en' });
  useFront.setState({ key: null });
});

const renderSessions = () =>
  render(
    <MemoryRouter>
      <SessionsPage />
    </MemoryRouter>,
  );

/** The sidebar of the chat page, with that page's conversation in front or not. */
const renderSidebar = (activeKey = '', inFront = false) => {
  useFront.setState({ key: inFront ? activeKey : null });
  return render(
    <MemoryRouter>
      <Sidebar activeKey={activeKey} onNewSession={vi.fn()} />
    </MemoryRouter>,
  );
};

/** The control a row opens its session with, by the title it prints. */
function rowButton(title: string): HTMLElement {
  const button = screen.getByText(title).closest('button');
  if (!button) throw new Error(`no row for ${title}`);
  return button;
}

const dotIn = (title: string): Element | null => rowButton(title).querySelector('.unseen-dot');

/** Puts the mark on, or takes it off, the way a `session.updated` does. */
function mark(title: string, unseen: boolean): void {
  const found = Object.values(useSessions.getState().sessions).find((s) => s.title === title);
  if (!found) throw new Error(`no session ${title}`);
  const { unseen: _drop, ...rest } = found;
  act(() => useSessions.getState().upsert(unseen ? { ...rest, unseen: true } : rest));
}

const css = (path: string[]): string =>
  readFileSync(join(process.cwd(), 'src', ...path), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** The declarations of the first rule whose selector is exactly `selector`. */
function rule(source: string, selector: string): string {
  const start = source.indexOf(`${selector} {`);
  expect(start, `${selector} is missing`).toBeGreaterThan(-1);
  return source.slice(source.indexOf('{', start) + 1, source.indexOf('}', start));
}

describe('the red dot on the Sessions page', () => {
  it('is drawn on the row of the session the gateway marked, and on no other', () => {
    renderSessions();

    expect(dotIn(MARKED)).not.toBeNull();
    expect(dotIn(QUIET)).toBeNull();
    expect(document.querySelectorAll('.unseen-dot')).toHaveLength(1);
  });

  it("sits on the title's line, before the title", () => {
    renderSessions();
    const dot = dotIn(MARKED);
    const line = dot?.parentElement;

    expect(line?.classList.contains('session-line')).toBe(true);
    expect(line?.querySelector('.session-title')?.textContent).toBe(MARKED);
    expect(line).toBe(rowButton(MARKED).querySelector('.session-line'));
  });

  it('is heard as "not yet opened" after the row’s own name', () => {
    renderSessions();
    const row = rowButton(MARKED);

    expect(row).toHaveAccessibleName(strings.sessions.open);
    expect(row).toHaveAccessibleDescription('not yet opened');
    expect(dotIn(MARKED)?.getAttribute('aria-hidden')).toBe('true');
    expect(rowButton(QUIET)).not.toHaveAttribute('aria-describedby');
  });

  it('comes with a session.updated and goes with the next one', () => {
    renderSessions();

    mark(QUIET, true);
    expect(dotIn(QUIET)).not.toBeNull();
    expect(rowButton(QUIET)).toHaveAccessibleDescription('not yet opened');

    mark(QUIET, false);
    expect(dotIn(QUIET)).toBeNull();
    expect(rowButton(QUIET)).not.toHaveAttribute('aria-describedby');
  });

  it('leaves the title and the time where they were', () => {
    renderSessions();
    const lineOf = (title: string) =>
      [...(rowButton(title).querySelector('.session-line')?.children ?? [])]
        .filter((child) => !child.classList.contains('unseen-dot') && !child.hasAttribute('hidden'))
        .map((child) => child.className);

    const before = lineOf(QUIET);
    mark(QUIET, true);
    expect(lineOf(QUIET)).toEqual(before);
    expect(before).toEqual(['session-title', 'session-time']);
  });

  it('is said in Chinese when the app is', () => {
    useSettings.setState({ language: 'zh-Hans' });
    renderSessions();

    expect(rowButton(MARKED)).toHaveAccessibleDescription(
      stringTables['zh-Hans'].sessions.unseen,
    );
    expect(stringTables['zh-Hans'].sessions.unseen).toBe('未查看');
  });
});

describe('the red dot in the chat sidebar', () => {
  it('is drawn on the marked row, on its title line', () => {
    renderSidebar();
    const dot = dotIn(MARKED);

    expect(dot?.parentElement?.classList.contains('sidebar-title-line')).toBe(true);
    expect(dot?.parentElement?.querySelector('.sidebar-title')?.textContent).toBe(MARKED);
    expect(dotIn(QUIET)).toBeNull();
  });

  it('is heard as "not yet opened", apart from the row’s name', () => {
    renderSidebar();
    const row = rowButton(MARKED);

    expect(row).toHaveAccessibleDescription('not yet opened');
    expect(row).not.toHaveAccessibleName(/not yet opened/);
  });

  it('is not drawn on the conversation the person has in front of them', () => {
    const vite = sessions.find((s) => s.title === MARKED)!;
    renderSidebar(keyOf(vite), true);

    expect(dotIn(MARKED)).toBeNull();
    expect(rowButton(MARKED)).not.toHaveAttribute('aria-describedby');
  });

  it('is drawn on the open conversation while its window is behind another', () => {
    const vite = sessions.find((s) => s.title === MARKED)!;
    renderSidebar(keyOf(vite), false);

    expect(dotIn(MARKED)).not.toBeNull();
  });
});

describe('how the dot is drawn', () => {
  it('is an 8 px disc in the Danger red, out of the flow, centred on its line', () => {
    const ui = css(['components', 'ui.css']);
    const dot = rule(ui, '.unseen-dot');

    expect(dot).toContain('position: absolute');
    expect(dot).toContain('top: 50%');
    expect(dot).toContain('width: var(--unseen-dot)');
    expect(dot).toContain('background: var(--danger)');
    expect(rule(css(['styles', 'tokens.css']), ':root')).toContain('--unseen-dot: 8px');
  });

  it('is placed from the title line of each list, in the gutter every row keeps', () => {
    const sessionsCss = css(['features', 'sessions', 'sessions.css']);
    expect(rule(sessionsCss, '.session-line')).toContain('position: relative');
    expect(rule(sessionsCss, '.session-open')).toContain('--unseen-reach: calc(var(--sp-5) / 2)');

    const chat = css(['features', 'chat', 'chat.css']);
    expect(rule(chat, '.sidebar-item')).toContain('padding: 0 var(--sp-2) 0 var(--sp-5)');
    const line = rule(chat, '.sidebar-title-line');
    expect(line).toContain('position: relative');
    expect(line).toContain('--unseen-reach: calc(var(--sp-5) / 2 + var(--status-dot) + var(--sp-2))');
  });
});
