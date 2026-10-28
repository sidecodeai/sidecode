/**
 * Writes one call down while it runs, so a page that is reloaded or killed in the middle of a call
 * keeps what was said, and once more when the call ends. A running call is saved at most once
 * every `SAVE_EVERY_MS`; a call nobody said anything in leaves no record.
 */
import { keepRecord, type CallRecord } from './history';
import type { Entry } from './transcript';

export const SAVE_EVERY_MS = 2_000;

export class CallRecorder {
  private entries: Entry[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly owner: string,
    private readonly startedAt: number,
  ) {}

  /** The transcript changed: it is written within `SAVE_EVERY_MS`, and not more often. */
  note(entries: Entry[]): void {
    this.entries = entries;
    this.timer ??= setTimeout(() => {
      this.save(null);
    }, SAVE_EVERY_MS);
  }

  /** The call is over: write it now, with the reason. Returns the account's calls now kept. */
  finish(entries: Entry[], reason: string | null): CallRecord[] | null {
    if (this.timer !== null) clearTimeout(this.timer);
    this.entries = entries;
    return this.save(reason);
  }

  private save(reason: string | null): CallRecord[] | null {
    this.timer = null;
    if (this.entries.length === 0) return null;
    return keepRecord(this.owner, {
      id: String(this.startedAt),
      startedAt: this.startedAt,
      // A clock set back during the call must not leave a record that ends before it began.
      endedAt: Math.max(Date.now(), this.startedAt),
      reason,
      entries: this.entries,
    });
  }
}
