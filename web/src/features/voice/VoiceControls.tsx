/**
 * What the composer's control row holds while dictation runs: the waveform,
 * how long it has been listening, and the one way out.
 *
 * Done stands where Send stands, at Send's size, because it is the one primary
 * action while listening. There is no Cancel: a dictation you do not want is
 * Done and then edited or cleared like any other draft. There is no "stop and
 * send" either — Done leaves the transcript in the message field and Send stays
 * the separate, explicit click it is for anything typed.
 *
 * The click on Done is answered in that same slot: the capsule gives way to the
 * spinner until the backend's final transcript lands, and nothing in the row can
 * be clicked in between (`docs/DESIGN.md` § "The composer"). Meanwhile the
 * waveform rests and the timer gives way to "Transcribing…" (A57). Once the
 * words are in, the ordinary row takes its place in the same frame as the
 * status line changes, so the composer moves once.
 */
import { useCallback, useRef } from 'react';
import { clock } from '../../lib/format';
import { strings } from '../../strings';
import type { PrimarySlot } from './primarySlot';
import type { VoiceController } from './useVoice';
import { Waveform } from './Waveform';
import { WorkingPill } from './WorkingPill';
import './voice.css';

/** The timer reads m:ss, and an hour of listening reads 59:59. */
const LONGEST_MS = 3_599_000;

interface Props {
  voice: VoiceController;
  /** The composer's one derivation: `done` while listening, else `working`. */
  slot: PrimarySlot;
  onDone: () => void;
}

export function VoiceControls({ voice, slot, onDone }: Props) {
  const resting = voice.state === 'finishing';
  const elapsed = clock(Math.min(voice.elapsedMs, LONGEST_MS));
  const timer = useRef<HTMLSpanElement>(null);
  const label = useRef<HTMLSpanElement>(null);
  // The label ends where the timer ends and reaches left over the waveform.
  const cover = useCallback(
    () => Math.max(0, (label.current?.offsetWidth ?? 0) - (timer.current?.offsetWidth ?? 0)),
    [],
  );

  return (
    <div className="voice-controls" data-phase={resting ? 'resting' : 'listening'}>
      <div className="voice-meter">
        <Waveform levels={voice.levels} resting={resting} cover={cover} />
      </div>
      <span className="voice-clock">
        <span
          ref={timer}
          className="mono voice-timer"
          aria-label={strings.voice.listeningFor(elapsed)}
          aria-hidden={resting || undefined}
        >
          {elapsed}
        </span>
        <span ref={label} className="voice-busy" aria-hidden={!resting || undefined}>
          {strings.voice.rowTranscribing}
        </span>
      </span>
      <span className="voice-slot">
        {/* Done's width, for as long as the row lasts: nothing beside the slot
            moves when Done becomes the spinner. */}
        <span className="btn small voice-slot-sizer" aria-hidden>
          {strings.voice.done}
        </span>
        {slot === 'done' ? (
          <button
            type="button"
            className="btn small primary send-btn"
            disabled={voice.state !== 'listening'}
            onClick={onDone}
          >
            {strings.voice.done}
          </button>
        ) : (
          <WorkingPill label={strings.voice.finishing} />
        )}
      </span>
    </div>
  );
}
