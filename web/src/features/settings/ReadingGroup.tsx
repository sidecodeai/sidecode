/**
 * How the app reads: the words it uses for itself, and how much of a turn the
 * timeline draws. Two choices each, so both are segmented controls in the row
 * (`docs/DESIGN.md` § "The Settings screen").
 */
import { Segmented } from '../../components/Segmented';
import type { InterfaceLanguage, TimelineDetail } from '../../protocol/types';
import { interfaceLanguageLabels, strings, timelineDetailLabel } from '../../strings';
import { INTERFACE_LANGUAGES, TIMELINE_DETAILS } from '../../stores/preferenceFields';
import { useSettings } from '../../stores/settings';
import { SettingsGroup } from './SettingsGroup';
import { SettingsRow } from './SettingsRow';

export function ReadingGroup() {
  const language = useSettings((s) => s.language);
  const setLanguage = useSettings((s) => s.setLanguage);
  const detail = useSettings((s) => s.timelineDetail);
  const setDetail = useSettings((s) => s.setTimelineDetail);

  return (
    <SettingsGroup title={strings.settings.reading}>
      <SettingsRow
        title={strings.settings.language}
        sentence={strings.settings.languageNote}
        control={
          <Segmented<InterfaceLanguage>
            ariaLabel={strings.settings.language}
            value={language}
            onChange={setLanguage}
            options={INTERFACE_LANGUAGES.map((code) => ({
              value: code,
              label: interfaceLanguageLabels[code],
            }))}
          />
        }
      />
      <SettingsRow
        title={strings.settings.timelineDetail}
        sentence={strings.settings.timelineDetailNote}
        control={
          <Segmented<TimelineDetail>
            ariaLabel={strings.settings.timelineDetail}
            value={detail}
            onChange={setDetail}
            options={TIMELINE_DETAILS.map((level) => ({
              value: level,
              label: timelineDetailLabel(level),
            }))}
          />
        }
      />
    </SettingsGroup>
  );
}
