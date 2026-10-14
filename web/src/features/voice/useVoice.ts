/**
 * Dictation into the composer's own field.
 *
 * The mic button starts listening; listening ends only when the user taps Done,
 * types into the field, or the microphone fails. There is no time limit here:
 * one gateway socket carries at most 120 s of audio, so a long dictation is cut
 * into segments whose transcripts are joined in order, and the replacement
 * socket is taking audio before the outgoing one is told to stop, so the seam
 * drops nothing. Nothing is ever sent by the act of stopping the recording.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { strings } from '../../strings';
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
  onLevel: (level: number) => void;
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

interface Options {
  enabled: boolean;
  /**
   * The transcript so far. `isFinal` marks the last call of a dictation, after
   * which the text belongs to the field and this controller is idle again.
   */
  onTranscript: (text: string, isFinal: boolean) => void;
  /** Injected for tests. */
  factory?: SttSocketFactory;
  recorderFactory?: (handlers: RecorderHandlers) => VoiceRecorder;
}

export interface VoiceController {
  state: VoiceState;
  level: number;
  elapsedMs: number;
  error: string | null;
  start: () => void;
  /** Stop listening and keep the transcript. Sending stays a separate click. */
  done: () => void;
  /** Drop the run without publishing: a keystroke takes the field back. */
  cancel: () => void;
  dismissError: () => void;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => window.setTimeout(resolve, ms));

