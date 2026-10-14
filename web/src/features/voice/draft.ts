/**
 * Where a live transcript lands in the message field.
 *
 * Dictation never replaces what was already typed: the transcript is appended
 * to the draft the mic button was pressed on, and every update rewrites only
 * that tail, so a correction from the recogniser does not leave a duplicate.
 */
export function mergeDraft(base: string, transcript: string): string {
  if (transcript.length === 0) return base;
  if (base.length === 0) return transcript;
  return /\s$/.test(base) ? base + transcript : `${base} ${transcript}`;
}
