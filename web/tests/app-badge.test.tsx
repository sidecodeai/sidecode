/**
 * A47 (PROTOCOL §8 rule 22, `docs/DESIGN.md` § "A red dot for a session that
 * stopped and waits for you"): the installed web app's icon carries the number
 * of unarchived sessions with a red dot, where the browser offers a badge
 * (`navigator.setAppBadge`) — none at zero, nothing at all elsewhere — kept to
 * the count while the page runs and cleared on sign-out. Every call goes to a
 * fake `navigator`: no test asks a real browser for anything.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { clearAppBadge, showAppBadge } from '../src/push/appBadge';
import { useAppBadge } from '../src/push/useAppBadge';
import { useFront } from '../src/stores/front';
import { countUnseen, keyOf, useSessions } from '../src/stores/sessions';
import { signOut } from '../src/stores/signOut';
import { sessions as fixtureSessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

vi.mock('../src/lib/gateway', () => ({
  rpc: vi.fn(),
  getSocket: () => null,
  setSocket: () => undefined,
}));

const base = fixtureSessions.find((s) => s.session_id === 'ses-push')!;

const session = (id: string, extra: Partial<Session> = {}): Session => ({
  ...base,
  session_id: id,
  ...extra,
});

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

/** A browser that offers the Badging API, and what it was told. */
function badging() {
  return {
    setAppBadge: vi.fn((_count?: number) => Promise.resolve()),
    clearAppBadge: vi.fn(() => Promise.resolve()),
  };
}

/** The same, installed on the page's own `navigator` for the hook and sign-out. */
function installBadging() {
  const fake = badging();
  Object.defineProperty(navigator, 'setAppBadge', { configurable: true, value: fake.setAppBadge });
  Object.defineProperty(navigator, 'clearAppBadge', {
    configurable: true,
    value: fake.clearAppBadge,
  });
  return fake;
}

afterEach(() => {
  Reflect.deleteProperty(navigator, 'setAppBadge');
  Reflect.deleteProperty(navigator, 'clearAppBadge');
});

describe('the count', () => {
  it('is the unarchived sessions with the mark', () => {
    const list = index([
      session('a', { unseen: true }),
      session('b', { unseen: true }),
      session('c'),
      session('d', { unseen: false }),
      session('e', { unseen: true, archived: true }),
    ]);

    expect(countUnseen(list)).toBe(2);
    expect(countUnseen({})).toBe(0);
  });

  it('reads the mock’s seeded mark, so a first sign-in shows one', () => {
    expect(countUnseen(index(fixtureSessions))).toBe(1);
  });

  it('leaves out the conversation in front of the person, which draws no dot', () => {
    const list = index([session('a', { unseen: true }), session('b', { unseen: true })]);

    expect(countUnseen(list, keyOf(session('a')))).toBe(1);
    expect(countUnseen(list, keyOf(session('c')))).toBe(2);
    expect(countUnseen(list, null)).toBe(2);
  });
});

describe('the badge the browser draws', () => {
  it('shows the count', () => {
    const nav = badging();
    showAppBadge(3, nav);

    expect(nav.setAppBadge).toHaveBeenCalledWith(3);
    expect(nav.clearAppBadge).not.toHaveBeenCalled();
  });

  it('shows none at zero', () => {
    const nav = badging();
    showAppBadge(0, nav);

    expect(nav.clearAppBadge).toHaveBeenCalledOnce();
    expect(nav.setAppBadge).not.toHaveBeenCalled();
  });

  it('does nothing where the browser has no badge', () => {
    expect(() => showAppBadge(2, {})).not.toThrow();
    expect(() => clearAppBadge({})).not.toThrow();
  });

  it('keeps a refusal to itself', async () => {
    const nav = {
      setAppBadge: vi.fn(() => Promise.reject(new DOMException('not installed', 'NotAllowedError'))),
      clearAppBadge: vi.fn(() => Promise.reject(new DOMException('not installed', 'NotAllowedError'))),
    };
    const unhandled = vi.fn();
    process.on('unhandledRejection', unhandled);
    showAppBadge(1, nav);
    clearAppBadge(nav);
    await new Promise((resolve) => setTimeout(resolve, 0));
    process.off('unhandledRejection', unhandled);

    expect(nav.setAppBadge).toHaveBeenCalledWith(1);
    expect(unhandled).not.toHaveBeenCalled();
  });
});

