/**
 * One section of Settings as its address draws it. On a narrow screen the way
 * back to the list and the section's title come first; on a wide screen the nav
 * beside it says which section this is, so the back link is not drawn and the
 * title is kept for assistive technology alone (`settings.css`).
 */
import type { ComponentType } from 'react';
import { ChevronLeft } from 'lucide-react';
import { Link } from 'react-router';
import { strings } from '../../strings';
import { AccountSection } from './AccountSection';
import { ReadingGroup } from './ReadingGroup';
import { sectionLabel, type SectionId } from './sections';
import { VoiceGroup } from './VoiceGroup';
import { WhileAwayGroup } from './WhileAwayGroup';

/** How the app reads, then what happens while nobody is looking. */
function GeneralSection() {
  return (
    <>
      <ReadingGroup />
      <WhileAwayGroup />
    </>
  );
}

const CONTENT: Record<SectionId, ComponentType> = {
  general: GeneralSection,
  voice: VoiceGroup,
  account: AccountSection,
};

export function SettingsSection({ id }: { id: SectionId }) {
  const Content = CONTENT[id];
  return (
    <>
      <div className="settings-section-head">
        <Link to="/settings" className="settings-back">
          <ChevronLeft size={18} aria-hidden />
          {strings.settings.title}
        </Link>
        <h2 className="settings-section-title">{sectionLabel(id)}</h2>
      </div>
      <Content />
    </>
  );
}
