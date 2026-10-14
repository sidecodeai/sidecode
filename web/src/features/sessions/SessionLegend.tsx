import { legendEntries } from './legend';
import { strings } from '../../strings';

/**
 * What the dots mean, said once above the list and nowhere else: not on the
 * chat sidebar, not on the Devices screen, and not when the list is empty,
 * where the empty state speaks instead. A caption line with no box, no border
 * and no title (`docs/DESIGN.md` § "A legend, once, and quiet").
 */
export function SessionLegend() {
  return (
    <div className="session-legend" role="list" aria-label={strings.sessions.legend}>
      {legendEntries().map(({ tone, label }) => (
        <span className="session-legend-entry" role="listitem" key={tone}>
          <span className={`dot ${tone}`} aria-hidden />
          {label}
        </span>
      ))}
    </div>
  );
}
