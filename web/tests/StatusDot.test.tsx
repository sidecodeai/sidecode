/**
 * How a session's dot looks, against the frozen table in `docs/DESIGN.md`
 * § "The status dot": green means working and breathes, so a running session is
 * easy to find; amber means there is something for you, and pulses while a
 * question waits. Reduce Motion stops both and holds the green's glow at rest.
 *
 * The tone rule itself is `tests/dotTone.test.ts`; this suite is the looks, so
 * it reads `src/components/ui.css` — a component test cannot see a stylesheet
 * jsdom never loads — plus the class each tone actually renders, which is what
 * ties the markup to those rules.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { OnlineDot, StatusDot } from '../src/components/StatusDot';
import { StatusLine } from '../src/features/chat/StatusLine';
import { SessionLegend } from '../src/features/sessions/SessionLegend';
import { IdentityHeader } from '../src/features/settings/IdentityHeader';
import { useConnection } from '../src/stores/connection';
import { strings } from '../src/strings';
import { stringTables } from '../src/strings';
import { claudeAgent, sessions } from '../mock/fixtures';
import type { ControlOwner, Session, SessionState } from '../src/protocol/types';

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

/** The class list the dot renders for a state, on an online device unless told otherwise. */
function classes(state: SessionState, control: ControlOwner, online = true): string[] {
  const { container } = render(<StatusDot state={state} control={control} online={online} />);
  return [...(container.firstElementChild as HTMLElement).classList];
}

/** The tooltip the dot carries for a state on an online device. */
function tooltip(state: SessionState, control: ControlOwner): string | null {
  const { container } = render(<StatusDot state={state} control={control} />);
  return (container.firstElementChild as HTMLElement).getAttribute('title');
}

describe('the status dot', () => {
  it('runs green and breathes, so a working session is easy to find', () => {
    expect(classes('running', 'remote')).toEqual(['dot', 'working', 'breathes']);
    expect(classes('starting', 'remote')).toEqual(['dot', 'working', 'breathes']);
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

  it('calls a finished session Done rather than Live', () => {
    expect(tooltip('idle', 'remote')).toBe('Done');
    expect(strings.labels.dotTone.live).toBe('Done');
    expect(stringTables['zh-Hans'].labels.dotTone.live).toBe('已完成');
  });
});

describe('the breath of a working session', () => {
  it('dims the dot and swells a glow of its own colour, once in 1.8 s on an ease-in-out', () => {
    const dot = block('.dot.breathes');
    expect(dot).toContain('position: relative');
    expect(dot).toContain('animation: rc-breathe-core 1.8s ease-in-out infinite');

    const glow = block('.dot.breathes::after');
    expect(glow).toContain('position: absolute');
    expect(glow).toContain('inset: 0');
    expect(glow).toContain('border-radius: 50%');
    expect(glow).toContain('background: inherit');
    expect(glow).toContain('pointer-events: none');
    expect(glow).toContain('animation: rc-breathe-glow 1.8s ease-in-out infinite');
  });

  it('runs the dot between 0.7 and 1, and the glow between nothing and 0.3 at 2.2 times its width', () => {
    const core = block('@keyframes rc-breathe-core');
    expect(core).toContain('opacity: 0.7');
    expect(core).toContain('opacity: 1');

    const glow = block('@keyframes rc-breathe-glow');
    expect(glow).toContain('opacity: 0;');
    expect(glow).toContain('transform: scale(1)');
    expect(glow).toContain('opacity: 0.3');
    expect(glow).toContain('transform: scale(2.2)');
  });

  it('moves opacity and transform alone, which the compositor runs without layout or paint', () => {
    const animated = (name: string): string[] =>
      [...new Set([...block(`@keyframes ${name}`).matchAll(/([a-z-]+)\s*:/g)].map((m) => m[1]))].sort() as string[];

    expect(animated('rc-breathe-core')).toEqual(['opacity']);
    expect(animated('rc-breathe-glow')).toEqual(['opacity', 'transform']);
  });

  it('stops every animation under Reduce Motion and holds the glow at rest', () => {
    const reduced = block('@media (prefers-reduced-motion: reduce)');

    expect(reduced).toContain('.dot.waiting');
    expect(reduced).toContain('.dot.breathes,');
    expect(reduced).toContain('.dot.breathes::after');
    expect(reduced).toContain('animation: none');
    // The static but still distinct indicator: the ring is there, it does not move.
    expect(reduced).toContain('transform: scale(1.8)');
    expect(reduced).toContain('opacity: 0.22');
  });

  it('never moves a tone through its own class, only through the one modifier', () => {
    const reduced = block('@media (prefers-reduced-motion: reduce)');

    for (const tone of ['working', 'live', 'off', 'failed']) {
      expect(reduced).not.toContain(`.dot.${tone}`);
    }
    expect(block('.dot.working')).not.toContain('animation');
  });

  it('is asked for by a working session alone', () => {
    const quiet: [SessionState, ControlOwner][] = [
      ['needs_approval', 'remote'],
      ['needs_input', 'remote'],
      ['idle', 'remote'],
      ['idle', 'none'],
      ['readonly', 'terminal'],
      ['stopped', 'none'],
      ['error', 'remote'],
    ];
    for (const [state, control] of quiet) {
      expect(classes(state, control), `${state}/${control}`).not.toContain('breathes');
    }
  });

  it('goes quiet with the device, which says nothing about what it last reported', () => {
    expect(classes('running', 'remote', false)).toEqual(['dot', 'off']);
  });

  it('is asked for by the chat status line while a turn runs, and only then', () => {
    const flaky = sessions.find((s) => s.session_id === 'ses-flaky') as Session;
    const line = (session: Session) => {
      const { container } = render(
        <StatusLine session={session} agent={claudeAgent} deviceOnline onTakeover={vi.fn()} />,
      );
      return [...(container.querySelector('.status-line .dot') as HTMLElement).classList];
    };

    expect(line(flaky)).toEqual(['dot', 'running', 'breathes']);
    expect(line({ ...flaky, state: 'starting' })).toEqual(['dot', 'running', 'breathes']);
    expect(line({ ...flaky, state: 'needs_approval' })).toEqual(['dot', 'attention']);
    expect(line({ ...flaky, state: 'error' })).toEqual(['dot', 'error']);
    expect(line({ ...flaky, state: 'stopped' })).toEqual(['dot']);
  });

  it('is left off the dots that are not a session at work', () => {
    const classesOf = (element: Element | null): string[] => [...(element as HTMLElement).classList];

    // The key to the colours says what the green means; nothing in it moves.
    const legend = render(<SessionLegend />).container;
    expect(legend.querySelector('.breathes')).toBeNull();
    expect(classesOf(legend.querySelector('.dot.working'))).toEqual(['dot', 'working']);

    // The Settings header's dot is a connection, green while it is open.
    useConnection.setState({ status: 'open' });
    const header = render(<IdentityHeader />).container;
    expect(classesOf(header.querySelector('.settings-identity .dot'))).toEqual(['dot', 'working']);

    // A device's own dot is green while it is online, and pulses while it updates.
    const online = render(<OnlineDot online pulse />).container;
    expect(classesOf(online.firstElementChild)).toEqual(['dot', 'running', 'pulse']);
  });
});
