/**
 * What the composer's one primary slot holds, derived from the two things that
 * can claim it: where dictation has got to, and whether the polish model is
 * still writing the words back.
 *
 * `docs/DESIGN.md` § "The composer" → **Done becomes a spinner, and the spinner
 * becomes Send**. The rule is one sentence long: Done while the microphone is
 * live, a spinner for as long as the words are still on their way, and Send the
 * moment the field holds what will be sent. It lives here, away from any
 * component, because the interesting part is the pairs — a phase of dictation
 * against a phase of polish — and every one of them can be checked without a
 * render.
 */
import type { VoiceState } from './useVoice';

/** A29 — where a dictation is between the recogniser and the model. */
export type PolishPhase = 'idle' | 'polishing' | 'polished' | 'failed';

export type PrimarySlot = 'done' | 'working' | 'send';

export function primarySlot(voice: VoiceState, polish: PolishPhase): PrimarySlot {
  switch (voice) {
    // A live dictation owns the slot whatever else is out. It cannot in fact be
    // polishing — starting a dictation drops the last answer — but the rule
    // reads the same either way: the words are still being spoken, so the only
    // thing to offer is the way out of speaking them.
    case 'starting':
    case 'listening':
      return 'done';
    // Done was clicked and the backend's final transcript is not here yet.
    case 'finishing':
      return 'working';
    // Dictation is over: the field holds the dictated words already, and only
    // an answer still on its way stands between them and Send.
    case 'idle':
    case 'error':
      return polish === 'polishing' ? 'working' : 'send';
  }
}
