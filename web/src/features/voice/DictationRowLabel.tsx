import { strings } from '../../strings';
import { jobOf, useDictationJobs } from '../../stores/dictationJobs';

/**
 * The session row's small trailing label for a dictation that runs on after the
 * person left: what it is doing, or what it left unseen. It says nothing else,
 * and nothing about plans. Opening the session clears a finished one.
 */
export function DictationRowLabel({ sessionKey }: { sessionKey: string }) {
  const job = useDictationJobs(jobOf(sessionKey));
  if (!job) return null;
  const words =
    job.phase === 'finishing'
      ? strings.voice.rowTranscribing
      : job.phase === 'polishing'
        ? strings.voice.rowPolishing
        : !job.unseen
          ? null
          : job.phase === 'ready'
            ? strings.voice.rowReady
            : strings.voice.rowInterrupted;
  return words ? <span className="dictation-label">{words}</span> : null;
}
