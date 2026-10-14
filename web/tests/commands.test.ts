/**
 * A27 — reading the composer's draft as a slash command, and the list the
 * panel draws from it.
 */
import { describe, expect, it } from 'vitest';
import {
  commandQuery,
  commandSections,
  completionFor,
  filterCommands,
  matchCommand,
  typedCommand,
} from '../src/features/chat/commands';
import { commandsFor } from '../mock/fixtures';
import type { Command } from '../src/protocol/types';

const codex = commandsFor('codex');
const pi = commandsFor('pi');

describe('commandQuery', () => {
  it('opens on the slash alone, with the whole list behind it', () => {
    expect(commandQuery('/')).toBe('');
  });

  it('is the partial name while it is being typed', () => {
    expect(commandQuery('/com')).toBe('com');
    expect(commandQuery('/skill:pdf')).toBe('skill:pdf');
  });

  it('closes the moment the name is followed by anything', () => {
    expect(commandQuery('/review ')).toBeNull();
    expect(commandQuery('/review the diff')).toBeNull();
  });

  it('is null for ordinary text, including a slash inside it', () => {
    expect(commandQuery('')).toBeNull();
    expect(commandQuery('run the tests')).toBeNull();
    expect(commandQuery('look at src/lib/ws.ts')).toBeNull();
    expect(commandQuery('\n/compact')).toBeNull();
  });
});

describe('typedCommand', () => {
  it('splits the first word from the argument', () => {
    expect(typedCommand('/review focus on the retry logic')).toEqual({
      name: 'review',
      argument: 'focus on the retry logic',
    });
  });

  it('leaves the argument out when nothing but space followed', () => {
    expect(typedCommand('/compact')).toEqual({ name: 'compact' });
    expect(typedCommand('/compact   ')).toEqual({ name: 'compact' });
  });
});

describe('matchCommand', () => {
  it('matches only what the session offers', () => {
    expect(matchCommand(codex, '/compact')?.command.name).toBe('compact');
    // A terminal treats an unknown slash as text, and so do we.
    expect(matchCommand(codex, '/nonesuch')).toBeNull();
    expect(matchCommand(codex, 'compact')).toBeNull();
  });

  it('carries whatever was typed after the name', () => {
    const match = matchCommand(codex, '/review gateway/link.py only');
    expect(match?.command.name).toBe('review');
    expect(match?.argument).toBe('gateway/link.py only');
  });
});

describe('filterCommands', () => {
  it('is a prefix of the name, in the device’s own order', () => {
    expect(filterCommands(codex, '').map((c) => c.name)).toEqual(codex.map((c) => c.name));
    expect(filterCommands(codex, 's').map((c) => c.name)).toEqual(['status', 'skills']);
    expect(filterCommands(codex, 'ski').map((c) => c.name)).toEqual(['skills']);
    expect(filterCommands(codex, 'zz')).toEqual([]);
  });

  it('matches a namespaced name by its prefix', () => {
    expect(filterCommands(pi, 'skill:').map((c) => c.name)).toEqual([
      'skill:pdf-tables',
      'skill:sql-review',
    ]);
  });
});

describe('commandSections', () => {
  it('draws no header when the agent distinguishes one group or none', () => {
    expect(commandSections(codex)).toEqual([{ group: null, items: codex }]);
  });

  it('sections by group, in the order the groups first appear', () => {
    const sections = commandSections(pi);
    expect(sections.map((s) => s.group)).toEqual(['Built-in', 'Prompts', 'Skills', 'Extensions']);
    expect(sections.flatMap((s) => s.items)).toHaveLength(pi.length);
  });

  it('keeps a one-group list flat', () => {
    const one: Command[] = [{ name: 'compact', description: 'x', group: 'Built-in' }];
    expect(commandSections(one)).toEqual([{ group: null, items: one }]);
  });
});

describe('completionFor', () => {
  it('leaves a space only where an argument goes', () => {
    expect(completionFor({ name: 'compact', description: '' })).toBe('/compact');
    expect(completionFor({ name: 'review', description: '', argument: 'instructions' })).toBe(
      '/review ',
    );
  });
});
