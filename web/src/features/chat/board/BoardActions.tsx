/**
 * A62 — the panel's two controls beside its title: Refresh, and a menu with
 * Start over and Turn off in Settings. The menu opens inside the panel rather
 * than as a popover of its own, which a press would take for one outside the
 * panel and close both.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router';
import { MoreHorizontal, RefreshCw } from 'lucide-react';
import { strings } from '../../../strings';
import { sectionPath } from '../../settings/sections';

interface Props {
  /** A run is out or scheduled: Refresh would add nothing. */
  updating: boolean;
  onRefresh: () => void;
  onStartOver: () => void;
  /** Turn off in Settings was followed. */
  onLeave: () => void;
}

export function BoardActions({ updating, onRefresh, onStartOver, onLeave }: Props) {
  const [menu, setMenu] = useState(false);
  const menuId = useId();
  const s = strings.chat.board;

  return (
    <div className="board-actions">
      <button
        type="button"
        className="icon-btn"
        aria-label={s.refresh}
        title={s.refresh}
        disabled={updating}
        onClick={onRefresh}
      >
        <RefreshCw size={14} aria-hidden />
      </button>
      <button
        type="button"
        className="icon-btn"
        aria-label={strings.a11y.openMenu}
        title={strings.a11y.openMenu}
        aria-haspopup="menu"
        aria-expanded={menu}
        aria-controls={menu ? menuId : undefined}
        onClick={() => setMenu((open) => !open)}
      >
        <MoreHorizontal size={15} aria-hidden />
      </button>
      {menu ? (
        <BoardMenu
          id={menuId}
          onStartOver={() => {
            setMenu(false);
            onStartOver();
          }}
          onLeave={onLeave}
        />
      ) : null}
    </div>
  );
}

function BoardMenu({ id, onStartOver, onLeave }: { id: string; onStartOver: () => void; onLeave: () => void }) {
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => first.current?.focus(), []);
  const s = strings.chat.board;

  return (
    <ul className="board-menu" id={id} role="menu" aria-label={s.title}>
      <li role="none">
        <button type="button" role="menuitem" className="menu-item" ref={first} onClick={onStartOver}>
          <span className="menu-label">{s.startOver}</span>
        </button>
      </li>
      <li role="none">
        <Link role="menuitem" className="menu-item" to={sectionPath('general')} onClick={onLeave}>
          <span className="menu-label">{s.turnOff}</span>
        </Link>
      </li>
    </ul>
  );
}
