/**
 * The microphone's input levels, handed straight to whoever draws them.
 *
 * The recorder reports one for every audio block, hundreds of times a second,
 * so they bypass React state: the waveform reads them from here, and nothing
 * renders on their account. Each is a block's energy — the sum of its squared
 * samples — and how many samples it had, so a window's RMS can be told.
 */
export type LevelListener = (energy: number, samples: number) => void;

export class LevelFeed {
  private readonly listeners = new Set<LevelListener>();

  /** Hear every level from now on; the returned function stops it. */
  subscribe(listener: LevelListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(energy: number, samples: number): void {
    for (const listener of this.listeners) listener(energy, samples);
  }
}
