/**
 * One utterance over `WS /ws/stt`: PCM16LE, 16 kHz mono frames out, partial
 * transcripts back, one final transcript after `stop()`.
 *
 * A socket carries a single utterance because the gateway caps one at 120 s and
 * 4 MiB. Dictation longer than that is a chain of these, joined by
 * `TranscriptSegments`.
 */
import { socketUrl } from '../../lib/ws';

export type SttEvent =
  | { type: 'partial'; text: string }
  | { type: 'final'; text: string }
  | { type: 'failed'; message: string | null }
  | { type: 'closed' };

/** The slice of `WebSocket` this class uses, so a test can stand in for one. */
export interface SttSocketLike {
  binaryType: string;
  readyState: number;
  send(data: string | ArrayBuffer): void;
  close(): void;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
}

export type SttSocketFactory = (url: string) => SttSocketLike;

/** Gateway budget for one utterance: 4 MiB of audio. */
const MAX_AUDIO_BYTES = 4 * 1024 * 1024;

/**
 * How long the gateway gets to accept the socket. A connection that hangs
 * after TCP would otherwise leave the composer in `starting` for ever: no
 * waveform, Done disabled, and nothing to press.
 */
const CONNECT_TIMEOUT_MS = 5_000;

const OPEN = 1;

interface Options {
  onEvent: (event: SttEvent) => void;
  factory?: SttSocketFactory;
}

export class SttSocket {
  private socket: SttSocketLike | null = null;
  private sentBytes = 0;
  private finished = false;
  private opened = false;
  /** Rejects a connect that is still waiting, from `cancel()` or the timeout. */
  private failConnect: ((error: Error) => void) | null = null;
  private connectTimer: number | null = null;

  constructor(private readonly options: Options) {}

  /** Connect, and resolve once the gateway is ready for audio. */
  start(): Promise<void> {
    const factory = this.options.factory ?? ((url: string) => new WebSocket(url) as SttSocketLike);
    // A44: no language is asked for. The gateway's provider detects it.
    const socket = factory(socketUrl('/ws/stt'));
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    socket.onmessage = (event) => this.receive(event.data);
    // Before the socket is open a failure rejects the connect; after it, the
    // same failure is an event for the dictation to weigh.
    return new Promise<void>((resolve, reject) => {
      const settle = (): void => {
        this.failConnect = null;
        if (this.connectTimer !== null) globalThis.clearTimeout(this.connectTimer);
        this.connectTimer = null;
      };
      this.failConnect = (error) => {
        settle();
        reject(error);
      };
      this.connectTimer = globalThis.setTimeout(() => {
        this.failConnect?.(new Error('stt socket timed out'));
        socket.close();
      }, CONNECT_TIMEOUT_MS) as unknown as number;
      socket.onopen = () => {
        this.opened = true;
        settle();
        resolve();
      };
      socket.onerror = () => {
        if (this.opened) this.emit({ type: 'failed', message: null });
        else this.failConnect?.(new Error('stt socket failed'));
      };
      socket.onclose = () => {
        this.socket = null;
        if (this.opened) this.emit({ type: 'closed' });
        else this.failConnect?.(new Error('stt socket closed'));
      };
    });
  }

  /**
   * Append one capture buffer. Audio past the gateway's budget is dropped
   * rather than letting the socket be closed under the speaker mid-sentence.
   */
  append(frame: ArrayBuffer): void {
    const socket = this.socket;
    if (!socket || this.finished || socket.readyState !== OPEN) return;
    if (this.sentBytes + frame.byteLength > MAX_AUDIO_BYTES) {
      this.stop();
      return;
    }
    this.sentBytes += frame.byteLength;
    socket.send(frame);
  }

  /** Transcribe everything sent so far and wait for `stt.final`. */
  stop(): void {
    const socket = this.socket;
    if (!socket || this.finished) return;
    this.finished = true;
    if (socket.readyState === OPEN) socket.send(JSON.stringify({ type: 'stt.stop' }));
    else this.emit({ type: 'closed' });
  }

  /** Drop the utterance: no final transcript is wanted. */
  cancel(): void {
    const socket = this.socket;
    this.socket = null;
    this.finished = true;
    // A socket cancelled while it was still connecting fires nothing once its
    // handlers are detached, so the pending connect is settled here or the
    // chain awaiting it is retained for the life of the page.
    this.failConnect?.(new Error('stt socket cancelled'));
    if (!socket) return;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    if (socket.readyState === OPEN) socket.send(JSON.stringify({ type: 'stt.cancel' }));
    socket.close();
  }

  private receive(data: unknown): void {
    if (typeof data !== 'string') return;
    let frame: { type?: string; text?: string; message?: string };
    try {
      frame = JSON.parse(data) as typeof frame;
    } catch {
      return;
    }
    if (frame.type === 'stt.partial') this.emit({ type: 'partial', text: frame.text ?? '' });
    if (frame.type === 'stt.final') this.emit({ type: 'final', text: frame.text ?? '' });
    if (frame.type === 'stt.error') this.emit({ type: 'failed', message: frame.message ?? null });
  }

  private emit(event: SttEvent): void {
    this.options.onEvent(event);
  }
}
