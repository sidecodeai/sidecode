/**
 * `docs/DESIGN.md` § "Session lists", "A session with no title still has a
 * name": the chat header prints "Untitled session" in the title's own type
 * rather than leaving an empty heading above the path.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { ChatHeader } from '../src/features/chat/ChatHeader';
import { strings } from '../src/strings';
import { sessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

/** A quiet session: no turn timer, so the header renders once and settles. */
const named: Session = { ...sessions[0]!, turn: null, usage: null };

const header = (session: Session) =>
  render(
    <MemoryRouter>
      <ChatHeader
        session={session}
        agent={null}
        deviceName="mac-studio-office"
        deviceOnline
        todos={[]}
        stopping={false}
        onStop={vi.fn()}
      />
    </MemoryRouter>,
  );

const heading = (): HTMLElement => screen.getByRole('heading', { level: 1 });

describe('the chat header title', () => {
  it('names a session with an empty title', () => {
    header({ ...named, title: '' });

    expect(heading()).toHaveTextContent(strings.sessions.untitled);
  });

  it('names one whose title is only whitespace', () => {
    header({ ...named, title: '   ' });

    expect(heading()).toHaveTextContent(strings.sessions.untitled);
  });

  it('leaves a real title alone', () => {
    header(named);

    expect(heading()).toHaveTextContent(named.title);
  });
});

/**
 * The line under the title: the device, the working directory and the branch. A narrow line cuts
 * the directory from its head, never past the folder it ends in, before the device or the branch
 * (`chat.css`, which jsdom does not lay out); what is drawn here are the parts that rule needs.
 */
describe('the line under the chat header title', () => {
  const git = { branch: 'main', dirty: false, ahead: 0, behind: 0, worktree: false };
  const line = (): HTMLElement => document.querySelector('.chat-sub') as HTMLElement;
  const part = (name: string): HTMLElement | null => line().querySelector(`.chat-sub-${name}`);

  it('keeps the device, the whole directory and the branch in its text', () => {
    header({ ...named, cwd: '/Users/me/dev/remote-control/gateway', git });

    expect(line().textContent).toBe('mac-studio-office:~/dev/remote-control/gateway · main');
    expect(part('device')).toHaveTextContent('mac-studio-office');
    expect(part('branch')).toHaveTextContent('main');
  });

  it('sets the folder the path ends in apart from what leads to it, which is cut from its head', () => {
    header({ ...named, cwd: '/Users/me/dev/remote-control/gateway', git });

    expect(part('folder')?.textContent).toBe('gateway');
    // Right-to-left outside, an isolated left-to-right run inside: the characters keep their
    // order, a "~/" with no letter in it included, and the ellipsis lands at the head.
    const run = part('head')?.querySelector('bdi');
    expect(run).toHaveAttribute('dir', 'ltr');
    expect(run?.textContent).toBe('~/dev/remote-control/');
  });

  it('draws no branch outside a repository, and no head for a path that is all folder', () => {
    header({ ...named, cwd: '/Users/me', git: null });

    expect(line().textContent).toBe('mac-studio-office:~');
    expect(part('branch')).toBeNull();
    expect(part('head')).toBeNull();
    expect(part('folder')?.textContent).toBe('~');
  });
});
