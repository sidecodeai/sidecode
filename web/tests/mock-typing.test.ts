/**
 * A40 and A42 — the rules the mock device applies to a change it has to type
 * into a terminal, so what `npm run dev:mock` does can be read without a
 * socket: the per-key gate of `shared_settings_keys`, which attachments type
 * rather than call, when the terminal is too busy to be typed into, and what
 * the Escape behind Stop will not talk over.
 */
import { describe, expect, it } from 'vitest';
import {
  ANSWER_THE_PROMPT,
  SETTING_KEYS,
  TERMINAL_BUSY,
  TYPING_MS,
  lockedKeys,
  promptOnScreen,
  settingKeys,
  terminalBusy,
  typedSession,
} from '../mock/typing';
import { claudeAgent, claudeNoShim, codexAgent, sessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const mockSession = (id: string): Session => {
  const found = sessions.find((s) => s.session_id === id);
  if (!found) throw new Error(`no such mock session ${id}`);
  return found;
};

const claudeShared = mockSession('ses-shared');
const codexShared = mockSession('ses-codex-shared');

describe('A40 which settings a shared session gives up', () => {
  it('locks the keys the agent did not name', () => {
    expect(lockedKeys(claudeShared, claudeAgent, { model: 'claude-haiku-4-5' })).toEqual([]);
    expect(lockedKeys(claudeShared, claudeAgent, { effort: 'low' })).toEqual([]);
    expect(lockedKeys(claudeShared, claudeAgent, { permission_mode: 'plan' })).toEqual([
      'permission_mode',
    ]);
    // The title is the app's own, never the terminal's.
    expect(lockedKeys(claudeShared, claudeAgent, { title: 'Renamed' })).toEqual([]);
  });

  it('locks every setting on an attachment that carries none', () => {
    expect(lockedKeys(claudeShared, claudeNoShim, { model: 'claude-haiku-4-5' })).toEqual([
      'model',
    ]);
  });

  it('locks nothing when the agent names no subset', () => {
    const frame = Object.fromEntries(SETTING_KEYS.map((key) => [key, 'x']));
    expect(codexAgent.shared_settings_keys).toBeUndefined();
    expect(lockedKeys(codexShared, codexAgent, frame)).toEqual([]);
  });

  it('locks nothing on a session no terminal holds', () => {
    const remote: Session = { ...claudeShared, control: 'remote' };
    expect(lockedKeys(remote, claudeNoShim, { permission_mode: 'plan' })).toEqual([]);
  });

  it('reads a null speed as a setting, and an absent one as nothing', () => {
    expect(settingKeys({ speed: null })).toEqual(['speed']);
    expect(settingKeys({ title: 'Renamed' })).toEqual([]);
    expect(settingKeys({ model: 'a', effort: 'b' })).toEqual(['model', 'effort']);
  });
});

describe('A40 when the device types instead of calling', () => {
  it('types into a channel attachment and calls every other one', () => {
    expect(typedSession(claudeShared, claudeAgent)).toBe(true);
    expect(typedSession(codexShared, codexAgent)).toBe(false);
    expect(typedSession({ ...claudeShared, control: 'remote' }, claudeAgent)).toBe(false);
  });

  it('waits for an idle terminal, and says so in the device\'s words', () => {
    expect(terminalBusy(claudeShared)).toBe(false);
    expect(terminalBusy({ ...claudeShared, state: 'running' })).toBe(true);
    expect(terminalBusy({ ...claudeShared, state: 'needs_approval' })).toBe(true);
    expect(
      terminalBusy({ ...claudeShared, turn: { turn_id: 't1', started_at: 1 } }),
    ).toBe(true);
    expect(TERMINAL_BUSY).toBe('the terminal is busy; try again in a moment');
  });

  it('takes a moment, the way typing does', () => {
    expect(TYPING_MS).toBeGreaterThan(0);
  });
});

/**
 * A42 — Stop on the same terminal is one Escape. It has the opposite gate to
 * a setting: a running turn is what it is for, while a prompt on screen is
 * what the CLI would spend the keystroke on.
 */
describe('A42 the Escape behind Stop', () => {
  it('refuses while the CLI is asking something', () => {
    expect(promptOnScreen({ ...claudeShared, state: 'needs_approval' })).toBe(true);
    expect(promptOnScreen({ ...claudeShared, state: 'needs_input' })).toBe(true);
    expect(ANSWER_THE_PROMPT).toBe('answer the prompt first');
  });

  it('does not wait for an idle terminal, unlike a setting', () => {
    const running: Session = { ...claudeShared, state: 'running' };
    expect(terminalBusy(running)).toBe(true);
    expect(promptOnScreen(running)).toBe(false);
    // Idle: nothing to stop, and nothing to refuse either.
    expect(promptOnScreen(claudeShared)).toBe(false);
  });

  it('is carried only where the shim gave the CLI a terminal', () => {
    expect(claudeAgent.shared_interrupt).toBe(true);
    expect(claudeNoShim.shared_interrupt).toBe(false);
    // The daemon does not type at all, so nothing here gates it.
    expect(typedSession(codexShared, codexAgent)).toBe(false);
    expect(codexAgent.shared_interrupt).toBe(true);
  });
});
