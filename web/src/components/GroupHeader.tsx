import { ChevronRight } from 'lucide-react';
import { cx } from '../lib/cx';
import { OnlineDot } from './StatusDot';
import { strings } from '../strings';

interface DeviceProps {
  name: string;
  online: boolean;
  expanded: boolean;
  onToggle: () => void;
  /**
   * While a search or a status is showing the group whatever the user folded,
   * a click would change nothing on screen and only flip the stored choice, so
   * the header takes none. It stays a button, marked inert for assistive
   * technology and drawn without the hover ink and the pointer.
   */
  locked?: boolean;
  className?: string;
}

/**
 * The header of one device's group. The Sessions page and the chat sidebar
 * share it so the two lists read the same way. The name is printed exactly as
 * the device reports it — never re-cased.
 */
export function DeviceGroupHeader({
  name,
  online,
  expanded,
  onToggle,
  locked = false,
  className,
}: DeviceProps) {
  return (
    <button
      type="button"
      className={cx('group-head device-head', className)}
      aria-expanded={expanded}
      aria-disabled={locked || undefined}
      onClick={locked ? undefined : onToggle}
    >
      <ChevronRight size={13} aria-hidden className={cx('group-chevron', expanded && 'open')} />
      <span className="device-head-name">{name}</span>
      <OnlineDot online={online} />
    </button>
  );
}

interface ArchiveProps {
  count: number;
  expanded: boolean;
  onToggle: () => void;
  /** Inert while a search or a status holds the Archive open, as the device header is. */
  locked?: boolean;
  className?: string;
}

/** One device's Archive, folded shut under its active rows. */
export function ArchiveGroupHeader({
  count,
  expanded,
  onToggle,
  locked = false,
  className,
}: ArchiveProps) {
  return (
    <button
      type="button"
      className={cx('group-head archive-head', className)}
      aria-expanded={expanded}
      aria-disabled={locked || undefined}
      onClick={locked ? undefined : onToggle}
    >
      <ChevronRight size={12} aria-hidden className={cx('group-chevron', expanded && 'open')} />
      {strings.sessions.archiveGroup(count)}
    </button>
  );
}
