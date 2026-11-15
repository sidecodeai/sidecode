/**
 * Dictation into the composer's own field.
 *
 * The mic button starts listening; listening ends only when the user taps Done,
 * reaches for the field, or the microphone fails. There is no time limit here:
 * one gateway socket carries at most 120 s of audio, so a long dictation is cut
 * into segments whose transcripts are joined in order, and the replacement
 * socket is taking audio before the outgoing one is told to stop, so the seam
 * drops nothing. Nothing is ever sent by the act of stopping the recording.
 *
 * Every dictation is a run of its own. The composer follows one run at a time;
 * a run it lets go of — the session is left, or a batch dictation's field is
 * reached for — goes on, and reports to the job store instead.
 *
 * A57: a live run shows its words as the gateway recognises them. A batch run
 * asks for none while the person speaks, holds each segment's final text, and
 * hands the whole transcript over once, at its end; no way of ending it early
 * drops what was said.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { strings } from '../../strings';
import type { DictationNotice, TranscriptSink } from '../../stores/dictationJobs';
import type { DictationMode } from './dictationMode';
import { LevelFeed } from './levelFeed';
import { MicRecorder, type RecorderError } from './recorder';
import { TranscriptSegments } from './segments';
import { SttSocket, type SttEvent, type SttSocketFactory } from './sttSocket';

/** The slice of `MicRecorder` this hook uses, so a test can stand in for one. */
export interface VoiceRecorder {
  start: () => Promise<boolean>;
  stop: () => Promise<void>;
}

export interface RecorderHandlers {
  onFrame: (frame: ArrayBuffer) => void;
  /** A block's peak, which a cut waits on, and its energy, which the waveform draws. */
  onLevel: (peak: number, energy: number, samples: number) => void;
  onError: (error: RecorderError) => void;
}

export type VoiceState = 'idle' | 'starting' | 'listening' | 'finishing' | 'error';

/** Well inside the gateway's 120 s and 4 MiB budget for one utterance. */
const SEGMENT_MS = 30_000;
/** How long a cut may wait for a pause in the speech before it is taken anyway. */
const SEGMENT_LIMIT_MS = 45_000;
/** Input level under which the speaker counts as between words. */
const SILENCE_LEVEL = 0.12;
/** How long the gateway gets to answer Done before the draft is kept as it is. */
const FINAL_TIMEOUT_MS = 30_000;
const POLL_MS = 200;

/** Where a run's words go: the composer's field, or the job it was handed to. */
type Report = (
  text: string,
  isFinal: boolean,
  interrupted?: boolean,
  notice?: DictationNotice,
) => void;

/** One dictation, from the mic button to its last word. */
interface Run {
  readonly mode: DictationMode;
  readonly segments: TranscriptSegments;
  recorder: VoiceRecorder | null;
  sockets: SttSocket[];
  /** The socket taking audio right now. The others are transcribing. */
  route: SttSocket | null;
  /** The last block's peak, which a cut waits on. */
  level: number;
  /** Done: no segment opens any more, and the last word is awaited. */
  finishing: boolean;
  finalTimer: number | null;
  /** The run has said its last word, or was dropped: nothing it started acts any more. */
  over: boolean;
  /**
   * A batch segment lost its words, and this is what the run ends saying once
   * the others have spoken.
   */
  lost: DictationNotice | null;
  /** Where the run reports once it is handed over; until then, the composer. */
  sink: TranscriptSink | null;
}

interface Options {
  enabled: boolean;
  /**
   * The transcript so far. `isFinal` marks the last call of a dictation, after
   * which the text belongs to the field and this controller is idle again.
   * `interrupted` marks a last call that is not the recogniser's final word: the
   * gateway failed or never answered, and the text is what had been heard. A
   * batch run makes the last call alone.
   */
  onTranscript: (text: string, isFinal: boolean, interrupted?: boolean) => void;
  /**
   * The composer lets go of a dictation that is listening or finishing: it is
   * going away, or (A57) a batch dictation's field was reached for. Returns where
   * the rest of it reports, which keeps the run going instead of ending it; null
   * ends it as it always did. A listening run is then told Done.
   */
  onDetach?: () => TranscriptSink | null;
  /** Injected for tests. */
  factory?: SttSocketFactory;
  recorderFactory?: (handlers: RecorderHandlers) => VoiceRecorder;
}

