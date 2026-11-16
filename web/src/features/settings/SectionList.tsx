/**
 * `/settings` on a narrow screen, where there is no room for a nav beside a
 * section: who is signed in, leading to Account, then a row per section, each
 * leading to it. A wide screen never draws this (`settings.css`).
 */
import { IdentityRow } from './IdentityHeader';
import { sectionLabel, sectionNote, sectionPath, type Section } from './sections';
import { SettingsLinkRow } from './SettingsRow';

export function SectionList({ sections }: { sections: Section[] }) {
  return (
    <div className="settings-list">
      <IdentityRow />
      <div className="surface">
        {sections.map(({ id, icon: Icon }) => (
          <SettingsLinkRow
            key={id}
            to={sectionPath(id)}
            icon={<Icon size={18} strokeWidth={1.75} />}
            title={sectionLabel(id)}
            sentence={sectionNote(id)}
          />
        ))}
      </div>
    </div>
  );
}
