/**
 * The microphone for a call: echo-cancelled, noise-suppressed and level-controlled by the browser,
 * tapped by the app's AudioWorklet (`/audio-worklet.js`, the one the dictation microphone uses)
 * and turned into frames of 20 ms of PCM16 at 24 kHz.
 */
import { MicEncoder } from './pcm';

export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
};

const WORKLET_MODULE = '/audio-worklet.js';
const WORKLET_NAME = 'rc-capture';

export class MicError extends Error {
  readonly reason: 'denied' | 'unsupported' | 'failed';

  constructor(reason: 'denied' | 'unsupported' | 'failed') {
    super(reason);
    this.name = 'MicError';
    this.reason = reason;
  }
}

/** The error a refused or missing microphone raises, named for the panel to word. */
export function micError(error: unknown): MicError {
  const name = (error as { name?: string } | null)?.name;
  if (name === 'NotAllowedError' || name === 'SecurityError') return new MicError('denied');
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return new MicError('unsupported');
  return new MicError('failed');
}

export async function openMicrophone(): Promise<MediaStream> {
  if (!navigator.mediaDevices?.getUserMedia) throw new MicError('unsupported');
  try {
    return await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
  } catch (error) {
    throw micError(error);
  }
}

export class MicCapture {
  private source: MediaStreamAudioSourceNode | null = null;
  private node: AudioWorkletNode | null = null;
  private stopped = false;

  constructor(
    private readonly context: AudioContext,
    private readonly stream: MediaStream,
    private readonly onFrame: (frame: ArrayBuffer) => void,
  ) {}

  /** Start tapping. The worklet is added to the context the player also uses. */
  async start(): Promise<void> {
    if (!this.context.audioWorklet) throw new MicError('unsupported');
    try {
      await this.context.audioWorklet.addModule(WORKLET_MODULE);
      const encoder = new MicEncoder(this.context.sampleRate);
      const node = new AudioWorkletNode(this.context, WORKLET_NAME);
      node.port.onmessage = (event: MessageEvent<Float32Array>) => {
        if (this.stopped) return;
        for (const frame of encoder.push(event.data)) this.onFrame(frame);
      };
      const source = this.context.createMediaStreamSource(this.stream);
      source.connect(node);
      this.source = source;
      this.node = node;
    } catch {
      throw new MicError('failed');
    }
  }

  /** Stop tapping and release the microphone: the browser's recording indicator goes out. */
  stop(): void {
    this.stopped = true;
    if (this.node) {
      this.node.port.onmessage = null;
      this.node.port.close();
      this.node.disconnect();
    }
    this.source?.disconnect();
    this.stream.getTracks().forEach((track) => track.stop());
    this.node = null;
    this.source = null;
  }
}
