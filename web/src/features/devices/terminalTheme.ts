/**
 * A38 — how the emulator is dressed.
 *
 * The app is one light canvas (`docs/DESIGN.md`), so the terminal is light too:
 * a dark rectangle dropped into these screens would read as a different
 * application. The sixteen ANSI colours are therefore chosen for ink on paper —
 * the same greens, ambers and reds the rest of the app uses, darkened enough to
 * stay legible on a near-white background — rather than taken from a palette
 * meant for a black one.
 */
import type { ITerminalInitOnlyOptions, ITerminalOptions } from '@xterm/xterm';

export const TERMINAL_FONT_SIZE = 13;

/** §7.3 keeps 64 KiB on the device; the emulator holds its own lines. */
export const TERMINAL_SCROLLBACK = 5_000;

export const terminalTheme: ITerminalOptions['theme'] = {
  background: '#fafaf9',
  foreground: '#111111',
  cursor: '#111111',
  cursorAccent: '#fafaf9',
  selectionBackground: 'rgba(17, 17, 17, 0.14)',

  black: '#111111',
  red: '#c23a2c',
  green: '#1f7a4d',
  yellow: '#8a6100',
  blue: '#2a56b0',
  magenta: '#8a4fa0',
  cyan: '#1f6f79',
  white: '#6b6b6b',

  brightBlack: '#767570',
  brightRed: '#d23f31',
  brightGreen: '#22a06b',
  brightYellow: '#b07c00',
  brightBlue: '#1f4fa0',
  brightMagenta: '#7a3f92',
  brightCyan: '#12656f',
  brightWhite: '#111111',
};

export const terminalOptions: ITerminalOptions & ITerminalInitOnlyOptions = {
  allowProposedApi: false,
  cursorBlink: true,
  fontFamily:
    "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: TERMINAL_FONT_SIZE,
  lineHeight: 1.25,
  scrollback: TERMINAL_SCROLLBACK,
  theme: terminalTheme,
};
