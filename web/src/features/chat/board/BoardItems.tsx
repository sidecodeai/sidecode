/**
 * A62 — the board's items under Doing, To do and Done recently, each one line of
 * plain text that wraps, behind a quiet glyph: a filled dot for doing, a hollow
 * circle for to do, a check for done, with done items in the secondary ink. The
 * text is the model's and the conversation's, so it is drawn as text and nothing
 * else: no Markdown, no links, no markup.
 */
import { useId } from 'react';
import { Check } from 'lucide-react';
import type { Board, BoardItemStatus } from '../../../protocol/types';
import { strings } from '../../../strings';
import { groupsOf, type BoardGroup } from './boardView';

const groupTitle = (status: BoardItemStatus): string => strings.chat.board[status];

export function BoardItems({ board }: { board: Board }) {
  return (
    <div className="board-scroll">
      {groupsOf(board).map((group) => (
        <Group key={group.status} group={group} />
      ))}
    </div>
  );
}

function Group({ group }: { group: BoardGroup }) {
  const titleId = useId();
  return (
    <div className="board-group">
      <h3 className="board-group-title" id={titleId}>
        {groupTitle(group.status)}
      </h3>
      <ul className="board-items" aria-labelledby={titleId}>
        {group.items.map((item) => (
          <li key={item.id} className={`board-item ${item.status}`}>
            <span className={`board-glyph ${item.status}`} aria-hidden>
              {item.status === 'done' ? <Check size={13} /> : null}
            </span>
            <span className="board-item-text">{item.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