describe('while the page runs', () => {
  function Badge() {
    useAppBadge();
    return null;
  }

  beforeEach(() => {
    useSessions.getState().reset();
    useFront.setState({ key: null });
  });

  it('never counts the conversation in front, so a turn that ends on screen shows nothing', () => {
    const nav = installBadging();
    render(<Badge />);
    act(() => useSessions.getState().replaceAll([session('a'), session('b')]));
    act(() => useFront.setState({ key: keyOf(session('a')) }));

    // The mark arrives while the person watches, and the gateway clears it
    // after the page's session.seen.
    act(() => useSessions.getState().upsert(session('a', { unseen: true })));
    act(() => useSessions.getState().upsert(session('a')));
    expect(nav.setAppBadge).not.toHaveBeenCalled();

    // Another session's mark is counted as ever.
    act(() => useSessions.getState().upsert(session('b', { unseen: true })));
    expect(nav.setAppBadge).toHaveBeenLastCalledWith(1);
  });

  it('counts the open conversation again once the page is out of sight', () => {
    const nav = installBadging();
    render(<Badge />);
    act(() => useSessions.getState().replaceAll([session('a', { unseen: true })]));
    act(() => useFront.setState({ key: keyOf(session('a')) }));
    expect(nav.clearAppBadge).toHaveBeenCalled();

    act(() => useFront.setState({ key: null }));
    expect(nav.setAppBadge).toHaveBeenLastCalledWith(1);
  });

  it('writes nothing before the account’s list has arrived', () => {
    const nav = installBadging();
    render(<Badge />);

    expect(nav.setAppBadge).not.toHaveBeenCalled();
    expect(nav.clearAppBadge).not.toHaveBeenCalled();
  });

  it('follows the count as marks come and go', () => {
    const nav = installBadging();
    render(<Badge />);

    act(() => useSessions.getState().replaceAll([session('a', { unseen: true }), session('b')]));
    expect(nav.setAppBadge).toHaveBeenLastCalledWith(1);

    act(() => useSessions.getState().upsert(session('b', { unseen: true })));
    expect(nav.setAppBadge).toHaveBeenLastCalledWith(2);

    // Seen on another app: the gateway's session.updated drops the mark.
    act(() => useSessions.getState().upsert(session('a')));
    expect(nav.setAppBadge).toHaveBeenLastCalledWith(1);
    expect(nav.clearAppBadge).not.toHaveBeenCalled();

    act(() => useSessions.getState().upsert(session('b')));
    expect(nav.clearAppBadge).toHaveBeenCalledOnce();
    expect(nav.setAppBadge.mock.calls.map(([count]) => count)).toEqual([1, 2, 1]);
  });

  it('is not written again for a change that leaves the count alone', () => {
    const nav = installBadging();
    render(<Badge />);
    act(() => useSessions.getState().replaceAll([session('a', { unseen: true })]));
    act(() => useSessions.getState().upsert(session('a', { unseen: true, updated_at: 9 })));

    expect(nav.setAppBadge).toHaveBeenCalledOnce();
  });

  it('is cleared when the account signs out', () => {
    const nav = installBadging();
    render(<Badge />);
    act(() => useSessions.getState().replaceAll([session('a', { unseen: true })]));
    nav.clearAppBadge.mockClear();

    act(() => signOut());

    expect(nav.clearAppBadge).toHaveBeenCalledOnce();
    expect(nav.setAppBadge).toHaveBeenCalledTimes(1);
  });
});
