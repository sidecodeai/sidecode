import type { Ref } from 'react';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { TooltipButton } from '../../components/TooltipButton';
import { strings } from '../../strings';
import { sidebarShortcut } from './useSidebarFold';

interface Props {
  collapsed: boolean;
  /** Sessions waiting for the person: folded, a dot on the toggle and a word in its name. */
  waiting: number;
  /** The sidebar's id. */
  controls: string;
  onToggle: () => void;
  ref?: Ref<HTMLButtonElement>;
}

/**
 * The sidebar's own toggle, top left and in the same place open and folded, so
 * the same button undoes what it did. Folded, it carries what the footer's
 * count would say: a dot in the attention colour while anything waits.
 */
export function SidebarToggle({ collapsed, waiting, controls, onToggle, ref }: Props) {
  const shortcut = sidebarShortcut();
  const flagged = collapsed && waiting > 0;
  const label = !collapsed
    ? strings.chat.closeSidebar
    : flagged
      ? strings.chat.openSidebarWaiting(waiting)
      : strings.chat.openSidebar;

  return (
    <TooltipButton
      ref={ref}
      className="sidebar-action sidebar-toggle"
      label={label}
      side={collapsed ? 'right' : 'bottom'}
      shortcut={shortcut.label}
      aria-expanded={!collapsed}
      aria-controls={controls}
      aria-keyshortcuts={shortcut.aria}
      onClick={onToggle}
    >
      {collapsed ? <PanelLeftOpen size={20} aria-hidden /> : <PanelLeftClose size={20} aria-hidden />}
      {flagged ? <span className="sidebar-toggle-dot" aria-hidden /> : null}
    </TooltipButton>
  );
}