export interface VoiceController {
  state: VoiceState;
  /** A57: how the run shows its words; the last run's once it is over. */
  mode: DictationMode;
  /** The microphone's levels, for the waveform. Nothing renders on them. */
  levels: LevelFeed;
  /** How long it has listened, in whole seconds; it stops when Done is tapped. */
  elapsedMs: number;
  error: string | null;
  start: (mode: DictationMode) => void;
  /** Stop listening and keep the transcript. Sending stays a separate click. */
  done: () => void;
  /**
   * The person reached for the field. A live run ends here, publishing
   * nothing: its words are in the field already. A batch run has none there
   * yet, so it goes on to `onDetach`, Done first if it was listening, and the
   * composer is free at once (A57); with nobody to take it, it is Done here.
   */
  takeover: () => void;
  dismissError: () => void;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => window.setTimeout(resolve, ms));

/** What the timer shows: whole seconds, so the composer redraws once a second. */
const wholeSeconds = (ms: number): number => Math.max(0, Math.floor(ms / 1000) * 1000);

export function useVoice({
  enabled,
  onTranscript,
  onDetach,
  factory,
  recorderFactory,
}: Options): VoiceController {
  const [state, setState] = useState<VoiceState>('idle');
  const [mode, setMode] = useState<DictationMode>('live');
  const [levels] = useState(() => new LevelFeed());
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);

  /** The run the composer follows. One it lets go of goes on without it. */
  const current = useRef<Run | null>(null);
  const startedAt = useRef(0);
  const onTranscriptRef = useRef(onTranscript);
  const onDetachRef = useRef(onDetach);
  const stateRef = useRef<VoiceState>('idle');

  useEffect(() => {
    onTranscriptRef.current = onTranscript;
    onDetachRef.current = onDetach;
  }, [onTranscript, onDetach]);

  const move = useCallback((next: VoiceState) => {
    stateRef.current = next;
    setState(next);
  }, []);

  /** Drop a run's microphone and every socket. Nothing is published from here. */
  const end = useCallback((run: Run) => {
    run.over = true;
    if (run.finalTimer !== null) window.clearTimeout(run.finalTimer);
    run.finalTimer = null;
    const recorder = run.recorder;
    run.recorder = null;
    void recorder?.stop();
    run.route = null;
    const closing = run.sockets;
    run.sockets = [];
    for (const socket of closing) socket.cancel();
  }, []);

  /** The composer lets go of its run: the row goes, and the clock with it. */
  const release = useCallback((run: Run) => {
    if (current.current !== run) return;
    current.current = null;
    setElapsedMs(0);
  }, []);

  const report = useCallback(
    (run: Run, isFinal: boolean, interrupted = false, notice?: DictationNotice) => {
      const to: Report = run.sink ?? onTranscriptRef.current;
      to(run.segments.joined, isFinal, interrupted, notice);
    },
    [],
  );

  /**
   * A failed run keeps whatever was recognised: a live run's words are in the
   * field already, and a batch run's held ones land now. The composer says why
   * of the run it follows; a batch run it handed on carries the notice to its
   * job, which says it wherever the session is opened (A57).
   */
  const fail = useCallback(
    (run: Run, message: string) => {
      if (run.over) return;
      report(run, true, true, run.mode === 'batch' ? { message } : undefined);
      end(run);
      if (current.current !== run) return;
      release(run);
      setError(message);
      move('error');
    },
    [report, end, release, move],
  );

  /** The run's last word: what it heard goes where it reports, and it is over. */
  const finish = useCallback(
    (run: Run, interrupted = false) => {
      if (run.over) return;
      if (run.lost) {
        fail(run, run.lost.message);
        return;
      }
      report(run, true, interrupted);
      end(run);
      if (current.current !== run) return;
      release(run);
      move('idle');
    },
    [fail, report, end, release, move],
  );

  const publish = useCallback(
    (run: Run, isFinal: boolean, interrupted = false) => {
      if (isFinal) finish(run, interrupted);
      // A57: a batch run holds what it has heard until its last word.
      else if (run.mode === 'live') report(run, false);
    },
    [finish, report],
  );

  /** Stop a run's microphone and await its last word, for as long as the gateway is given. */
  const stop = useCallback(
    (run: Run) => {
      if (run.finishing) return;
      run.finishing = true;
      const recorder = run.recorder;
      run.recorder = null;
      // The recorder flushes its tail frame as it stops, so the live socket is
      // told to transcribe only once that last audio has reached it.
      void (recorder?.stop() ?? Promise.resolve()).then(() => {
        if (run.over) return;
        const live = run.route;
        run.route = null;
        if (live) live.stop();
        else publish(run, true);
      });
      // A gateway that never answers must not leave the words waiting. A live
      // run's are in the field already; a batch run's held ones land, and the
      // composer says the rest was lost.
      run.finalTimer = window.setTimeout(() => {
        if (run.mode === 'live') publish(run, true, true);
        else fail(run, run.lost?.message ?? strings.voice.failed);
      }, FINAL_TIMEOUT_MS);
    },
    [publish, fail],
  );

  /**
   * A batch segment lost its words, to a failure or to a close without its
   * final. What the other segments heard is still worth having: listening stops
   * as Done would, they are awaited, and the run then ends with what they held
   * and the notice of the first loss.
   */
  const lose = useCallback(
    (run: Run, notice: DictationNotice) => {
      run.lost ??= notice;
      if (!run.finishing && current.current === run && stateRef.current === 'listening') {
        setElapsedMs(wholeSeconds(Date.now() - startedAt.current));
        move('finishing');
      }
      stop(run);
      if (run.segments.settled) fail(run, run.lost.message);
    },
    [move, stop, fail],
  );

  const receive = useCallback(
    (run: Run, index: number, event: SttEvent) => {
      if (run.over || !run.segments.has(index)) return;
      const settle = () => publish(run, run.finishing && run.segments.settled);
      switch (event.type) {
        case 'partial':
          // A57: a batch run shows nothing before Done, whatever arrives.
          if (run.mode === 'batch') return;
          if (run.segments.update(index, event.text)) settle();
          return;
        case 'final':
          run.segments.update(index, event.text);
          run.segments.end(index);
          settle();
          return;
        case 'failed': {
          run.segments.end(index);
          const message = event.message || strings.voice.failed;
          // A batch segment's words are only ever in its final, so a failure
          // costs that segment alone: the others are still awaited.
          if (run.mode === 'batch') lose(run, { message });
          // A live segment that already handed the microphone on keeps what it
          // transcribed; only the live one can end the dictation.
          else if (index === run.segments.active) fail(run, message);
          else settle();
          return;
        }
        case 'closed':
          // A socket closes after its final as a matter of course; only an
          // unannounced close still has a segment to settle. A batch segment
          // closed so has lost its words.
          if (!run.segments.isOpen(index)) return;
          run.segments.end(index);
          if (run.mode === 'batch') lose(run, { message: strings.voice.failed });
          else settle();
      }
    },
    [publish, fail, lose],
  );

  /**
   * Connect a fresh socket, hand the audio over to it, and let the one it
   * replaces transcribe what it already holds.
   */
  const openSegment = useCallback(
    async (run: Run): Promise<boolean> => {
      const index = run.segments.begin();
      const socket = new SttSocket({
        ...(factory ? { factory } : {}),
        partials: run.mode === 'live',
        onEvent: (event) => receive(run, index, event),
      });
      run.sockets.push(socket);
      try {
        await socket.start();
      } catch {
        run.segments.end(index);
        // A dictation that ended while this socket was still connecting is not
        // a failure: its end is what rejected the connect.
        if (run.over) return false;
        fail(run, strings.voice.failed);
        return false;
      }
      // Connecting is the one await here, so the dictation can have ended while
      // it ran. A socket nobody is going to speak into is closed, not armed.
      if (run.over || run.finishing) {
        run.segments.end(index);
        socket.cancel();
        return false;
      }
      const previous = run.route;
      run.route = socket;
      previous?.stop();
      return true;
    },
    [factory, receive, fail],
  );

  /** Hold a cut until the speaker pauses, and take it anyway if they do not. */
  const waitForSilence = useCallback(async (run: Run) => {
    const forced = Date.now() + (SEGMENT_LIMIT_MS - SEGMENT_MS);
    while (!run.over && !run.finishing && run.level > SILENCE_LEVEL && Date.now() < forced) {
      await sleep(POLL_MS);
    }
  }, []);

  /** Roll over to a new socket for as long as the user keeps talking. */
  const segmentLoop = useCallback(
    async (run: Run) => {
      while (!run.over && !run.finishing) {
        await sleep(SEGMENT_MS);
        if (run.over || run.finishing) return;
        await waitForSilence(run);
        if (run.over || run.finishing) return;
        if (!(await openSegment(run))) return;
      }
    },
    [waitForSilence, openSegment],
  );

  const start = useCallback(
    (next: DictationMode) => {
      if (!enabled || current.current) return;
      const run: Run = {
        mode: next,
        segments: new TranscriptSegments(),
        recorder: null,
        sockets: [],
        route: null,
        level: 0,
        finishing: false,
        finalTimer: null,
        over: false,
        lost: null,
        sink: null,
      };
      current.current = run;
      setMode(next);
      setError(null);
      setElapsedMs(0);
      startedAt.current = Date.now();
      move('starting');

      const handlers: RecorderHandlers = {
        onFrame: (frame) => run.route?.append(frame),
        onLevel: (peak, energy, samples) => {
          run.level = peak;
          levels.emit(energy, samples);
        },
        onError: (err) => fail(run, recorderErrorText(err)),
      };
      const recorder = recorderFactory ? recorderFactory(handlers) : new MicRecorder(handlers);
      run.recorder = recorder;

      void (async () => {
        const started = await recorder.start();
        if (!started || run.over) return;
        if (!(await openSegment(run))) return;
        if (run.over || current.current !== run) return;
        startedAt.current = Date.now();
        move('listening');
        void segmentLoop(run);
      })();
    },
    [enabled, move, fail, recorderFactory, openSegment, segmentLoop, levels],
  );

  const done = useCallback(() => {
    const run = current.current;
    if (!run || stateRef.current !== 'listening') return;
    setElapsedMs(wholeSeconds(Date.now() - startedAt.current));
    move('finishing');
    stop(run);
  }, [move, stop]);

  /**
   * Let the composer's run go on without it: from now on it reports to the sink
   * `onDetach` returns, a listening run is told Done, and the composer is free.
   * False, with nothing changed, when the run is not under way or nobody takes it.
   */
  const handOver = useCallback(
    (run: Run): boolean => {
      if (stateRef.current !== 'listening' && stateRef.current !== 'finishing') return false;
      const sink = onDetachRef.current?.() ?? null;
      if (!sink) return false;
      sink.bindCancel(() => end(run));
      run.sink = sink;
      stop(run);
      release(run);
      move('idle');
      return true;
    },
    [end, stop, release, move],
  );

  /** Drop the composer's run without publishing: its words are in the field already. */
  const cancel = useCallback(
    (run: Run) => {
      end(run);
      release(run);
      setError(null);
      move('idle');
    },
    [end, release, move],
  );

  const takeover = useCallback(() => {
    const run = current.current;
    if (!run) return;
    if (run.mode === 'live' || stateRef.current === 'starting') cancel(run);
    else if (!handOver(run)) done();
  }, [cancel, handOver, done]);

  const dismissError = useCallback(() => {
    setError(null);
    move('idle');
  }, [move]);

  useEffect(() => {
    if (state !== 'listening') return;
    const handle = window.setInterval(
      () => setElapsedMs(wholeSeconds(Date.now() - startedAt.current)),
      POLL_MS,
    );
    return () => window.clearInterval(handle);
  }, [state]);

  /**
   * The composer can be taken away mid-dictation: the device goes offline, or
   * the terminal takes the session back. The field greys out, and a run left
   * going would keep the microphone open and keep streaming audio to the
   * gateway for as long as the tab lived. It ends here instead, the way a hand
   * on the field ends it: a live run keeps the words already in the field, and
   * a batch run goes on to the job store, so the ones it holds still land.
   */
  useEffect(() => {
    if (enabled || !current.current) return;
    takeover();
  }, [enabled, takeover]);

  /**
   * Leaving the session is not the end of a dictation that has begun: it is
   * Done, and the words keep coming to whoever took them over. One that is only
   * connecting, or that nobody takes, ends here.
   */
  useEffect(
    () => () => {
      const run = current.current;
      if (run && !handOver(run)) end(run);
    },
    [handOver, end],
  );

  return {
    state,
    mode,
    levels,
    elapsedMs,
    error,
    start,
    done,
    takeover,
    dismissError,
  };
}

function recorderErrorText(error: RecorderError): string {
  if (error === 'denied') return strings.voice.denied;
  if (error === 'unsupported') return strings.voice.unsupported;
  return strings.voice.failed;
}
