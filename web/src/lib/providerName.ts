/**
 * The name of the provider an AI feature sends to, as a gateway says it: what
 * the permission for that feature names, so the person knows who receives what
 * they say or write. A gateway that names none, or sends anything other than a
 * short line of text, leaves the permission in its unnamed words.
 */

/** The longest name the protocol allows; a longer one is not a provider's name. */
const MAX_NAME = 40;

const isControl = (char: string): boolean => char < ' ' || char === '\u007f';

export function providerName(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const name = value.trim();
  if (name.length === 0 || name.length > MAX_NAME) return null;
  return [...name].some(isControl) ? null : name;
}
