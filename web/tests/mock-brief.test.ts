/**
 * A60 — the brief page the mock device answers `session.history {brief: true}`
 * with: every tool call cut harder than 2.6, everything else as it was.
 */
import { describe, expect, it } from 'vitest';
import { BRIEF_INPUT_BYTES, briefEvent, briefInput } from '../mock/brief';
import type { SessionEvent, ToolCallEvent } from '../src/protocol/types';

const sizeOf = (value: unknown): number => new TextEncoder().encode(JSON.stringify(value)).length;

const shell: ToolCallEvent = {
  seq: 26,
  ts: 1_788_944_430_700,
  kind: 'tool_call',
  first_seq: 25,
  block_id: 'd026b2fb-c6fd-4a29-bbdc-34b98ee362bb',
  tool: 'Bash',
  tool_kind: 'shell',
  title: 'pytest -k refresh --count 20',
  status: 'failed',
  input: { command: 'pytest -k refresh --count 20', timeout: 120_000 },
  output: '=== test session starts ===\n2 failed\n',
  summary: '2 failed',
  started_at: 1_788_944_424_300,
  ended_at: 1_788_944_430_700,
  duration_ms: 6_400,
};

const edit: ToolCallEvent = {
  ...shell,
  seq: 23,
  block_id: '9d231cee-9a1c-48da-8f14-77c0746ced34',
  tool: 'Edit',
  tool_kind: 'edit',
  title: 'rc_gateway/auth/tokens.py',
  status: 'succeeded',
  input: { file_path: '/Users/me/dev/gateway/rc_gateway/auth/tokens.py' },
  output: undefined,
  summary: '+4 -2',
  diff: {
    path: 'rc_gateway/auth/tokens.py',
    additions: 4,
    deletions: 2,
    patch: '--- a/tokens.py\n+++ b/tokens.py\n-old\n+new\n',
  },
};

describe('a brief tool call', () => {
  it('leaves the output out and says it did', () => {
    const brief = briefEvent(shell) as ToolCallEvent;
    expect(brief.output).toBeUndefined();
    expect(brief.output_truncated).toBe(true);
  });

  it('leaves the patch out, keeps the path and the counts, and says it did', () => {
    const brief = briefEvent(edit) as ToolCallEvent;
    expect(brief.diff).toEqual({
      path: 'rc_gateway/auth/tokens.py',
      additions: 4,
      deletions: 2,
      patch_truncated: true,
    });
    // No output to begin with, so none was cut.
    expect(brief.output_truncated).toBeUndefined();
  });

  it('keeps an input that fits, uncut and unflagged', () => {
    const brief = briefEvent(shell) as ToolCallEvent;
    expect(brief.input).toEqual(shell.input);
    expect(brief.input_truncated).toBeUndefined();
  });

  it('cuts an input past 1 KiB to the bound, and flags it', () => {
    const long: ToolCallEvent = {
      ...shell,
      input: { command: 'x'.repeat(5_000), description: 'run it', timeout: 120_000 },
    };
    const brief = briefEvent(long) as ToolCallEvent;
    expect(sizeOf(brief.input)).toBeLessThanOrEqual(BRIEF_INPUT_BYTES);
    expect(brief.input_truncated).toBe(true);
    // The short values are what a row shows first, and they stay.
    expect(brief.input).toMatchObject({ description: 'run it', timeout: 120_000 });
  });

  it('drops keys from the end once no string is worth shortening', () => {
    const wide = Object.fromEntries(Array.from({ length: 200 }, (_, i) => [`k${i}`, i]));
    const { input, cut } = briefInput(wide);
    expect(sizeOf(input)).toBeLessThanOrEqual(BRIEF_INPUT_BYTES);
    expect(cut).toBe(true);
    expect(input).toHaveProperty('k0', 0);
  });

  it('changes nothing a row is drawn from', () => {
    const brief = briefEvent(shell) as ToolCallEvent;
    const { input: _input, output: _output, ...kept } = shell;
    expect(brief).toMatchObject(kept);
  });

  it('leaves every other kind of event as it was', () => {
    const text: SessionEvent = {
      seq: 30,
      ts: 1,
      kind: 'assistant_text',
      block_id: 'b30',
      text: 'y'.repeat(5_000),
      done: true,
    };
    expect(briefEvent(text)).toBe(text);
  });
});
