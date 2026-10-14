/**
 * A38 §7.3 — one terminal on one device, from this app connection.
 *
 * Everything the screen needs is here: xterm.js opened into the page's own
 * box, the shell started at the size that box fitted itself to, the bytes the
 * device sends written into the emulator, what is typed sent back, the size
 * followed, the terminal taken over again after a lost socket, and the shell
 * ended when the page is left. The emulator and the session are one hook
 * because they are one thing: the session's size comes from the emulator and
 * the emulator's contents come from the session.
 *
 * Three rules are worth knowing before changing anything here.
 *
 * A reply that belongs to a previous attempt must never land. A reconnect, a
 * New shell and leaving the page all void what is in flight, and an `open`
 * that comes back after that is closed rather than left running for ten
 * minutes with nobody attached.
 *
 * An `attach` reply's `scrollback` is the screen as it was left, so the
 * emulator is reset before it is written: appending it would print everything
 * the person already read a second time.
 *
 * And what a shell writes is bytes, not text. They are decoded once, by the
 * emulator, which is the only thing that knows how to. Nothing here logs,
 * stores or inspects them.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import { base64ToBytes, textToBase64 } from '../../lib/base64';
import { errorText } from '../../lib/errors';
import { rpc } from '../../lib/gateway';
import { RequestError } from '../../lib/ws';
import { useConnection } from '../../stores/connection';
import {
  forgetTerminal,
  onTerminalFrame,
  rememberTerminal,
  rememberedTerminal,
} from '../../stores/terminal';
import { terminalOptions } from './terminalTheme';

export type TerminalStatus = 'connecting' | 'connected' | 'disconnected' | 'exited';

export interface TerminalScreen {
  /**
   * The box the emulator is opened into and measures itself against, taken as
   * a callback rather than a ref object: a ref handed out of a hook is a ref
   * the page would be reading during its own render.
   */
  host: (node: HTMLDivElement | null) => void;
  status: TerminalStatus;
  /** The shell's exit code, or null when the device never said (§7.3). */
  exitCode: number | null;
  /** Why the last attempt failed, in the reader's language. */
  error: string | null;
  /** §7.3: a `seq` that skipped, so bytes were dropped rather than delayed. */
  gap: boolean;
  /** True once a shell has run here, which is what makes a loss a loss. */
  started: boolean;
  reconnect: () => void;
  restart: () => void;
  close: () => void;
}

/** The view is fitted on every frame of a drag; the device is told once. */
const RESIZE_DEBOUNCE_MS = 100;

/** Before the emulator has measured itself, the size every terminal starts at. */
const DEFAULT_SIZE = { cols: 80, rows: 24 };

/** A38's bounds. A collapsed box fits to nothing, and nothing is not a size. */
const clamp = (value: number, max: number): number =>
  Math.min(max, Math.max(1, Math.round(value) || 1));

const sizeOf = (term: Terminal | null): { cols: number; rows: number } => ({
  cols: clamp(term?.cols ?? DEFAULT_SIZE.cols, 500),
  rows: clamp(term?.rows ?? DEFAULT_SIZE.rows, 200),
});

/** What one attempt at a terminal ended as. `exited` is the shell's own doing. */
type Outcome = { status: 'connected' } | { status: 'disconnected'; error: string };

