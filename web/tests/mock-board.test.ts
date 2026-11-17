/**
 * A62 — the mock gateway's task boards, so `npm run dev:mock` shows the panel
 * the way a gateway fills it: off until the account turns the board on, the
 * demo conversation's board made on its first open and current after that,
 * every other board empty, Refresh raising the revision and Start over giving
 * every item a new id. Revisions come from one counter for every board, so a
 * conversation's never goes back.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  CURRENT,
  DEMO_BOARD_SESSION,
  OFF,
  UPDATING,
  answerGet,
  boardOf,
  forgetBoard,
  refusal,
  resetBoards,
  run,
} from '../mock/board';
import type { Board } from '../src/protocol/types';
import { fixturesAvailable, readFixture } from './fixtures';

const DEVICE = 'dev-mac';
const NOW = 1_788_944_481_100;

const ids = (board: Board | null) => board?.items.map((item) => item.id);

beforeEach(() => {
  resetBoards();
});

describe('the mock’s task boards', () => {
  it('refuses nothing to an account with the board on', () => {
    expect(refusal(false)).toEqual(OFF);
    expect(refusal(true)).toBeNull();
  });

  it('answers off with no board while the board is off', () => {
    run(DEMO_BOARD_SESSION, DEVICE, false, NOW);

    expect(answerGet(DEMO_BOARD_SESSION, DEVICE, OFF)).toEqual({ board: null, status: OFF, build: false });
  });

  it('makes the demo board on its first open, and serves it current after that', () => {
    expect(answerGet(DEMO_BOARD_SESSION, DEVICE, null)).toEqual({ board: null, status: UPDATING, build: true });

    const made = run(DEMO_BOARD_SESSION, DEVICE, false, NOW);

    expect(made).toMatchObject({ session_id: DEMO_BOARD_SESSION, device_id: DEVICE, partial: false });
    expect(made.revision).toBeGreaterThanOrEqual(1);
    expect(ids(made)).toEqual(['t3', 't4', 't2', 't1']);
    expect(made.items.map((item) => item.status)).toEqual(['doing', 'todo', 'done', 'done']);
    expect(answerGet(DEMO_BOARD_SESSION, DEVICE, null)).toEqual({ board: made, status: CURRENT, build: false });
  });

  it('keeps the stored board readable while its updates are refused', () => {
    const made = run(DEMO_BOARD_SESSION, DEVICE, false, NOW);
    const refused = { state: 'unavailable', reason: 'billing_not_configured' };

    expect(answerGet(DEMO_BOARD_SESSION, DEVICE, refused)).toEqual({ board: made, status: refused, build: false });
  });

  it('serves every other conversation an empty board', () => {
    expect(answerGet('ses-vite', DEVICE, null)).toEqual({ board: null, status: CURRENT, build: false });
    expect(run('ses-vite', DEVICE, false, NOW)).toMatchObject({ items: [] });
  });

  it('raises the revision on Refresh, keeping the ids, and gives new ids on Start over', () => {
    const made = run(DEMO_BOARD_SESSION, DEVICE, false, NOW);

    const refreshed = run(DEMO_BOARD_SESSION, DEVICE, false, NOW + 1);
    expect(refreshed.revision).toBeGreaterThan(made.revision);
    expect(ids(refreshed)).toEqual(['t3', 't4', 't2', 't1']);

    const rebuilt = run(DEMO_BOARD_SESSION, DEVICE, true, NOW + 2);
    expect(rebuilt.revision).toBeGreaterThan(refreshed.revision);
    expect(ids(rebuilt)).toEqual(['t7', 't8', 't6', 't5']);
  });

  it('forgets a board when the account turns the board off, and makes it again with a higher revision', () => {
    const first = run(DEMO_BOARD_SESSION, DEVICE, false, NOW);

    expect(forgetBoard(DEMO_BOARD_SESSION)).toBe(true);
    expect(boardOf(DEMO_BOARD_SESSION, DEVICE)).toBeNull();
    expect(answerGet(DEMO_BOARD_SESSION, DEVICE, null).build).toBe(true);
    expect(run(DEMO_BOARD_SESSION, DEVICE, false, NOW).revision).toBeGreaterThan(first.revision);
  });

  it('numbers every board from the one counter', () => {
    const demo = run(DEMO_BOARD_SESSION, DEVICE, false, NOW);
    const other = run('ses-vite', DEVICE, false, NOW);

    expect(other.revision).toBe(demo.revision + 1);
  });

  it.runIf(fixturesAvailable())(
    'says what the protocol’s example board says, in the same order',
    () => {
      const example = readFixture<Board>('objects/board.json');
      const made = run(DEMO_BOARD_SESSION, DEVICE, false, NOW);

      expect(made.items.map(({ id, text, status }) => ({ id, text, status }))).toEqual(
        example.items.map(({ id, text, status }) => ({ id, text, status })),
      );
    },
  );
});
