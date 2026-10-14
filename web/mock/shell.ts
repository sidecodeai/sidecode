/**
 * A38 — a shell the mock gateway can run, so the terminal page can be driven
 * and shot without a device.
 *
 * It is not a shell: it prints a prompt, echoes what is typed, answers a few
 * commands and ends on `exit` or Ctrl-D. What it does honour is the protocol
 * around it — `seq` from 1 and rising per terminal, output coalesced into
 * frames, a 64 KiB scrollback ring for `terminal.attach`, and an exit code.
 */
const SCROLLBACK_BYTES = 64 * 1024;

/** §7.3: the device coalesces for about 16 ms before it sends. */
const COALESCE_MS = 16;

export interface ShellSink {
  output: (data: string, seq: number) => void;
  exited: (code: number) => void;
}

export class FakeShell {
  readonly deviceId: string;
  cols: number;
  rows: number;

  private readonly prompt: string;
  private sink: ShellSink | null = null;
  private seq = 0;
  private ring = Buffer.alloc(0);
  private pending: Buffer[] = [];
  private flushTimer: NodeJS.Timeout | null = null;
  private line = '';
  private alive = true;

  constructor(deviceId: string, prompt: string, cols: number, rows: number) {
    this.deviceId = deviceId;
    this.prompt = prompt;
    this.cols = cols;
    this.rows = rows;
  }

  get scrollback(): string {
    return this.ring.toString('base64');
  }

  get running(): boolean {
    return this.alive;
  }

  /** The first output only starts once somebody is listening for it. */
  attach(sink: ShellSink): void {
    this.sink = sink;
  }

  detach(): void {
    this.sink = null;
  }

  greet(): void {
    this.write(`Last login: ${new Date().toDateString()}\r\n${this.prompt}`);
  }

  resize(cols: number, rows: number): void {
    this.cols = cols;
    this.rows = rows;
  }

  /** Bytes as the app typed them, base64. */
  input(data: string): void {
    if (!this.alive) return;
    for (const ch of Buffer.from(data, 'base64').toString('utf8')) this.key(ch);
  }

  close(code = 0): void {
    if (!this.alive) return;
    this.alive = false;
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.flushTimer = null;
    this.pending = [];
    this.sink?.exited(code);
  }

  /* ------------------------------------------------------------ internals */

  private key(ch: string): void {
    if (ch === '\r' || ch === '\n') {
      const line = this.line;
      this.line = '';
      this.write('\r\n');
      this.run(line.trim());
      return;
    }
    if (ch === '' || ch === '\b') {
      if (this.line.length === 0) return;
      this.line = this.line.slice(0, -1);
      this.write('\b \b');
      return;
    }
    if (ch === '') {
      this.line = '';
      this.write(`^C\r\n${this.prompt}`);
      return;
    }
    if (ch === '') {
      this.write('exit\r\n');
      this.close(0);
      return;
    }
    if (ch < ' ') return;
    this.line += ch;
    this.write(ch);
  }

  private run(line: string): void {
    if (line === 'exit' || line === 'logout') {
      this.close(0);
      return;
    }
    if (line.length > 0) this.write(`${this.answer(line)}\r\n`);
    this.write(this.prompt);
  }

  private answer(line: string): string {
    if (line === 'pwd') return '/Users/me';
    if (line === 'whoami') return 'me';
    if (line === 'date') return new Date().toString();
    if (line === 'ls') return 'Desktop    Documents  Downloads  work';
    if (line.startsWith('echo ')) return line.slice(5);
    // Everything else comes straight back, which is what makes typing visible.
    return line;
  }

  private write(text: string): void {
    if (!this.alive) return;
    this.pending.push(Buffer.from(text, 'utf8'));
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      this.flush();
    }, COALESCE_MS);
  }

  private flush(): void {
    const chunk = Buffer.concat(this.pending);
    this.pending = [];
    if (chunk.length === 0) return;
    this.ring = Buffer.concat([this.ring, chunk]).subarray(-SCROLLBACK_BYTES);
    this.seq += 1;
    this.sink?.output(chunk.toString('base64'), this.seq);
  }
}
