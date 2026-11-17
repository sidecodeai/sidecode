/**
 * A62 — when the open conversation asks the gateway for its task board. Only on
 * a gateway that keeps boards, and only once the conversation's subscribe has
 * answered: the gateway lets a socket watch a board, and so receive its
 * `board.updated`, only while it is subscribed to the session.
 *
 * `board.get` goes when the conversation opens, when a reconnect has subscribed
 * it again — both are the subscribe answering — and when the page comes back in
 * front of the person with it open. `board.refresh` goes when the account turns
 * the board on, here or in another of its apps, so the conversation on screen is
 * followed and updated at once.
 */
import { useCallback, useEffect, useRef } from 'react';
import { useBoards } from '../../../stores/board';
import { useChat } from '../../../stores/chat';
import { useConnection } from '../../../stores/connection';
import { usePreferences } from '../../../stores/preferences';
import { sessionKey } from '../../../stores/sessions';

/** Runs `act` when `value` turns true while `ready`; a turn while not ready is let go. */
function useOnRise(value: boolean, ready: boolean, act: () => void): void {
  const last = useRef(value);
  useEffect(() => {
    const rose = value && !last.current;
    last.current = value;
    if (rose && ready) act();
  }, [value, ready, act]);
}

export function useBoardRequests(deviceId: string, sessionId: string, inFront: boolean): void {
  const offered = useConnection((s) => s.board?.enabled === true);
  const subscribed = useChat((s) => s.sessions[sessionKey(deviceId, sessionId)]?.subscription === 'live');
  const turnedOn = usePreferences((s) => s.preferences?.task_board === true);
  const ready = offered && subscribed;

  const get = useCallback(() => void useBoards.getState().get(deviceId, sessionId), [deviceId, sessionId]);
  const refresh = useCallback(() => void useBoards.getState().refresh(deviceId, sessionId), [deviceId, sessionId]);

  useEffect(() => {
    if (ready) get();
  }, [ready, get]);
  useOnRise(inFront, ready, get);
  useOnRise(turnedOn, ready, refresh);
}
