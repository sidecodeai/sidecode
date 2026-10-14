/**
 * The transcript of a dictation that outlived a single gateway request.
 *
 * The gateway accepts at most 120 s of audio per utterance, so a long
 * dictation rolls over to a fresh socket while the microphone keeps running,
 * and each socket owns one slot here.
 *
 * Slots fill out of order: a new segment's first partial can arrive before the
 * previous segment's final does. The text is therefore joined by position
 * rather than by arrival, and a segment stays open until its socket has said
 * the last word about it, so a dictation is only final once every slot has
 * settled.
 */
export class TranscriptSegments {
  private texts: string[] = [];
  private open = new Set<number>();

  /** Open a slot for a new socket and return its index. */
  begin(): number {
    const index = this.texts.length;
    this.texts.push('');
    this.open.add(index);
    return index;
  }

  /** The slot currently taking audio, which is the last one opened. */
  get active(): number | null {
    return this.texts.length === 0 ? null : this.texts.length - 1;
  }

  /**
   * Replace one segment's text and say whether the joined transcript moved.
   * The gateway reports the whole segment on every result rather than a delta,
   * so this is a replacement; a result for a slot never opened is ignored.
   */
  update(index: number, text: string): boolean {
    if (index < 0 || index >= this.texts.length) return false;
    const trimmed = text.trim();
    if (this.texts[index] === trimmed) return false;
    this.texts[index] = trimmed;
    return true;
  }

  /** Mark a segment finished: its socket will say nothing more about it. */
  end(index: number): void {
    this.open.delete(index);
  }

  /** Whether this segment's socket may still say something about it. */
  isOpen(index: number): boolean {
    return this.open.has(index);
  }

  has(index: number): boolean {
    return index >= 0 && index < this.texts.length;
  }

  /** True once every segment has finished — the only moment a text is final. */
  get settled(): boolean {
    return this.open.size === 0;
  }

  /** Every segment in the order its audio was spoken, empty ones dropped. */
  get joined(): string {
    return this.texts.filter((text) => text.length > 0).join(' ');
  }
}
