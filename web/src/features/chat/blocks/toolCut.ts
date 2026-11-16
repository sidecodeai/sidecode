/**
 * A60: what the device left out of a tool call's copy. A brief page of history
 * cuts `input` to 1 KiB and leaves `output` and `diff.patch` out altogether, and
 * every event has the bounds of PROTOCOL 2.6; each cut is flagged. The whole
 * block is one `session.block` away.
 */
import type { ToolCallEvent } from '../../../protocol/types';

/** Whether any part of the block was cut, by a brief page or by the 2.6 bounds. */
export function isCut(event: ToolCallEvent): boolean {
  return (
    event.input_truncated === true ||
    event.output_truncated === true ||
    event.diff?.patch_truncated === true
  );
}

/**
 * Whether a part was left out altogether, so the opened row has nothing of it
 * to draw: an output or a patch a brief page did not carry.
 */
export function lacksContent(event: ToolCallEvent): boolean {
  const output = event.output === undefined && event.output_truncated === true;
  const patch = event.diff?.patch === undefined && event.diff?.patch_truncated === true;
  return output || patch;
}
