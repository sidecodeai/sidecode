/**
 * Microphone capture. Prefers an AudioWorklet and falls back to a
 * ScriptProcessor where worklets are unavailable (older Safari, insecure
 * contexts). Emits 16 kHz PCM16LE frames plus an input level for the waveform.
 */
import { PcmChunker, peakLevel } from './downsample';

export type RecorderError = 'denied' | 'unsupported' | 'failed';

interface Handlers {
  onFrame: (frame: ArrayBuffer) => void;
  onLevel: (level: number) => void;
  onError: (error: RecorderError) => void;
}

interface WithWebkit {
  webkitAudioContext?: typeof AudioContext;
}

export class MicRecorder {
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private worklet: AudioWorkletNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private chunker: PcmChunker | null = null;

  constructor(private readonly handlers: Handlers) {}

  async start(): Promise<boolean> {
    const AudioCtor =
      globalThis.AudioContext ?? (globalThis as unknown as WithWebkit).webkitAudioContext;
    if (!navigator.mediaDevices?.getUserMedia || !AudioCtor) {
      this.handlers.onError('unsupported');
      return false;
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      const name = (err as { name?: string }).name;
      this.handlers.onError(name === 'NotAllowedError' || name === 'SecurityError' ? 'denied' : 'failed');
      return false;
    }
    try {
      this.context = new AudioCtor();
      await this.context.resume();
      this.chunker = new PcmChunker(this.context.sampleRate);
      this.source = this.context.createMediaStreamSource(this.stream);
      const usedWorklet = await this.attachWorklet();
      if (!usedWorklet) this.attachScriptProcessor();
      return true;
    } catch {
      this.handlers.onError('failed');
      await this.stop();
      return false;
    }
  }

  async stop(): Promise<void> {
    const tail = this.chunker?.flush();
    if (tail) this.handlers.onFrame(tail);
    this.worklet?.port.close();
    this.worklet?.disconnect();
    this.processor?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.worklet = null;
    this.processor = null;
    this.source = null;
    this.stream = null;
    this.context = null;
    this.chunker = null;
  }

  private async attachWorklet(): Promise<boolean> {
    const context = this.context;
    if (!context?.audioWorklet || !this.source) return false;
    try {
      await context.audioWorklet.addModule('/audio-worklet.js');
      const node = new AudioWorkletNode(context, 'rc-capture');
      node.port.onmessage = (event: MessageEvent<Float32Array>) => this.consume(event.data);
      this.source.connect(node);
      this.worklet = node;
      return true;
    } catch {
      return false;
    }
  }

  private attachScriptProcessor(): void {
    const context = this.context;
    if (!context || !this.source) return;
    const node = context.createScriptProcessor(4096, 1, 1);
    node.onaudioprocess = (event) => this.consume(event.inputBuffer.getChannelData(0));
    this.source.connect(node);
    // A ScriptProcessor only runs while connected to the graph; a muted gain
    // node keeps it alive without playing the microphone back to the user.
    const mute = context.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(context.destination);
    this.processor = node;
  }

  private consume(block: Float32Array): void {
    if (!this.chunker) return;
    this.handlers.onLevel(peakLevel(block));
    for (const frame of this.chunker.push(block)) this.handlers.onFrame(frame);
  }
}
