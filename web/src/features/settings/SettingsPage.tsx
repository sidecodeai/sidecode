/**
 * Settings, in the shape `docs/DESIGN.md` § "The Settings screen" gives the
 * web: one page whose sections — General, Voice and Account — each have an
 * address, `/settings/<section>`. A wide screen draws the sections as a nav
 * beside the one on show, with the footer caption under it, and `/settings`
 * itself shows General. A narrow screen has no room for both: `/settings` is
 * the list of sections and each section leads back to it. Which of the two a
 * screen draws is `settings.css`'s alone; the page renders the same at every
 * width. An address that names no section leads to `/settings`.
 */
import type { CSSProperties } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router';
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import { SectionList } from './SectionList';
import { SECTIONS, type Section } from './sections';
import { SettingsFoot } from './SettingsFoot';
import { SettingsNav } from './SettingsNav';
import { SettingsSection } from './SettingsSection';
import './settings.css';

export function SettingsPage() {
  const sections = SECTIONS;
  // `/settings/voice/` names the same section as `/settings/voice`, as the routes below read it.
  const shown = (useParams()['*'] ?? '').replace(/\/+$/, '');
  const section = sections.find((candidate) => candidate.id === shown) ?? null;

  return (
    <div className={cx('settings', section ? 'at-section' : 'at-index')}>
      <div className="page-head settings-head">
        <h1>{strings.settings.title}</h1>
      </div>
      {/* The nav, the section, then the footer caption: the order the page is
          read and tabbed through at every width. The stylesheet sets the
          caption under the nav on a wide screen, and needs the nav's length
          to do so. */}
      <div className="settings-layout" style={{ '--settings-nav-items': sections.length } as CSSProperties}>
        <SettingsNav sections={sections} current={section?.id ?? 'general'} />
        <div className="settings-content">
          <Routes>
            <Route index element={<SettingsIndex sections={sections} />} />
            {sections.map(({ id }) => (
              <Route key={id} path={id} element={<SettingsSection id={id} />} />
            ))}
            <Route path="*" element={<Navigate to="/settings" replace />} />
          </Routes>
        </div>
        <SettingsFoot />
      </div>
    </div>
  );
}

/** `/settings`: the list of sections on a narrow screen, General on a wide one. */
function SettingsIndex({ sections }: { sections: Section[] }) {
  return (
    <>
      <SectionList sections={sections} />
      <div className="settings-index-general">
        <SettingsSection id="general" />
      </div>
    </>
  );
}
