import { reloadPage } from '../lib/reload';
import { strings } from '../strings';
import { useSettings } from '../stores/settings';
import { useStaleBuild } from '../stores/staleBuild';
import './staleBuildNotice.css';

/**
 * Shown once this tab's build has been replaced on the server. It floats over
 * the page and takes no room from it, so what the person typed stays where it
 * is and can be copied before the reload.
 */
export function StaleBuildNotice() {
  const stale = useStaleBuild((s) => s.stale);
  // It sits outside the routes that are keyed on the interface language, so it
  // follows the language itself.
  useSettings((s) => s.language);
  if (!stale) return null;
  return (
    <div className="stale-notice" role="alert">
      <span>{strings.appUpdate.stale}</span>
      <button type="button" className="btn small primary" onClick={reloadPage}>
        {strings.appUpdate.reload}
      </button>
    </div>
  );
}
