/**
 * A55: the rules of renaming a session from the browser that are not drawing —
 * when Rename is offered, what stands between the text in the field and Save,
 * and the line a refusal becomes. The device is authoritative for the name
 * (it normalises, may suffix, and answers with what the agent now holds); what
 * is checked here is only what saves a round trip.
 */
import { errorText } from '../../lib/errors';
import { RequestError } from '../../lib/ws';
import { strings } from '../../strings';
import type { AgentInfo, Device, Session } from '../../protocol/types';

/** The most a name can be, counted in code points as the device counts it. */
export const NAME_LIMIT = 60;

/** `[...text].length`: an emoji is one, where `text.length` would say two. */
export const codePointCount = (text: string): number => [...text].length;

/**
 * Whether Rename is offered for a session whose agent is `agent` on its device.
 * The device lists `rename` for an agent whose sessions it can name in the
 * agent itself, and one from before A55 does not, so the capability is the
 * only question: never a version. An offline device cannot be asked.
 */
export function canRename(agent: AgentInfo | null | undefined, online: boolean): boolean {
  return online && (agent?.capabilities.includes('rename') ?? false);
}

/** `canRename` for a row of a list, which knows its device and not its agent. */
export function deviceCanRename(device: Device, session: Session): boolean {
  return canRename(
    device.agents.find((agent) => agent.agent === session.agent),
    device.online,
  );
}

/**
 * A code point the device turns into a space before it trims a name: control,
 * format, surrogate, line and paragraph separator characters, and any white
 * space. The two format characters that join emoji sequences, U+200C and
 * U+200D, are the exception: the device keeps them.
 */
const BLANK = /^(?:[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]|(?![\u200C\u200D])\p{Cf})$/u;
const isBlank = (point: string): boolean => BLANK.test(point) || point.trim() === '';

/** Where the name starts and ends in a text's code points: the first and last that are not blank. */
function spanOf(text: string): { points: string[]; start: number; end: number } {
  const points = [...text];
  const start = points.findIndex((point) => !isBlank(point));
  if (start === -1) return { points, start: 0, end: 0 };
  return { points, start, end: points.findLastIndex((point) => !isBlank(point)) + 1 };
}

/**
 * The name a text holds: both ends trimmed as the device trims them, so a text
 * of nothing but space, control and zero-width characters holds none. This is
 * what is counted, compared and sent.
 */
export function trimName(text: string): string {
  const { points, start, end } = spanOf(text);
  return points.slice(start, end).join('');
}

/**
 * Keeps a field to the limit the way `maxLength` does, in code points of the
 * name: an edit that would grow the name past it is cut at it, and one that does
 * not grow it is let through, so a longer name the agent already holds can still
 * be edited down. The cut is made at the end, where typing and a paste into an
 * empty field put what overflows.
 */
export function keepToLimit(next: string, previous: string): string {
  const { points, start, end } = spanOf(next);
  const length = end - start;
  if (length <= NAME_LIMIT || length <= codePointCount(trimName(previous))) return next;
  return points.slice(0, start + NAME_LIMIT).join('');
}

/** What stands between the text in the field and a request. */
export type NameCheck = 'ok' | 'empty' | 'unchanged' | 'too-long';

/** The name, trimmed as the device trims it, decides: it is what Save sends. */
export function checkName(value: string, current: string): NameCheck {
  const name = trimName(value);
  if (name === '') return 'empty';
  if (codePointCount(name) > NAME_LIMIT) return 'too-long';
  if (name === trimName(current)) return 'unchanged';
  return 'ok';
}

/**
 * The one line under the field after a refusal. `conflict` here means one
 * thing, a busy terminal, and `unsupported` means the session cannot be named
 * from here, so each has a sentence of its own in place of the canned
 * "controlled by the terminal" and "not supported by this device". Any other
 * code keeps the app's words for it: offline, timeout, or the device's own.
 */
export function renameFailure(error: unknown): string {
  if (error instanceof RequestError) {
    if (error.code === 'conflict') return strings.sessions.renameBusy;
    if (error.code === 'unsupported') return strings.sessions.renameUnsupported;
  }
  return errorText(error);
}
