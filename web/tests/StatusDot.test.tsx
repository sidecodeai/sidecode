/**
 * How a session's dot looks, against the frozen table in `docs/DESIGN.md`
 * § "The status dot": green means working — leave it; amber means there is
 * something for you. Only `waiting` animates, and Reduce Motion stops it.
 *
 * The tone rule itself is `tests/dotTone.test.ts`; this suite is the looks, so
 * it reads `src/components/ui.css` — a component test cannot see a stylesheet
 * jsdom never loads — plus the class each tone actually renders, which is what
 * ties the markup to those rules.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { StatusDot } from '../src/components/StatusDot';
import { strings } from '../src/strings';
import { stringTables } from '../src/strings';
import type { ControlOwner, SessionState } from '../src/protocol/types';

const css = readFileSync(join(process.cwd(), 'src', 'components', 'ui.css'), 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
);

/** The body of the rule or at-rule introduced by `header`, braces balanced. */
function block(header: string): string {
  const start = css.indexOf(`${header} {`);
  expect(start, `${header} is not in ui.css`).toBeGreaterThan(-1);
  let depth = 0;
  for (let i = css.indexOf('{', start); i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}') {
      depth -= 1;
      if (depth === 0) return css.slice(css.indexOf('{', start) + 1, i);
    }
  }
  throw new Error(`${header} has no closing brace`);
}

/** The class list the dot renders for a state on an online device. */
function classes(state: SessionState, control: ControlOwner): string[] {
  const { container } = render(<StatusDot state={state} control={control} />);
  return [...(container.firstElementChild as HTMLElement).classList];
}

/** The tooltip the dot carries for a state on an online device. */
function tooltip(state: SessionState, control: ControlOwner): string | null {
  const { container } = render(<StatusDot state={state} control={control} />);
  return (container.firstElementChild as HTMLElement).getAttribute('title');
}

describe('the status dot', () => {
  it('runs green and still, so a working session asks for nothing', () => {
    expect(classes('running', 'remote')).toEqual(['dot', 'working']);
    expect(block('.dot.working')).toContain('background: var(--running)');
    expect(block('.dot.working')).not.toContain('animation');
  });

  it('pulses amber while it is blocked on the person', () => {
    expect(classes('needs_approval', 'remote')).toEqual(['dot', 'waiting']);
    expect(classes('needs_input', 'remote')).toEqual(['dot', 'waiting']);
    expect(block('.dot.waiting')).toContain('background: var(--attention)');
    expect(block('.dot.waiting')).toContain('rc-pulse');
  });

  it('turns amber and still once a turn has finished', () => {
    expect(classes('idle', 'remote')).toEqual(['dot', 'live']);
    expect(block('.dot.live')).toContain('background: var(--attention)');
    expect(block('.dot.live')).not.toContain('animation');
  });

  it('leaves an exited CLI and a failure where they were', () => {
    expect(classes('idle', 'none')).toEqual(['dot', 'off']);
    expect(block('.dot.off')).toContain('background: var(--idle)');
    expect(block('.dot.failed')).toContain('background: var(--danger)');
  });

  it('stops the one animation under Reduce Motion, and no other tone', () => {
    const reduced = block('@media (prefers-reduced-motion: reduce)');

    expect(reduced).toContain('.dot.waiting');
    expect(reduced).not.toContain('.dot.working');
    expect(reduced).toContain('animation: none');
  });

  it('calls a finished session Done rather than Live', () => {
    expect(tooltip('idle', 'remote')).toBe('Done');
    expect(strings.labels.dotTone.live).toBe('Done');
    expect(stringTables['zh-Hans'].labels.dotTone.live).toBe('已完成');
  });
});
