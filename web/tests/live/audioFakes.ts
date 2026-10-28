/** Stand-ins for the browser's audio, microphone and WebSocket, shared by the live tests. */
import { vi } from 'vitest';

export class FakeAudioBuffer {
  channel: Float32Array = new Float32Array(0);

  constructor(
    readonly numberOfChannels: number,
    readonly length: number,
    readonly sampleRate: number,
  ) {}

  get duration(): number {
    return this.length / this.sampleRate;
  }

  copyToChannel(data: Float32Array): void {
    this.channel = data;
  }
}

export class FakeSource {
  buffer: FakeAudioBuffer | null = null;
  startedAt: number | null = null;
  stopped = false;
  disconnected = false;
  onended: (() => void) | null = null;

  connect(): void {}
  disconnect(): void {
    this.disconnected = true;
  }
  start(when: number): void {
    this.startedAt = when;
  }
  stop(): void {
    this.stopped = true;
  }
  /** The browser calls this when a source has played to its end. */
  finish(): void {
    this.onended?.();
  }
}

export class FakeWorkletNode {
  port: { onmessage: ((event: { data: Float32Array }) => void) | null; close: () => void } = {
    onmessage: null,
    close: vi.fn(),
  };
  disconnected = false;

  constructor(
    readonly context: FakeAudioContext,
    readonly name: string,
  ) {
    context.worklets.push(this);
  }

  disconnect(): void {
    this.disconnected = true;
  }

  /** The worklet posts one block of microphone samples. */
  post(block: Float32Array): void {
    this.port.onmessage?.({ data: block });
  }
}

export class FakeMediaSource {
  connected: unknown = null;
  disconnected = false;

  constructor(readonly stream: FakeStream) {}

  connect(node: unknown): void {
    this.connected = node;
  }
  disconnect(): void {
    this.disconnected = true;
  }
}

export class FakeTrack {
  stopped = false;
  stop(): void {
    this.stopped = true;
  }
}

export class FakeStream {
  readonly track = new FakeTrack();
  getTracks(): FakeTrack[] {
    return [this.track];
  }
}

export class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  currentTime = 0;
  sampleRate = 48_000;
  destination = {};
  closed = false;
  readonly sources: FakeSource[] = [];
  readonly worklets: FakeWorkletNode[] = [];
  readonly audioWorklet = { addModule: vi.fn(async (_url: string) => undefined) };

  constructor(readonly options?: unknown) {
    FakeAudioContext.instances.push(this);
  }

  async resume(): Promise<void> {}

  async close(): Promise<void> {
    this.closed = true;
  }

  createBuffer(channels: number, length: number, rate: number): FakeAudioBuffer {
    return new FakeAudioBuffer(channels, length, rate);
  }

  createBufferSource(): FakeSource {
    const source = new FakeSource();
    this.sources.push(source);
    return source;
  }

  createMediaStreamSource(stream: FakeStream): FakeMediaSource {
    return new FakeMediaSource(stream);
  }
}

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  binaryType = '';
  bufferedAmount = 0;
  closedWith: number | null = null;
  readonly sent: (string | ArrayBuffer)[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: { code?: number }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  close(code?: number): void {
    this.closedWith = code ?? 1000;
    this.readyState = 3;
    this.onclose?.({ code });
  }

  /** The gateway accepts the connection. */
  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  serverSend(message: unknown): void {
    this.onmessage?.({ data: typeof message === 'string' ? message : JSON.stringify(message) });
  }

  /** A frame of the assistant's voice: silence unless a `level` (a constant PCM16 sample) is given. */
  serverAudio(bytes: number, level = 0): void {
    const frame = new Int16Array(bytes / 2).fill(level);
    this.onmessage?.({ data: frame.buffer });
  }

  serverClose(code = 1000): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }

  /** The JSON commands the page has sent, in order. */
  commands(): Record<string, unknown>[] {
    return this.sent
      .filter((item): item is string => typeof item === 'string')
      .map((item) => JSON.parse(item) as Record<string, unknown>);
  }

  frames(): ArrayBuffer[] {
    return this.sent.filter((item): item is ArrayBuffer => typeof item !== 'string');
  }
}

export const READY = {
  type: 'ready',
  sample_rate: 24_000,
  frame_ms: 20,
  seconds_left: 1200,
};

/** Install the fakes as the browser's globals, and forget the last test's. */
export function installAudioFakes(): { stream: FakeStream; getUserMedia: ReturnType<typeof vi.fn> } {
  FakeAudioContext.instances = [];
  FakeWebSocket.instances = [];
  const stream = new FakeStream();
  const getUserMedia = vi.fn(async (_constraints: unknown) => stream);
  vi.stubGlobal('AudioContext', FakeAudioContext);
  vi.stubGlobal(
    'AudioWorkletNode',
    class extends FakeWorkletNode {
      constructor(context: FakeAudioContext, name: string) {
        super(context, name);
      }
    },
  );
  vi.stubGlobal('WebSocket', FakeWebSocket);
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  });
  return { stream, getUserMedia };
}
