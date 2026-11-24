/**
 * A66 — a press of the microphone while a voice assistant call holds it says so, under the field,
 * with the way out: Hang up ends the call. It stays until it is closed or Hang up is used, and the
 * next dictation replaces it; it never fades by itself.
 */
import { strings } from '../../strings';
import { hangUp } from '../live/liveCall';
import { liveStrings } from '../live/strings';

export function MicHeldNotice({ onClose }: { onClose: () => void }) {
  return (
    <div className="composer-errors voice-error" role="alert">
      <p>{strings.voice.callHoldsMic}</p>
      <button
        type="button"
        className="link-btn"
        onClick={() => {
          hangUp();
          onClose();
        }}
      >
        {liveStrings.hangUp}
      </button>
      <button type="button" className="link-btn" onClick={onClose}>
        {strings.common.close}
      </button>
    </div>
  );
}
