/**
 * One call over `WS /ws/live`. Binary frames are raw PCM16LE mono at 24 kHz: the microphone up,
 * the assistant's voice down. Text frames are small JSON messages, listed in `ServerMessage`.
 * The socket is the call: when it closes the call is over, and a gateway that falls silent is a
 * gateway that is gone.
 */
import { socketUrl } from '../../lib/ws';
import type { LiveDraft } from './api';

export type Speaker = 'user' | 'assistant' | 'none';

export type ServerMessage =
  | { type: 'ready'; sample_rate: number; frame_ms: number; seconds_left: number }
  | { type: 'state'; speaker: Speaker }
  | { type: 'caption'; speaker: 'user' | 'assistant'; delta: string }
  | { type: 'draft'; draft: LiveDraft | null }
  | { type: 'draft.result'; draft_id: string; ok: boolean; session?: string | null; error?: string }
  /** The assistant is checking something: a backend response or a tool is running. On transitions only. */
  | { type: 'working'; on: boolean }
  | { type: 'flush' }
  | { type: 'notice'; code: string }
  | { type: 'time'; seconds_left: number }
  | { type: 'ended'; reason: string };

export type Command =
  | { type: 'hangup' }
  | { type: 'captions'; enabled: boolean }
  /** Which session's chat the person has on screen: both ids, or both null anywhere else. */
  | { type: 'focus'; device_id: string | null; session_id: string | null }
  /** What the voice has been through, cumulative for the call, and the delay now in force. */
  | { type: 'playback'; underruns: number; gap_ms: number; restarts: number; delay_ms: number }
  | { type: 'draft.send'; draft_id: string }
  | { type: 'draft.cancel'; draft_id: string };

/** The slice of `WebSocket` this class uses, so a test can stand in for one. */
export interface SocketLike {
  binaryType: string;
  readyState: number;
  bufferedAmount: number;
  send(data: string | ArrayBuffer): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code?: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
}

export type SocketFactory = (url: string) => SocketLike;

export interface CloseInfo {
  /** Why the gateway ended the call, when it said so before closing. */
  ended: string | null;
  code: number;
}

interface Handlers {
  onMessage: (message: ServerMessage) => void;
  onAudio: (frame: ArrayBuffer) => void;
  onClose: (info: CloseInfo) => void;
}

/** How long the gateway gets to answer with `ready`: it dials the voice service first. */
export const CONNECT_TIMEOUT_MS = 25_000;
/** A call that hears nothing from the gateway for this long is over: the gateway ticks every 3 s. */
export const SILENCE_TIMEOUT_MS = 10_000;
/** Microphone audio the socket may hold back before frames are dropped rather than queued. */
export const MAX_BUFFERED_BYTES = 96 * 1024;

export const CODE_TIMEOUT = 4000;
const OPEN = 1;

export class LiveSocket {
  private socket: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private ended: string | null = null;
  private finished = false;

  constructor(
    private readonly handlers: Handlers,
    private readonly factory: SocketFactory = (url) => new WebSocket(url) as SocketLike,
  ) {}

  open(): void {
    const socket = this.factory(socketUrl('/ws/live'));
    socket.binaryType = 'arraybuffer';
    socket.onmessage = (event) => this.receive(event.data);
    socket.onclose = (event) => this.finish(event.code ?? 1006);
    socket.onerror = () => undefined;
    this.socket = socket;
    this.arm(CONNECT_TIMEOUT_MS);
  }

  /** Send one frame of microphone audio. Returns false when it was dropped. */
  sendAudio(frame: ArrayBuffer): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN || socket.bufferedAmount > MAX_BUFFERED_BYTES) {
      return false;
    }
    socket.send(frame);
    return true;
  }

  send(command: Command): void {
    if (this.socket?.readyState === OPEN) this.socket.send(JSON.stringify(command));
  }

  /** Close from this end. The close is not reported: the caller already knows. */
  close(): void {
    this.finished = true;
    this.disarm();
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    socket.onmessage = null;
    socket.onclose = null;
    try {
      socket.close(1000);
    } catch {
      /* a socket that is already gone cannot be closed again */
    }
  }

  private receive(data: unknown): void {
    if (this.finished) return;
    this.arm(SILENCE_TIMEOUT_MS);
    if (data instanceof ArrayBuffer) {
      this.handlers.onAudio(data);
      return;
    }
    const message = parse(data);
    if (!message) return;
    if (message.type === 'ended') this.ended = message.reason;
    this.handlers.onMessage(message);
  }

  private finish(code: number): void {
    if (this.finished) return;
    this.finished = true;
    this.disarm();
    this.socket = null;
    this.handlers.onClose({ ended: this.ended, code });
  }

  private arm(ms: number): void {
    this.disarm();
    this.timer = setTimeout(() => {
      const socket = this.socket;
      this.finish(CODE_TIMEOUT);
      try {
        socket?.close(1000);
      } catch {
        /* already gone */
      }
    }, ms);
  }

  private disarm(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}

function parse(data: unknown): ServerMessage | null {
  if (typeof data !== 'string') return null;
  try {
    const value: unknown = JSON.parse(data);
    if (typeof value === 'object' && value !== null && typeof (value as { type?: unknown }).type === 'string') {
      return value as ServerMessage;
    }
  } catch {
    /* not a message */
  }
  return null;
}