export function useTerminal(deviceId: string, enabled: boolean): TerminalScreen {
  const socketOpen = useConnection((s) => s.status === 'open');

  const [attempt, setAttempt] = useState(0);
  const [answered, setAnswered] = useState<{ key: string; outcome: Outcome } | null>(null);
  const [exit, setExit] = useState<{ code: number | null } | null>(null);
  const [gap, setGap] = useState(false);
  const [started, setStarted] = useState(false);

  const hostRef = useRef<HTMLDivElement | null>(null);
  const host = useCallback((node: HTMLDivElement | null) => {
    hostRef.current = node;
  }, []);
  const termRef = useRef<Terminal | null>(null);
  const idRef = useRef<string | null>(null);
  const seqRef = useRef<number | null>(null);
  const startedRef = useRef(false);
  const epochRef = useRef(0);
  const resizeTimer = useRef<number | null>(null);

  // Which attempt a reply has to belong to: this device, this try, and this
  // state of the socket — so a reconnect reads as Connecting rather than
  // keeping the word the attempt before it earned.
  const key = `${deviceId}:${attempt}:${socketOpen}`;

  /* -------------------------------------------------------------- emulator */

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const term = new Terminal(terminalOptions);
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();
    termRef.current = term;

    const typed = term.onData((data) => {
      const terminalId = idRef.current;
      if (terminalId === null || data.length === 0) return;
      void rpc('terminal.input', {
        device_id: deviceId,
        terminal_id: terminalId,
        data: textToBase64(data),
      }).catch(() => undefined);
    });

    // Rotating a phone, raising a keyboard and dragging a window are all this
    // one path: fit to the box, then tell the device, once the dragging stops.
    const follow = () => {
      fit.fit();
      if (idRef.current === null) return;
      if (resizeTimer.current !== null) globalThis.clearTimeout(resizeTimer.current);
      resizeTimer.current = globalThis.setTimeout(() => {
        resizeTimer.current = null;
        const terminalId = idRef.current;
        if (terminalId === null) return;
        void rpc('terminal.resize', {
          device_id: deviceId,
          terminal_id: terminalId,
          ...sizeOf(term),
        }).catch(() => undefined);
      }, RESIZE_DEBOUNCE_MS) as unknown as number;
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(follow);
    observer?.observe(host);
    globalThis.addEventListener('resize', follow);

    return () => {
      globalThis.removeEventListener('resize', follow);
      observer?.disconnect();
      if (resizeTimer.current !== null) globalThis.clearTimeout(resizeTimer.current);
      typed.dispose();
      term.dispose();
      termRef.current = null;
    };
  }, [deviceId]);

  /* -------------------------------------------------------------- incoming */

  useEffect(
    () =>
      onTerminalFrame((frame) => {
        if (frame.device_id !== deviceId || frame.terminal_id !== idRef.current) return;
        if (frame.type === 'terminal.exited') {
          idRef.current = null;
          seqRef.current = null;
          forgetTerminal(deviceId);
          setExit({ code: frame.code });
          return;
        }
        if (seqRef.current !== null && frame.seq !== seqRef.current + 1) setGap(true);
        seqRef.current = frame.seq;
        termRef.current?.write(base64ToBytes(frame.data));
      }),
    [deviceId],
  );

  /* ------------------------------------------------------------ connecting */

  useEffect(() => {
    if (!enabled || !socketOpen || exit !== null) return;
    const requested = key;
    const epoch = (epochRef.current += 1);
    let cancelled = false;
    const live = (): boolean => !cancelled && epochRef.current === epoch;

    const adopt = (terminalId: string): void => {
      idRef.current = terminalId;
      startedRef.current = true;
      rememberTerminal(deviceId, terminalId);
      setStarted(true);
    };

    const run = async (): Promise<void> => {
      const size = sizeOf(termRef.current);
      const wanted = idRef.current ?? rememberedTerminal(deviceId);

      if (wanted !== null) {
        try {
          const result = await rpc('terminal.attach', {
            device_id: deviceId,
            terminal_id: wanted,
          });
          if (!live()) return;
          adopt(result.terminal_id);
          termRef.current?.reset();
          termRef.current?.write(base64ToBytes(result.scrollback));
          // §7.3: `seq` continues where it left off and this side cannot know
          // where that was, so the next frame sets the baseline.
          seqRef.current = null;
          setGap(false);
          setAnswered({ key: requested, outcome: { status: 'connected' } });
          if (result.cols !== size.cols || result.rows !== size.rows) {
            void rpc('terminal.resize', {
              device_id: deviceId,
              terminal_id: result.terminal_id,
              ...size,
            }).catch(() => undefined);
          }
          return;
        } catch (err) {
          if (!live()) return;
          if (!(err instanceof RequestError) || err.code !== 'not_found') {
            setAnswered({ key: requested, outcome: { status: 'disconnected', error: errorText(err) } });
            return;
          }
          // The ten minutes ran out, or the shell ended while the socket was gone.
          idRef.current = null;
          forgetTerminal(deviceId);
          if (startedRef.current) {
            setExit({ code: null });
            return;
          }
          // A remembered id from an earlier visit that is simply stale: the
          // person asked for a terminal, so they get one.
        }
      }

      try {
        const result = await rpc('terminal.open', {
          device_id: deviceId,
          cols: size.cols,
          rows: size.rows,
        });
        if (!live()) {
          void rpc('terminal.close', {
            device_id: deviceId,
            terminal_id: result.terminal_id,
          }).catch(() => undefined);
          return;
        }
        adopt(result.terminal_id);
        seqRef.current = 0;
        setGap(false);
        setAnswered({ key: requested, outcome: { status: 'connected' } });
      } catch (err) {
        if (!live()) return;
        setAnswered({ key: requested, outcome: { status: 'disconnected', error: errorText(err) } });
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [deviceId, enabled, socketOpen, exit, key]);

  /* ---------------------------------------------------------------- actions */

  const close = useCallback(() => {
    epochRef.current += 1;
    const terminalId = idRef.current;
    idRef.current = null;
    seqRef.current = null;
    forgetTerminal(deviceId);
    if (terminalId === null) return;
    void rpc('terminal.close', { device_id: deviceId, terminal_id: terminalId }).catch(
      () => undefined,
    );
  }, [deviceId]);

  // Leaving the page ends the shell: rule 20's Close, the browser's back and a
  // route change are all this one cleanup.
  useEffect(() => () => close(), [close]);

  const reconnect = useCallback(() => setAttempt((n) => n + 1), []);

  const restart = useCallback(() => {
    idRef.current = null;
    seqRef.current = null;
    startedRef.current = false;
    forgetTerminal(deviceId);
    setStarted(false);
    setExit(null);
    setGap(false);
    setAttempt((n) => n + 1);
  }, [deviceId]);

  /* ----------------------------------------------------------------- state */

  const outcome = answered?.key === key ? answered.outcome : null;
  const status: TerminalStatus =
    exit !== null
      ? 'exited'
      : !enabled || !socketOpen
        ? started
          ? 'disconnected'
          : 'connecting'
        : (outcome?.status ?? 'connecting');

  return {
    host,
    status,
    exitCode: exit?.code ?? null,
    error: outcome?.status === 'disconnected' ? outcome.error : null,
    gap,
    started,
    reconnect,
    restart,
    close,
  };
}
