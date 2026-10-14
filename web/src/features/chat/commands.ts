/**
 * A27 — reading the composer's draft as a slash command.
 *
 * Everything here is pure, so the panel, the send routing and the tests all
 * read the draft the same way. The names come from the device and obey
 * `Command.name` of PROTOCOL.md §4.11: lower case, `[a-z0-9_:.-]`, never with
 * the slash.
 */
import type { Command } from '../../protocol/types';

/** The draft while the name is still being typed: a slash and nothing after it. */
const QUERY = /^\/([a-z0-9_:.-]*)$/i;

/** A complete first word, and whatever was typed after it. */
const TYPED = /^\/([a-z0-9_:.-]+)(?:[ \t]+([\s\S]*))?$/i;

export interface TypedCommand {
  name: string;
  /** Absent when nothing but whitespace followed the name. */
  argument?: string;
}

/**
 * The partial name the panel filters on, or null when the draft is not a
 * command being typed. `/` alone is an empty query, which is the whole list —
 * that is the keystroke the panel opens on.
 */
export function commandQuery(draft: string): string | null {
  const match = QUERY.exec(draft);
  return match ? (match[1] ?? '').toLowerCase() : null;
}

/** The draft read as `/name argument`, whether or not any agent offers it. */
export function typedCommand(draft: string): TypedCommand | null {
  const match = TYPED.exec(draft);
  if (!match) return null;
  const argument = (match[2] ?? '').trim();
  return {
    name: (match[1] ?? '').toLowerCase(),
    ...(argument.length > 0 ? { argument } : {}),
  };
}

export interface CommandMatch {
  command: Command;
  argument?: string;
}

/**
 * The listed command a draft names, or null. Anything the session does not
 * offer is ordinary text, which is how a terminal treats an unknown slash.
 */
export function matchCommand(
  commands: readonly Command[],
  draft: string,
): CommandMatch | null {
  const typed = typedCommand(draft);
  if (!typed) return null;
  const command = commands.find((c) => c.name.toLowerCase() === typed.name);
  if (!command) return null;
  return {
    command,
    ...(typed.argument !== undefined ? { argument: typed.argument } : {}),
  };
}

/** Prefix of the name, in the order the device listed them. §4.11. */
export function filterCommands(commands: readonly Command[], query: string): Command[] {
  const prefix = query.toLowerCase();
  if (prefix.length === 0) return [...commands];
  return commands.filter((command) => command.name.toLowerCase().startsWith(prefix));
}

export interface CommandSection {
  /** Null when the list is not sectioned, or for commands with no group. */
  group: string | null;
  items: Command[];
}

/**
 * The rows in the order they are drawn, sectioned by group only when the agent
 * distinguishes more than one — a single header says nothing about anything.
 * Sections keep the order the groups first appear in, so the device's own
 * ordering survives.
 */
export function commandSections(commands: readonly Command[]): CommandSection[] {
  const order: (string | null)[] = [];
  const byGroup = new Map<string | null, Command[]>();
  for (const command of commands) {
    const group = command.group ?? null;
    if (!byGroup.has(group)) {
      byGroup.set(group, []);
      order.push(group);
    }
    byGroup.get(group)?.push(command);
  }
  if (order.length <= 1) return [{ group: null, items: [...commands] }];
  return order.map((group) => ({ group, items: byGroup.get(group) ?? [] }));
}

/**
 * What taking a row writes into the field: `/name ` when the command takes an
 * argument, so the hint shows where it goes, and `/name` when it does not, so
 * a second Enter runs it.
 */
export function completionFor(command: Command): string {
  return command.argument ? `/${command.name} ` : `/${command.name}`;
}
