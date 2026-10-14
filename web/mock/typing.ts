/**
 * A40 — the rules the mock device follows when a change has to be typed into
 * a terminal rather than handed to a process.
 *
 * A Claude Code session the shim attached runs inside a pseudo-terminal the
 * device owns, so `session.set` for the settings the agent names,
 * `session.command` for `/compact`, and `session.stop` (A42) are keystrokes:
 * they take a moment to land, they need a terminal that will read them, and
 * what the terminal will not take is refused rather than queued. These are
 * pure functions so the mock's behaviour can be read and tested without a
 * socket; `server.ts` holds the timers.
 */
import type { AgentInfo, Session, SharedSettingKey } from '../src/protocol/types';

/** §6.3: the four settings `session.set` carries besides the title. */
export const SETTING_KEYS: SharedSettingKey[] = ['model', 'permission_mode', 'effort', 'speed'];

/** What the device answers when it cannot get at the terminal. */
export const TERMINAL_BUSY = 'the terminal is busy; try again in a moment';

/** What the device answers when an Escape would land on an open prompt (A42). */
export const ANSWER_THE_PROMPT = 'answer the prompt first';

/** How long the device takes to type a change in and read the answer back. */
export const TYPING_MS = 1_500;

/** The settings this frame asks for, in the order the contract lists them. */
export const settingKeys = (frame: Record<string, unknown>): SharedSettingKey[] =>
  SETTING_KEYS.filter((key) => key in frame && frame[key] !== undefined);

/**
 * A11/A40 §6.3: of the settings this frame asks for, the ones a shared
 * session's attachment does not carry. `shared_settings_keys` names the subset
 * the device changes; its absence means all four, and `shared_settings` false
 * means none. A session no terminal holds locks nothing.
 */
export function lockedKeys(
  session: Session,
  agent: AgentInfo | undefined,
  frame: Record<string, unknown>,
): SharedSettingKey[] {
  if (session.control !== 'shared') return [];
  const wanted = settingKeys(frame);
  if (agent?.shared_settings !== true) return wanted;
  const allowed = agent.shared_settings_keys;
  return allowed === undefined ? [] : wanted.filter((key) => !allowed.includes(key));
}

/**
 * A shared session the device drives by typing into the pseudo-terminal its
 * shim gave the CLI — Claude's channel attachment, and no other. A daemon or a
 * leader takes a method call instead, which lands at once.
 */
export const typedSession = (session: Session, agent: AgentInfo | undefined): boolean =>
  session.control === 'shared' && agent?.attach === 'channel';

/** A turn is running or a dialog is open, so nobody else may type there. */
export const terminalBusy = (session: Session): boolean =>
  session.turn !== null ||
  session.state === 'running' ||
  session.state === 'needs_approval' ||
  session.state === 'needs_input';

/**
 * A42: the CLI is asking something, so the Escape that Stop types would answer
 * the prompt instead of ending the turn. Unlike a setting, Stop does not wait
 * for the terminal to fall idle — a running turn is exactly what it is for.
 */
export const promptOnScreen = (session: Session): boolean =>
  session.state === 'needs_approval' || session.state === 'needs_input';
