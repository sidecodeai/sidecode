/**
 * Base64, the way the wire uses it.
 *
 * `session.send` carries attachments as base64 and the device computes the size
 * (amendment A3), so an optimistic row has to work it out for itself. A38 sends
 * terminal bytes both ways in the same encoding, and those are bytes rather
 * than text: what a shell writes is not always valid UTF-8, and decoding it
 * here would corrupt what the emulator is meant to interpret.
 */

/** Bytes per `btoa` call. Spreading a whole 64 KiB frame overflows the stack. */
const CHUNK = 0x8000;

export function base64Size(data: string): number {
  const padding = data.endsWith('==') ? 2 : data.endsWith('=') ? 1 : 0;
  return Math.max(0, Math.floor(data.length / 4) * 3 - padding);
}

/** A38: one `terminal.output` frame's payload, as the emulator wants it. */
export function base64ToBytes(data: string): Uint8Array {
  const binary = atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/** A38: what the emulator reports a keystroke as, encoded for `terminal.input`. */
export function textToBase64(text: string): string {
  return bytesToBase64(new TextEncoder().encode(text));
}
