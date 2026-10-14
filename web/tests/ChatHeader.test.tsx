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
