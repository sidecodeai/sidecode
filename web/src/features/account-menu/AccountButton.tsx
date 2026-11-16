import { useCallback, useId, useRef, useState } from 'react';
import type { Align, Side } from '../../components/popoverPlacement';
import { strings } from '../../strings';
import { AccountMenu } from './AccountMenu';
import { SignOutDialog } from './SignOutDialog';
import { useAccountSummary } from './useAccountSummary';
import './accountMenu.css';

/**
 * Where the button sits: the foot of the open chat sidebar, which names the
 * account beside the initials; the foot of its rail; or the top bar, both of
 * which draw the initials alone.
 */
export type AccountButtonPlace = 'sidebar' | 'rail' | 'topbar';

/** Up from the sidebar's foot; down and back from the top bar's trailing edge. */
const OPENS: Record<AccountButtonPlace, { side: Side; align: Align }> = {
  sidebar: { side: 'top', align: 'start' },
  rail: { side: 'top', align: 'start' },
  topbar: { side: 'bottom', align: 'end' },
};

/**
 * Who is signed in, and the menu of what belongs to the account. The same
 * button stays mounted when the sidebar folds to its rail, so focus and an open
 * menu stay with it.
 */
export function AccountButton({ place }: { place: AccountButtonPlace }) {
  const summary = useAccountSummary();
  const button = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const menuId = useId();
  const nameId = useId();
  const secondaryId = useId();
  const named = place === 'sidebar';

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) button.current?.focus({ preventScroll: true });
  }, []);

  return (
    <>
      <button
        ref={button}
        type="button"
        className="account-button"
        data-place={place}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        // Where the name is printed, its lines are the button's name, read as
        // two words whatever the styles; the initials alone are not one.
        aria-label={named ? undefined : strings.accountMenu.button(summary.name)}
        aria-labelledby={
          named ? (summary.secondary ? `${nameId} ${secondaryId}` : nameId) : undefined
        }
        onClick={() => setOpen((was) => !was)}
      >
        <span className="avatar" aria-hidden>
          {summary.initials}
        </span>
        {named ? (
          <span className="account-button-text">
            <span className="account-button-name" id={nameId}>
              {summary.name}
            </span>
            {summary.secondary ? (
              <span className="account-button-meta" id={secondaryId}>
                {summary.secondary}
              </span>
            ) : null}
          </span>
        ) : null}
      </button>
      {open ? (
        <AccountMenu
          id={menuId}
          anchor={button}
          {...OPENS[place]}
          summary={summary}
          onClose={close}
          onSignOut={() => setSigningOut(true)}
        />
      ) : null}
      <SignOutDialog open={signingOut} onClose={() => setSigningOut(false)} />
    </>
  );
}
