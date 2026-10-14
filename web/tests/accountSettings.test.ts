/**
 * A24: what this app remembers belongs to the person signed in, not to the
 * browser. Two people who share one browser keep their own language, polish
 * choices and timeline detail. `docs/DESIGN.md` § "Accounts".
 *
 * The store keeps one preference store for the whole module, so each test uses
 * account names of its own rather than clearing something it does not own.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { readSettingsFor, useSettings } from '../src/stores/settings';

const defaults = {
  language: 'en',
  timelineDetail: 'simple',
  // A29: dictation polish is off, with no model and the gentler strength.
  polishEnabled: false,
  polishModel: '',
  polishStrength: 'moderate',
};

const current = () => ({
  language: useSettings.getState().language,
  timelineDetail: useSettings.getState().timelineDetail,
  polishEnabled: useSettings.getState().polishEnabled,
  polishModel: useSettings.getState().polishModel,
  polishStrength: useSettings.getState().polishStrength,
});

beforeEach(() => {
  readSettingsFor(null);
});

describe('per-account settings', () => {
  it("gives a new account the defaults, not the last person's choices", () => {
    readSettingsFor('one.a');
    useSettings.getState().setLanguage('zh-Hans');
    useSettings.getState().setPolishEnabled(true);
    useSettings.getState().setPolishModel('gpt-4.1-mini');
    useSettings.getState().setPolishStrength('strong');

    readSettingsFor('one.b');
    expect(current()).toEqual(defaults);
  });

  it('reads an account its own choices back when it signs in again', () => {
    readSettingsFor('two.a');
    useSettings.getState().setLanguage('zh-Hans');
    useSettings.getState().setTimelineDetail('detailed');
    useSettings.getState().setPolishModel('gpt-4.1');

    readSettingsFor('two.b');
    expect(current()).toEqual(defaults);

    readSettingsFor('two.a');
    expect(current()).toEqual({
      ...defaults,
      language: 'zh-Hans',
      timelineDetail: 'detailed',
      polishModel: 'gpt-4.1',
    });
  });

  it('keeps the login screen, where nobody is signed in, on its own key', () => {
    useSettings.getState().setLanguage('zh-Hans');

    readSettingsFor('three.a');
    expect(useSettings.getState().language).toBe('en');

    readSettingsFor(null);
    expect(useSettings.getState().language).toBe('zh-Hans');
  });
});
