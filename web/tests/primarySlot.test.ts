/**
 * `docs/DESIGN.md` § "The composer" → **Done becomes a spinner, and the spinner
 * becomes Send**, over every pair of a dictation phase and a polish phase. The
 * composer draws nothing in that slot except what this function returns, so the
 * whole rule can be read here.
 */
import { describe, expect, it } from 'vitest';
import { primarySlot, type PolishPhase, type PrimarySlot } from '../src/features/voice/primarySlot';
import type { VoiceState } from '../src/features/voice/useVoice';

const VOICE: VoiceState[] = ['idle', 'starting', 'listening', 'finishing', 'error'];
const POLISH: PolishPhase[] = ['idle', 'polishing', 'polished', 'failed'];

/** Every pair, spelled out rather than derived, so the table is the test. */
const EXPECTED: Record<VoiceState, Record<PolishPhase, PrimarySlot>> = {
  idle: { idle: 'send', polishing: 'working', polished: 'send', failed: 'send' },
  starting: { idle: 'done', polishing: 'done', polished: 'done', failed: 'done' },
  listening: { idle: 'done', polishing: 'done', polished: 'done', failed: 'done' },
  finishing: { idle: 'working', polishing: 'working', polished: 'working', failed: 'working' },
  error: { idle: 'send', polishing: 'working', polished: 'send', failed: 'send' },
};

describe('the composer primary slot', () => {
  it('holds what the ruling says for every pair', () => {
    for (const voice of VOICE) {
      for (const polish of POLISH) {
        expect([voice, polish, primarySlot(voice, polish)]).toEqual([
          voice,
          polish,
          EXPECTED[voice][polish],
        ]);
      }
    }
  });

  it('gives a live dictation the way out of it', () => {
    expect(primarySlot('listening', 'idle')).toBe('done');
    expect(primarySlot('starting', 'idle')).toBe('done');
  });

  it('waits from the click on Done until the last word is in the field', () => {
    // Done, then the final transcript arrives and the model takes it, then the
    // answer lands: one uninterrupted spinner, and only then Send.
    expect(primarySlot('finishing', 'idle')).toBe('working');
    expect(primarySlot('idle', 'polishing')).toBe('working');
    expect(primarySlot('idle', 'polished')).toBe('send');
  });

  it('offers Send the moment the field holds what will be sent', () => {
    // Polish off, or not offered: the final transcript is the words themselves.
    expect(primarySlot('idle', 'idle')).toBe('send');
    // A failed polish leaves the dictated words, which are sendable as they are.
    expect(primarySlot('idle', 'failed')).toBe('send');
  });
});
