import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router';
import { focusFirstItem, moveMenuFocus } from '../../components/menuKeys';
import { placementFor, type Align, type Side } from '../../components/popoverPlacement';
import { strings } from '../../strings';
import type { AccountSummary } from './useAccountSummary';
import '../../components/popover.css';
import './accountMenu.css';

interface Props {
  /** The menu's id, which the button's `aria-controls` names. */
  id: string;
  /** The account button the menu belongs to. */
  anchor: RefObject<HTMLButtonElement | null>;
  side: Side;
  align: Align;
  summary: AccountSummary;
  /** Closes the menu, with focus back on the button or left where a press put it. */
  onClose: (returnFocus: boolean) => void;
  onSignOut: () => void;
}

/**
 * The account menu, on the body beside its button: who is signed in and how,
 * then Settings, and Sign out last. A menu as the keyboard expects one (`menuKeys.ts`); Escape, Tab and choosing an item
 * close it with focus back on the button, and a press outside closes it. So
 * do a resize, which may take the button away (the sidebar is not drawn below
 * 1024 px), and the window losing focus, as a context menu closes for both.
 */
export function AccountMenu({ id, anchor, side, align, summary, onClose, onSignOut }: Props) {
  const panel = usePlacedPanel(anchor, align, side);
  const nameId = useId();
  const detailId = useId();

  useEffect(() => focusFirstItem(panel.current), [panel]);

  useEffect(() => {
    const onDown = (e: MouseEvent): void => {
      const target = e.target as Node;
      // The button's own press is its click's: that closes the menu.
      if (panel.current?.contains(target) || anchor.current?.contains(target)) return;
      onClose(false);
    };
    const onKey = (e: globalThis.KeyboardEvent): void => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      onClose(true);
    };
    // Focus goes back to the button, which takes none where it is no longer drawn.
    const onAway = (): void => onClose(true);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onAway);
    window.addEventListener('blur', onAway);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onAway);
      window.removeEventListener('blur', onAway);
    };
  }, [panel, anchor, onClose]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== 'Tab') {
      moveMenuFocus(e, panel.current);
      return;
    }
    // A menu is not a tab stop: leaving it closes it, back on the button.
    e.preventDefault();
    onClose(true);
  };

  const picked = (): void => onClose(true);

  return createPortal(
    <div className="popover-panel account-menu" ref={panel} onKeyDown={onKeyDown}>
      {/* The head names the menu, and is no item of it; each line is named
          apart, so the two are read as two words whatever the styles. */}
      <div className="account-menu-head">
        <span className="account-menu-name" id={nameId}>
          {summary.name}
        </span>
        {summary.detail ? (
          <span className="account-menu-meta" id={detailId}>
            {summary.detail}
          </span>
        ) : null}
      </div>
      <ul
        id={id}
        className="menu"
        role="menu"
        aria-labelledby={summary.detail ? `${nameId} ${detailId}` : nameId}
      >
        <MenuLink to="/settings" label={strings.nav.settings} onPick={picked} />
        <li role="separator" className="account-menu-rule" />
        <li role="none">
          <button
            type="button"
            role="menuitem"
            className="menu-item account-menu-danger"
            onClick={() => {
              // Focus is back on the button first, so the dialog hands it back there.
              onClose(true);
              onSignOut();
            }}
          >
            <span className="menu-label">{strings.settings.signOut}</span>
          </button>
        </li>
      </ul>
    </div>,
    document.body,
  );
}

interface MenuLinkProps {
  to: string;
  label: string;
  onPick: () => void;
}

function MenuLink({ to, label, onPick }: MenuLinkProps) {
  return (
    <li role="none">
      <Link role="menuitem" className="menu-item" to={to} onClick={onPick}>
        <span className="menu-label">{label}</span>
      </Link>
    </li>
  );
}

/**
 * The panel's ref, and the panel placed against the button before the browser
 * paints and again on a scroll under it, as a popover is. A resize closes it.
 */
function usePlacedPanel(
  anchor: RefObject<HTMLButtonElement | null>,
  align: Align,
  side: Side,
): RefObject<HTMLDivElement | null> {
  const panel = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const place = (): void => {
      const button = anchor.current;
      const el = panel.current;
      if (!button || !el) return;
      const at = placementFor(
        button.getBoundingClientRect(),
        el.getBoundingClientRect(),
        { width: window.innerWidth, height: window.innerHeight },
        align,
        side,
      );
      el.style.left = `${at.left}px`;
      el.style.top = at.top === null ? '' : `${at.top}px`;
      el.style.bottom = at.bottom === null ? '' : `${at.bottom}px`;
    };
    place();
    window.addEventListener('scroll', place, true);
    return () => window.removeEventListener('scroll', place, true);
  }, [anchor, align, side]);
  return panel;
}
