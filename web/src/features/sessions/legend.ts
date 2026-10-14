import { strings } from '../../strings';
import type { DotTone } from '../../components/dotTone';

/**
 * The four colours a dot takes, in the order the legend reads them
 * (`docs/DESIGN.md` § "A legend, once, and quiet"). Four entries, not five: the
 * pulsing amber of a waiting session and the solid amber of a finished turn are
 * one colour to the eye, so the legend draws the still one and "For you" covers
 * both. The words come from the table the interface language names, so the
 * legend follows a language change like everything else.
 */
export function legendEntries(): { tone: DotTone; label: string }[] {
  return [
    { tone: 'working', label: strings.sessions.legendWorking },
    { tone: 'live', label: strings.sessions.legendAttention },
    { tone: 'off', label: strings.sessions.legendOff },
    { tone: 'failed', label: strings.sessions.legendFailed },
  ];
}
