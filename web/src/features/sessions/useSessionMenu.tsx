import type { HTMLAttributes, ReactNode } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { useContextMenu } from '../../components/useContextMenu';
import { sessionTitle, strings } from '../../strings';
import type { Session } from '../../protocol/types';

/**
 * The context menu of a session row, in the Sessions list and in the chat
 * sidebar alike (A55): Rename, and nothing else — Close stays the row's own
 * control. `onRename` is present only where the device can rename the session;
 * without it the row has no menu and the browser's own is left alone. The row
 * spreads `triggerProps` on its element and draws `menu` inside it.
 */
export function useSessionMenu(
  session: Session,
  onRename: ((session: Session) => void) | undefined,
): { triggerProps: HTMLAttributes<HTMLElement>; menu: ReactNode } {
  const { at, anchor, close, triggerProps } = useContextMenu(onRename !== undefined);
  const menu =
    at && anchor && onRename ? (
      <ContextMenu
        at={at}
        anchor={anchor}
        label={sessionTitle(session)}
        items={[
          { id: 'rename', label: strings.common.rename, onSelect: () => onRename(session) },
        ]}
        onClose={close}
      />
    ) : null;
  return { triggerProps, menu };
}
