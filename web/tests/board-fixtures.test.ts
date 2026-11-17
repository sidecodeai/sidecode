/**
 * A62 — the task board's examples from `protocol/fixtures/`, decoded the way
 * the app reads them: the two requests, the board, both replies and the frame,
 * `board` in `hello` and `GET /api/config`, and `task_board` in the account's
 * preferences, which the app reads and never writes up from its own value.
 * What the schema refuses the app still survives: an item of a status it does
 * not know is not drawn, and an unavailable status with no reason reads as the
 * generic line.
 */
import { describe, expect, it } from 'vitest';
import { groupsOf, noteKind } from '../src/features/chat/board/boardView';
import type { ConfigResponse, PreferencesPatch, PreferencesResponse } from '../src/lib/api';
import type {
  BoardGetResult,
  BoardRefreshResult,
  BoardUpdatedFrame,
  HelloFrame,
  Reply,
  RequestParams,
} from '../src/protocol/frames';
import type { Board, Preferences } from '../src/protocol/types';
import { readBoardStatus } from '../src/stores/board';
import { readPreferences } from '../src/stores/preferenceFields';
import { fixturesAvailable, readFixture, readInvalidFixture } from './fixtures';

const ITEM_ID = /^t[1-9][0-9]{0,5}$/;

function assertBoard(board: Board): void {
  expect(typeof board.session_id).toBe('string');
  expect(typeof board.device_id).toBe('string');
  expect(board.revision).toBeGreaterThanOrEqual(1);
  expect(board.items.length).toBeLessThanOrEqual(30);
  expect(typeof board.updated_at).toBe('number');
  expect(typeof board.partial).toBe('boolean');
  for (const item of board.items) {
    expect(item.id).toMatch(ITEM_ID);
    expect(['todo', 'doing', 'done']).toContain(item.status);
    expect(item.text.length).toBeGreaterThan(0);
    expect(item.text.length).toBeLessThanOrEqual(120);
    expect(typeof item.created_at).toBe('number');
    expect(typeof item.updated_at).toBe('number');
  }
}

/** The result of a reply that is `ok`, or a failure that names the fixture. */
function resultOf<T>(name: string): T {
  const reply = readFixture<Reply<T>>(name);
  if (!reply.ok) throw new Error(`${name} is not an ok reply`);
  return reply.result;
}

describe.runIf(fixturesAvailable())('the task board fixtures', () => {
  it('decodes the two requests', () => {
    const get = readFixture<RequestParams<'board.get'> & { type: string; id: string }>('app/board.get.json');
    expect(get.type).toBe('board.get');
    expect(typeof get.id).toBe('string');
    expect(typeof get.session_id).toBe('string');

    const refresh = readFixture<RequestParams<'board.refresh'> & { type: string }>('app/board.refresh.json');
    expect(refresh.type).toBe('board.refresh');
    expect(refresh.session_id).toBe(get.session_id);
    expect(refresh.rebuild).toBe(false);
  });

  it('decodes the board in the order the panel draws it', () => {
    const board = readFixture<Board>('objects/board.json');
    assertBoard(board);
    expect(groupsOf(board).flatMap((group) => group.items)).toEqual(board.items);
  });

  it('decodes both replies and the frame, which carry the same board', () => {
    const board = readFixture<Board>('objects/board.json');

    const got = resultOf<BoardGetResult>('app/reply.board.get.json');
    expect(got.board).toEqual(board);
    expect(got.status).toEqual({ state: 'updating', reason: null });

    const off = resultOf<BoardGetResult>('app/reply.board.get.off.json');
    expect(off).toEqual({ board: null, status: { state: 'off', reason: null } });

    const refreshed = resultOf<BoardRefreshResult>('app/reply.board.refresh.json');
    expect(refreshed.status).toEqual({ state: 'stale', reason: 'device_offline', retry_at: null });
    expect(noteKind(readBoardStatus(refreshed.status))).toBe('offline');

    const updated = readFixture<BoardUpdatedFrame>('app/board.updated.json');
    expect(updated.type).toBe('board.updated');
    expect(updated.session_id).toBe(board.session_id);
    expect(updated.device_id).toBe(board.device_id);
    expect(updated.board).toEqual(board);
    expect(updated.status).toEqual({ state: 'current', reason: null });
  });

  it('reads whether the gateway keeps boards from hello and GET /api/config', () => {
    expect(readFixture<HelloFrame>('app/hello.json').board).toEqual({ enabled: true });
    expect(readFixture<ConfigResponse>('http/config.response.json').board).toEqual({ enabled: true });
  });

  it('reads task_board off the account, and keeps it out of what the app writes up', () => {
    const response = readFixture<PreferencesResponse>('http/preferences.response.json');
    const patch = readFixture<PreferencesPatch>('http/preferences.patch.request.json');
    const updated = readFixture<{ preferences: Preferences }>('app/preferences.updated.json');
    for (const preferences of [response.preferences, patch, updated.preferences]) {
      expect(preferences.task_board).toBe(true);
    }
    expect(readPreferences(response.preferences)).not.toHaveProperty('task_board');
  });

  it('survives what the schema refuses', () => {
    const unknown = readInvalidFixture<Board>('objects.board__item_status_unknown.json');
    const drawn = groupsOf(unknown).flatMap((group) => group.items);
    expect(drawn.every((item) => ['todo', 'doing', 'done'].includes(item.status))).toBe(true);
    expect(drawn.length).toBeLessThan(unknown.items.length);

    const reasonless = readInvalidFixture<BoardUpdatedFrame>('app__board_updated_unavailable_without_reason.json');
    expect(noteKind(readBoardStatus(reasonless.status))).toBe('unavailable');
  });
});
