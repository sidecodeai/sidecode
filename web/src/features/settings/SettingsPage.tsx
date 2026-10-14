/**
 * Settings, in the shape `docs/DESIGN.md` § "The Settings screen" gives it: a
 * header saying who and where, four groups named for the question each answers,
 * and the versions as one caption line.
 */
import { strings } from '../../strings';
import { AccountGroup } from './AccountGroup';
import { IdentityHeader } from './IdentityHeader';
import { ReadingGroup } from './ReadingGroup';
import { VersionsLine } from './VersionsLine';
import { VoiceGroup } from './VoiceGroup';
import { WhileAwayGroup } from './WhileAwayGroup';
import './settings.css';

export function SettingsPage() {
  return (
    <>
      <div className="page-head">
        <h1>{strings.settings.title}</h1>
      </div>

      <div className="settings">
        <IdentityHeader />
        <AccountGroup />
        <WhileAwayGroup />
        <VoiceGroup />
        <ReadingGroup />
        <VersionsLine />
      </div>
    </>
  );
}
