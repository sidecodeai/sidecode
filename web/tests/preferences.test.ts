/**
 * A35 — the account's preferences. They live on the gateway, so `hello` seeds
 * them, `preferences.updated` keeps every app of the account in step, and a
 * change is written with `PATCH /api/preferences` rather than kept here.
 * `docs/DESIGN.md` § "Paused by the usage limit".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/lib/api';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import { signOut } from '../src/stores/signOut';
import type { HelloFrame, PushFrame } from '../src/protocol/frames';

// The store owns the socket, so the frames are fed through the one it opens.
const { captured } = vi.hoisted(() => ({
  captured: { onFrame: null as ((frame: PushFrame) => void) | null },
}));

vi.mock('../src/lib/ws', () => ({
  socketUrl: () => 'ws://test/ws/app',
  AppSocket: class {
    constructor(options: { onFrame: (frame: PushFrame) => void }) {
      captured.onFrame = options.onFrame;
    }
    start(): void {
      /* nothing to dial in a test */
    }
    stop(): void {
      /* nothing to close */
    }
  },
}));

const handleFrame = (frame: PushFrame): void => {
  if (!captured.onFrame) throw new Error('the connection store opened no socket');
  captured.onFrame(frame);
};

beforeEach(() => {
  usePreferences.setState({ preferences: undefined });
  useConnection.getState().connect(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const hello = (preferences?: { resume_after_limit: boolean }): HelloFrame =>
  ({
    type: 'hello',
    protocol: 1,
    gateway_version: '1.4.0',
    user: { username: 'admin', role: 'admin' },
    devices: [],
    sessions: [],
    stt: { enabled: false, languages: ['auto'] },
    server_time: Date.now(),
    ...(preferences ? { preferences } : {}),
  }) as HelloFrame;

describe('the account preferences', () => {
  it('reads what hello carried', () => {
    handleFrame(hello({ resume_after_limit: true }));
    expect(usePreferences.getState().preferences).toEqual({ resume_after_limit: true });
  });

  it('stays undefined on a gateway that sent none, which is not "off"', () => {
    handleFrame(hello());
    expect(usePreferences.getState().preferences).toBeUndefined();
  });

  it('follows a change another app of the account made', () => {
    handleFrame(hello({ resume_after_limit: false }));
    handleFrame({
      type: 'preferences.updated',
      preferences: { resume_after_limit: true },
    } as PushFrame);
    expect(usePreferences.getState().preferences?.resume_after_limit).toBe(true);
  });

  it('writes a change to the gateway and keeps what it answered', async () => {
    const patch = vi
      .spyOn(api, 'setPreferences')
      .mockResolvedValue({ preferences: { resume_after_limit: true } });
    handleFrame(hello({ resume_after_limit: false }));

    await usePreferences.getState().setResumeAfterLimit(true);

    expect(patch).toHaveBeenCalledWith({ resume_after_limit: true });
    expect(usePreferences.getState().preferences?.resume_after_limit).toBe(true);
  });

  it('puts the switch back when the gateway refuses', async () => {
    vi.spyOn(api, 'setPreferences').mockRejectedValue(new Error('nope'));
    handleFrame(hello({ resume_after_limit: false }));

    await expect(usePreferences.getState().setResumeAfterLimit(true)).rejects.toThrow();
    expect(usePreferences.getState().preferences?.resume_after_limit).toBe(false);
  });

  it('writes nothing to a gateway that holds no preferences', async () => {
    const patch = vi.spyOn(api, 'setPreferences');
    handleFrame(hello());

    await usePreferences.getState().setResumeAfterLimit(true);

    expect(patch).not.toHaveBeenCalled();
    expect(usePreferences.getState().preferences).toBeUndefined();
  });

  it('is emptied by signing out, like everything else of an account', () => {
    handleFrame(hello({ resume_after_limit: true }));
    signOut();
    expect(usePreferences.getState().preferences).toBeUndefined();
  });
});
