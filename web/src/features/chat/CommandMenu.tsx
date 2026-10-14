import { useEffect, type ReactNode } from 'react';
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import type { Command } from '../../protocol/types';
import { commandSections } from './commands';
import './commandMenu.css';

interface Props {
  /** The rows, already filtered by what has been typed, in the device's order. */
  rows: Command[];
  /** Index into `rows` of the row the keyboard is on. */
  highlight: number;
  /** Id of the list, for the field's `aria-controls`. */
  listId: string;
  /** Id of one row, shared with the field's `aria-activedescendant`. */
  optionId: (index: number) => string;
  /** A27: a command waits for the turn, so the rows dim and the footer says so. */
  running: boolean;
  onHighlight: (index: number) => void;
  onTake: (command: Command) => void;
}

/**
 * A27 — the terminal's `/` menu, above the composer. One row per command:
 * `/name` in the monospace face, the description after it, and the argument
 * placeholder at the trailing edge when the command takes one. Group headers
 * appear only when the agent distinguishes more than one group.
 *
 * The panel never takes focus: the field keeps it, so typing goes on filtering
 * the list, and the keyboard reaches the rows through `aria-activedescendant`.
 */
export function CommandMenu({
  rows,
  highlight,
  listId,
  optionId,
  running,
  onHighlight,
  onTake,
}: Props) {
  // Arrowing past the eighth row has to bring the row into view, in a list that
  // the field's own scrolling knows nothing about.
  useEffect(() => {
    const el = document.getElementById(optionId(highlight));
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' });
  }, [highlight, optionId]);

  const sections = commandSections(rows);

  const row = (command: Command): ReactNode => {
    const index = rows.indexOf(command);
    return (
      <li
        key={command.name}
        id={optionId(index)}
        role="option"
        aria-selected={index === highlight}
        aria-label={strings.commands.rowLabel(command.name, command.description)}
        className={cx('command-row', index === highlight && 'active', running && 'dim')}
        // The field must not lose focus to the click that takes a row.
        onMouseDown={(e) => e.preventDefault()}
        onMouseEnter={() => onHighlight(index)}
        onClick={() => onTake(command)}
      >
        <span className="mono command-name">/{command.name}</span>
        <span className="command-desc">{command.description}</span>
        {command.argument ? <span className="command-arg">{command.argument}</span> : null}
      </li>
    );
  };

  return (
    <div className="command-menu">
      <ul className="command-list" role="listbox" id={listId} aria-label={strings.commands.menu}>
        {sections.map((section) =>
          section.group === null ? (
            section.items.map(row)
          ) : (
            <li key={section.group} role="group" aria-label={section.group} className="command-group">
              <span className="command-group-name" aria-hidden>
                {section.group}
              </span>
              <ul className="command-group-rows" role="none">
                {section.items.map(row)}
              </ul>
            </li>
          ),
        )}
      </ul>
      {running ? <p className="command-foot">{strings.commands.whileRunning}</p> : null}
    </div>
  );
}

/**
 * A27 — what a complete first word says about the rest of the line: the command
 * and where its argument goes. It stands where the panel stood, so the field
 * does not jump when the panel closes on the space that follows the name.
 */
export function CommandHint({ command }: { command: Command }) {
  return (
    <div className="command-hint" role="note">
      <span className="mono command-name">/{command.name}</span>
      <span className="command-desc">{command.description}</span>
      {command.argument ? <span className="command-arg">{command.argument}</span> : null}
    </div>
  );
}
