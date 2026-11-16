/**
 * The footer caption: the versions. It comes after the section in the page, so
 * it is read and reached last; a wide screen draws it under the nav, a narrow
 * one at the end of the list of sections (`settings.css`).
 */
import { VersionsLine } from './VersionsLine';

export function SettingsFoot() {
  return (
    <div className="settings-foot">
      <VersionsLine />
    </div>
  );
}
