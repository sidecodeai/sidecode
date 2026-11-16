/**
 * A60 — the brief page the mock device answers `session.history {brief: true}`
 * with, so what `npm run dev:mock` does can be read without a socket. Every
 * tool call is cut harder than PROTOCOL 2.6: `input` keeps at most 1 KiB of
 * JSON, `output` and `diff.patch` are left out, and a flag says each cut.
 * Titles, names, status, ids, `block_id`, `seq`, the diff's path and counts and
 * every other kind of event are as they were. `session.block` still answers
 * the whole block.
 */
import type { SessionEvent, ToolCallEvent } from '../src/protocol/types';

/** The most JSON a brief page keeps of a tool call's input, in bytes. */
export const BRIEF_INPUT_BYTES = 1024;

/** What a string value is cut down to before keys start to go. */
const SHORTEST_CUT = 64;

const encoder = new TextEncoder();
const sizeOf = (value: unknown): number => encoder.encode(JSON.stringify(value)).length;

/** The longest string value of an input, the first thing a cut shortens. */
function longestString(input: Record<string, unknown>): [string, string] | null {
  let found: [string, string] | null = null;
  for (const [key, value] of Object.entries(input)) {
    if (typeof value === 'string' && (found === null || value.length > found[1].length)) {
      found = [key, value];
    }
  }
  return found;
}

/**
 * An input kept to the bound the way a device keeps one: the longest string
 * halves, with a marker, until the JSON fits, and once no string is worth
 * shortening the last keys go.
 */
export function briefInput(input: Record<string, unknown>): {
  input: Record<string, unknown>;
  cut: boolean;
} {
  let kept: Record<string, unknown> = { ...input };
  let cut = false;
  while (sizeOf(kept) > BRIEF_INPUT_BYTES) {
    cut = true;
    const longest = longestString(kept);
    if (longest && longest[1].length > SHORTEST_CUT) {
      const [key, text] = longest;
      kept = { ...kept, [key]: `${text.slice(0, Math.floor(text.length / 2))}…` };
      continue;
    }
    const last = Object.keys(kept).at(-1);
    if (last === undefined) break;
    kept = { ...kept };
    delete kept[last];
  }
  return { input: kept, cut };
}

/** One event of a brief page: a tool call cut, anything else as it was. */
export function briefEvent(event: SessionEvent): SessionEvent {
  if (event.kind !== 'tool_call') return event;
  const { input, output, diff, ...rest } = event;
  const brief: ToolCallEvent = { ...rest };
  if (input !== undefined) {
    const kept = briefInput(input);
    brief.input = kept.input;
    if (kept.cut) brief.input_truncated = true;
  }
  if (output) brief.output_truncated = true;
  if (diff) {
    const { patch, ...stat } = diff;
    brief.diff = patch ? { ...stat, patch_truncated: true } : stat;
  }
  return brief;
}
