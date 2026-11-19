/**
 * The Todos and Tasks chips in the conversation header. Where there is room
 * they read "Todos 1/4" and "Tasks 3"; on a phone they are their icon and
 * number alone, so the title keeps its room — at 390 px the two chips once left
 * "Fix flak…" of "Fix flaky auth test". Each keeps its accessible name, its
 * whole pill as the target and the panel it opens at every width.
 *
 * jsdom never loads a stylesheet, so the widths are read from `chat.css`, and
 * the markup is checked for the classes those rules hang off.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { ChatHeader } from '../src/features/chat/ChatHeader';
import { BoardChip } from '../src/features/chat/board/BoardChip';
import { useBoards } from '../src/stores/board';
import { useConnection } from '../src/stores/connection';
import { useSettings } from '../src/stores/settings';
import { sessionKey } from '../src/stores/sessions';
import { sessions } from '../mock/fixtures';
import type { Board, BoardItem, Session, TodoItem } from '../src/protocol/types';

const css = readFileSync(join(process.cwd(), 'src', 'features', 'chat', 'chat.css'), 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
);

/** The body of the rule or at-rule `header` opens, braces balanced. */
function block(source: string, header: string): string {
  const start = source.indexOf(`${header} {`);
  expect(start, `${header} is not in chat.css`).toBeGreaterThan(-1);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(source.indexOf('{', start) + 1, i);
    }
  }
  throw new Error(`${header} has no closing brace`);
}

const PHONE = '@media (max-width: 480px)';

/** A quiet session: no turn timer, so the header renders once and settles. */
const session: Session = { ...sessions[0]!, turn: null, usage: null, todos: null };

const todo = (id: string, status: TodoItem['status']): TodoItem => ({ id, text: `step ${id}`, status });
const TODOS = [todo('1', 'completed'), todo('2', 'in_progress'), todo('3', 'pending'), todo('4', 'pending')];

const boardItem = (id: string, status: BoardItem['status']): BoardItem => ({
  id,
  text: `task ${id}`,
  status,
  created_at: 1,
  updated_at: 1,
});

function holdsBoard(open: number) {
  const items = Array.from({ length: open }, (_, i) => boardItem(`t${i}`, 'todo'));
  const board: Board = {
    session_id: session.session_id,
    device_id: session.device_id,
    revision: 1,
    items,
    updated_at: Date.now(),
    partial: false,
  };
  useBoards.setState({
    boards: {
      [sessionKey(session.device_id, session.session_id)]: {
        board,
        status: { state: 'current', reason: null },
      },
    },
  });
}

function header() {
  return render(
    <MemoryRouter>
      <ChatHeader
        session={session}
        agent={null}
        deviceName="mac-studio-office"
        deviceOnline
        todos={TODOS}
        stopping={false}
        onStop={vi.fn()}
      />
    </MemoryRouter>,
  );
}

function boardChip() {
  return render(
    <MemoryRouter>
      <BoardChip deviceId={session.device_id} sessionId={session.session_id} />
    </MemoryRouter>,
  );
}

/** The chip's two faces: its words where there is room, its number on a phone. */
function faces(chip: HTMLElement) {
  return {
    words: chip.querySelector('.header-chip-words')?.textContent ?? null,
    count: chip.querySelector('.header-chip-count'),
  };
}

beforeEach(() => {
  useSettings.setState({ language: 'en', timelineDetail: 'detailed' });
  useBoards.getState().reset();
  useConnection.setState({ board: { enabled: true } });
});

afterEach(() => {
  useConnection.setState({ board: null });
  useSettings.setState({ language: 'en' });
});

describe('the Todos chip', () => {
  it('reads Todos 1/4, and 1/4 on a phone, named Todos either way', () => {
    header();
    const chip = screen.getByRole('button', { name: 'Todos' });

    expect(chip.closest('.popover-root')).toHaveClass('header-chip');
    const { words, count } = faces(chip);
    expect(words).toBe('Todos 1/4');
    expect(count).toHaveTextContent('1/4');
    expect(count).toHaveAttribute('aria-hidden', 'true');
  });

  it('opens the same list from either face', async () => {
    header();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Todos' }));

    expect(screen.getByText('step 3')).toBeInTheDocument();
  });

  it('reads 待办 1/4 in 中文, and the same 1/4 on a phone', () => {
    useSettings.setState({ language: 'zh-Hans' });
    header();
    const chip = screen.getByRole('button', { name: '待办' });

    expect(faces(chip).words).toBe('待办 1/4');
    expect(faces(chip).count).toHaveTextContent('1/4');
  });
});

describe('the Tasks chip', () => {
  it('reads Tasks 3, and 3 on a phone, named Tasks 3 either way', () => {
    holdsBoard(3);
    boardChip();
    const chip = screen.getByRole('button', { name: 'Tasks 3' });

    expect(chip.closest('.popover-root')).toHaveClass('board-chip', 'header-chip');
    expect(faces(chip).words).toBe('Tasks 3');
    expect(faces(chip).count).toHaveTextContent('3');
  });

  it('is its icon alone on a phone while nothing is open, named Tasks', () => {
    holdsBoard(0);
    boardChip();
    const chip = screen.getByRole('button', { name: 'Tasks' });

    expect(faces(chip).words).toBe('Tasks');
    expect(faces(chip).count).toBeNull();
  });

  it('reads 看板 3 in 中文, and the same 3 on a phone', () => {
    useSettings.setState({ language: 'zh-Hans' });
    holdsBoard(3);
    boardChip();
    const chip = screen.getByRole('button', { name: '看板 3' });

    expect(within(chip).getByText('看板 3')).toHaveClass('header-chip-words');
    expect(faces(chip).count).toHaveTextContent('3');
  });
});

describe('the chips’ widths', () => {
  it('show the number only on a phone, where the words and the chevron give way', () => {
    expect(block(css, '.chat-header-actions .header-chip-count')).toContain('display: none');

    const phone = block(css, PHONE);
    expect(block(phone, '.chat-header-actions .header-chip-count')).toContain('display: inline');
    const hidden = block(
      phone,
      '.chat-header-actions .header-chip-words,\n  .chat-header-actions .header-chip .popover-chevron',
    );
    expect(hidden).toContain('display: none');
  });

  it('leave the wide header as it was', () => {
    // The words and the chevron are hidden by the phone rule alone.
    const outsidePhone = css.replace(block(css, PHONE), '');
    expect(outsidePhone).not.toMatch(/\.header-chip-words/);
    expect(outsidePhone).not.toMatch(/\.header-chip \.popover-chevron/);
  });
});
