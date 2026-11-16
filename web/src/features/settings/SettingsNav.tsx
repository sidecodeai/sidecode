/**
 * The sections beside the one on show, on a wide screen: each a glyph and its
 * name, the one on show marked current. A narrow screen does not draw it; its
 * list of sections is `SectionList` (`settings.css`).
 */
import { Link } from 'react-router';
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import { sectionLabel, sectionPath, type Section, type SectionId } from './sections';

interface Props {
  sections: Section[];
  /** The section on show; the index shows General. */
  current: SectionId;
}

export function SettingsNav({ sections, current }: Props) {
  return (
    <nav className="settings-nav" aria-label={strings.settings.title}>
      {sections.map(({ id, icon: Icon }) => (
        <Link
          key={id}
          to={sectionPath(id)}
          className={cx('settings-nav-item', id === current && 'active')}
          aria-current={id === current ? 'page' : undefined}
        >
          <Icon size={16} strokeWidth={1.75} aria-hidden />
          <span>{sectionLabel(id)}</span>
        </Link>
      ))}
    </nav>
  );
}
