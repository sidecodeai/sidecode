import { ChevronRight } from 'lucide-react';
import { cx } from '../lib/cx';
import { OnlineDot } from './StatusDot';
import { strings } from '../strings';

interface DeviceProps {
  name: string;
  online: boolean;
  expanded: boolean;
  onToggle: () => void;
  className?: string;
}

/**
 * The header of one device's group. The Sessions page and the chat sidebar
 * share it so the two lists read the same way. The name is printed exactly as
 * the device reports it — never re-cased.
 */
export function DeviceGroupHeader({ name, online, expanded, onToggle, className }: DeviceProps) {
  return (
    <button
      type="button"
      className={cx('group-head device-head', className)}
      aria-expanded={expanded}
      onClick={onToggle}
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
  className?: string;
}

/** One device's Archive, folded shut under its active rows. */
export function ArchiveGroupHeader({ count, expanded, onToggle, className }: ArchiveProps) {
  return (
    <button
      type="button"
      className={cx('group-head archive-head', className)}
      aria-expanded={expanded}
      onClick={onToggle}
    >
      <ChevronRight size={12} aria-hidden className={cx('group-chevron', expanded && 'open')} />
      {strings.sessions.archiveGroup(count)}
    </button>
  );
}