export function useVoice({
  enabled,
  onTranscript,
  factory,
  recorderFactory,
}: Options): VoiceController {
  const [state, setState] = useState<VoiceState>('idle');
  const [level, setLevel] = useState(0);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const recorder = useRef<VoiceRecorder | null>(null);
  const sockets = useRef<SttSocket[]>([]);
  const segments = useRef(new TranscriptSegments());
  /** The socket taking audio right now. The others are transcribing. */
  const route = useRef<SttSocket | null>(null);
  const levelRef = useRef(0);
  const startedAt = useRef(0);
  const finishing = useRef(false);
  const finalTimer = useRef<number | null>(null);
  /** Bumped by every start and every end, so a stale loop or reply is ignored. */
  const runId = useRef(0);
  const onTranscriptRef = useRef(onTranscript);
  const stateRef = useRef<VoiceState>('idle');

  useEffect(() => {
    onTranscriptRef.current = onTranscript;
  }, [onTranscript]);

  const move = useCallback((next: VoiceState) => {
    stateRef.current = next;
    setState(next);
  }, []);

  const clearFinalTimer = useCallback(() => {
    if (finalTimer.current !== null) window.clearTimeout(finalTimer.current);
    finalTimer.current = null;
  }, []);

  /** Drop the microphone and every socket. Nothing is published from here. */
  const teardown = useCallback(() => {
    runId.current += 1;
    finishing.current = false;
    clearFinalTimer();
    const rec = recorder.current;
    recorder.current = null;
    void rec?.stop();
    route.current = null;
    const closing = sockets.current;
    sockets.current = [];
    for (const socket of closing) socket.cancel();
    levelRef.current = 0;
  }, [clearFinalTimer]);

  const reset = useCallback(() => {
    teardown();
    segments.current = new TranscriptSegments();
    setLevel(0);
    setElapsedMs(0);
  }, [teardown]);

  const cancel = useCallback(() => {
    reset();
    setError(null);
    move('idle');
  }, [reset, move]);

  const fail = useCallback(
    (message: string) => {
      // A failed dictation keeps whatever was recognised: those words are
      // already in the field, and losing them helps no one.
      onTranscriptRef.current(segments.current.joined, true);
      reset();
      setError(message);
      move('error');
    },
    [reset, move],
  );

  const publish = useCallback(
    (isFinal: boolean) => {
      onTranscriptRef.current(segments.current.joined, isFinal);
      if (isFinal) {
        reset();
        move('idle');
      }
    },
    [reset, move],
  );

  const receive = useCallback(
    (event: SttEvent, index: number, run: number) => {
      if (run !== runId.current || !segments.current.has(index)) return;
      const settle = () => publish(finishing.current && segments.current.settled);
      switch (event.type) {
        case 'partial':
          if (segments.current.update(index, event.text)) settle();
          return;
        case 'final':
          segments.current.update(index, event.text);
          segments.current.end(index);
          settle();
          return;
        case 'failed':
          segments.current.end(index);
          // A segment that already handed the microphone on keeps what it
          // transcribed; only the live one can end the dictation.
          if (index === segments.current.active) fail(event.message || strings.voice.failed);
          else settle();
          return;
        case 'closed':
          // A socket closes after its final as a matter of course; only an
          // unannounced close still has a segment to settle.
          if (!segments.current.isOpen(index)) return;
          segments.current.end(index);
          settle();
      }
    },
    [publish, fail],
  );

  /**
   * Connect a fresh socket, hand the audio over to it, and let the one it
   * replaces transcribe what it already holds.
   */
  const openSegment = useCallback(
    async (run: number): Promise<boolean> => {
      const index = segments.current.begin();
      const socket = new SttSocket({
        ...(factory ? { factory } : {}),
        onEvent: (event) => receive(event, index, run),
      });
      sockets.current.push(socket);
      try {
        await socket.start();
      } catch {
        segments.current.end(index);
        // A dictation that ended while this socket was still connecting is not
        // a failure: the teardown is what rejected the connect.
        if (run !== runId.current) return false;
        fail(strings.voice.failed);
        return false;
      }
      // Connecting is the one await here, so the dictation can have ended while
      // it ran. A socket nobody is going to speak into is closed, not armed.
      if (run !== runId.current || finishing.current) {
        segments.current.end(index);
        socket.cancel();
        return false;
      }
      const previous = route.current;
      route.current = socket;
      previous?.stop();
      return true;
    },
    [factory, receive, fail],
  );

  /** Hold a cut until the speaker pauses, and take it anyway if they do not. */
  const waitForSilence = useCallback(async (run: number) => {
    const forced = Date.now() + (SEGMENT_LIMIT_MS - SEGMENT_MS);
    while (
      run === runId.current &&
      !finishing.current &&
      levelRef.current > SILENCE_LEVEL &&
      Date.now() < forced
    ) {
      await sleep(POLL_MS);
    }
  }, []);

  /** Roll over to a new socket for as long as the user keeps talking. */
  const segmentLoop = useCallback(
    async (run: number) => {
      while (run === runId.current && !finishing.current) {
        await sleep(SEGMENT_MS);
        if (run !== runId.current || finishing.current) return;
        await waitForSilence(run);
        if (run !== runId.current || finishing.current) return;
        if (!(await openSegment(run))) return;
      }
    },
    [waitForSilence, openSegment],
  );

  const start = useCallback(() => {
    if (!enabled || (stateRef.current !== 'idle' && stateRef.current !== 'error')) return;
    teardown();
    segments.current = new TranscriptSegments();
    setError(null);
    setLevel(0);
    setElapsedMs(0);
    startedAt.current = Date.now();
    move('starting');
    const run = runId.current;

    const handlers: RecorderHandlers = {
      onFrame: (frame) => route.current?.append(frame),
      onLevel: (value) => {
        levelRef.current = value;
        setLevel(value);
      },
      onError: (err) => fail(recorderErrorText(err)),
    };
    const rec = recorderFactory ? recorderFactory(handlers) : new MicRecorder(handlers);
    recorder.current = rec;

    void (async () => {
      const started = await rec.start();
      if (run !== runId.current) return;
      if (!started) return;
      if (!(await openSegment(run))) return;
      if (run !== runId.current) return;
      startedAt.current = Date.now();
      move('listening');
      void segmentLoop(run);
    })();
  }, [enabled, teardown, move, fail, recorderFactory, openSegment, segmentLoop]);

  const done = useCallback(() => {
    if (stateRef.current !== 'listening') return;
    finishing.current = true;
    move('finishing');
    setLevel(0);
    const run = runId.current;
    const rec = recorder.current;
    recorder.current = null;
    // The recorder flushes its tail frame as it stops, so the live socket is
    // told to transcribe only once that last audio has reached it.
    void (rec?.stop() ?? Promise.resolve()).then(() => {
      if (run !== runId.current) return;
      const live = route.current;
      route.current = null;
      if (live) live.stop();
      else publish(true);
    });
    // A gateway that never answers must not leave the composer waiting: keep
    // what was recognised instead.
    finalTimer.current = window.setTimeout(() => {
      if (run === runId.current) publish(true);
    }, FINAL_TIMEOUT_MS);
  }, [move, publish]);

  const dismissError = useCallback(() => {
    setError(null);
    move('idle');
  }, [move]);

  useEffect(() => {
    if (state !== 'listening') return;
    const handle = window.setInterval(
      () => setElapsedMs(Date.now() - startedAt.current),
      POLL_MS,
    );
    return () => window.clearInterval(handle);
  }, [state]);

  /**
   * The composer can be taken away mid-dictation: the device goes offline, or
   * the terminal takes the session back. The field greys out, and a run left
   * going would keep the microphone open and keep streaming audio to the
   * gateway for as long as the tab lived. It ends here instead, keeping the
   * words already recognised, which are in the field.
   */
  useEffect(() => {
    if (enabled) return;
    if (stateRef.current === 'idle' || stateRef.current === 'error') return;
    cancel();
  }, [enabled, cancel]);

  useEffect(() => () => teardown(), [teardown]);

  return { state, level, elapsedMs, error, start, done, cancel, dismissError };
}

function recorderErrorText(error: RecorderError): string {
  if (error === 'denied') return strings.voice.denied;
  if (error === 'unsupported') return strings.voice.unsupported;
  return strings.voice.failed;
}
