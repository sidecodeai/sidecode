/**
 * A33 — one rate-limit window: its name, a meter filled to the used share, the
 * percentage, and when it resets. `docs/DESIGN.md` § "Quota is a meter, drawn
 * for accounts only".
 */
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import { meterTone, resetsText, usedPercent, windowName } from './accounts';
import type { AgentLimit } from '../../protocol/types';

export function QuotaMeter({ limit }: { limit: AgentLimit }) {
  const name = windowName(limit);
  const used = usedPercent(limit);

  return (
    <li className="device-page-meter">
      <span className="device-page-meter-name">{name}</span>
      <span className="device-page-meter-value">{strings.devicePage.percent(used)}</span>
      {limit.resets_at ? (
        <span className="device-page-meter-reset">{resetsText(limit.resets_at)}</span>
      ) : null}
      <span
        className="device-page-meter-track"
        role="progressbar"
        aria-label={strings.devicePage.usage(name)}
        aria-valuenow={used}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span
          className={cx('device-page-meter-fill', meterTone(limit.used_percent))}
          style={{ width: `${used}%` }}
        />
      </span>
    </li>
  );
}
