/**
 * A41 §3.2 — what the mock gateway does with `PATCH /api/preferences`: it sets
 * the fields the body carries, refuses a value the contract does not allow and
 * ignores a field the contract has never heard of, so a tab of the mock is
 * refused exactly where the real gateway would refuse it.
 */
import { describe, expect, it } from 'vitest';
import { emptyPreferences, patchPreferences } from '../mock/preferences';

const held = () => emptyPreferences();

describe('the mock gateway preferences', () => {
  it('starts an account with the switch off and no field set', () => {
    expect(held()).toEqual({ resume_after_limit: false });
  });

  it('sets the fields the body carries and leaves the rest alone', () => {
    const outcome = patchPreferences(
      { resume_after_limit: true, language: 'en' },
      { language: 'zh-Hans' },
    );

    expect(outcome).toEqual({
      preferences: { resume_after_limit: true, language: 'zh-Hans' },
    });
  });

  it('takes every field of A41 in one request', () => {
    const outcome = patchPreferences(held(), {
      language: 'zh-Hans',
      stt_language: 'zh',
      polish_enabled: true,
      polish_model: 'gpt-5.4-mini',
      polish_strength: 'strong',
      timeline_detail: 'detailed',
    });

    expect(outcome).toEqual({
      preferences: {
        resume_after_limit: false,
        language: 'zh-Hans',
        stt_language: 'zh',
        polish_enabled: true,
        polish_model: 'gpt-5.4-mini',
        polish_strength: 'strong',
        timeline_detail: 'detailed',
      },
    });
  });

  it('allows an empty polish model, which is what "none chosen" reads as', () => {
    expect(patchPreferences(held(), { polish_model: '' })).toEqual({
      preferences: { resume_after_limit: false, polish_model: '' },
    });
  });

  it('ignores a field the contract does not know', () => {
    expect(patchPreferences(held(), { terminal_font_size: 14 })).toEqual({
      preferences: { resume_after_limit: false },
    });
  });

  it.each([
    ['language', 'fr'],
    ['polish_strength', 'gentle'],
    ['timeline_detail', 'everything'],
    ['stt_language', ''],
    ['stt_language', 'x'.repeat(33)],
    ['polish_model', 'x'.repeat(129)],
    ['polish_enabled', 'yes'],
    ['resume_after_limit', 1],
  ])('refuses %s: %s', (field, value) => {
    expect(patchPreferences(held(), { [field]: value })).toEqual({ error: field });
  });
});
