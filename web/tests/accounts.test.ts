/**
 * A33 — the words a device page puts on an account and its windows.
 * `docs/DESIGN.md` § "A device has a page" and § "Quota is a meter, drawn for
 * accounts only": the vendor comes from a table of three ids, everything else
 * the device reported is printed as it arrived, and the meter's colour band is
 * the ink colour, the warning colour past 80 and the danger colour at 100.
 */
import { afterEach, describe, expect, it } from 'vitest';
import {
  meterTone,
  resetsText,
  signInLine,
  usedPercent,
  windowName,
} from '../src/features/devices/accounts';
import { useSettings } from '../src/stores/settings';
import { strings, vendorLabel } from '../src/strings';
import type { AgentAccount } from '../src/protocol/types';

afterEach(() => useSettings.setState({ language: 'en' }));

describe('the vendor a credential belongs to', () => {
  it('names the three ids the apps know', () => {
    expect(vendorLabel('anthropic')).toBe('Anthropic');
    expect(vendorLabel('openai')).toBe('OpenAI');
    expect(vendorLabel('xai')).toBe('xAI');
  });

  it('prints an id it does not know as itself', () => {
    expect(vendorLabel('mistral')).toBe('mistral');
  });
});

describe('the sign-in line', () => {
  it('reads vendor, plan, tier and email for an account', () => {
    const account: AgentAccount = {
      provider: 'anthropic',
      method: 'account',
      plan: 'max',
      tier: 'Max 5x',
      email: 'me@example.com',
    };
    expect(signInLine(account)).toBe('Anthropic account · Max · Max 5x · me@example.com');
  });

  it('raises the plan first letter and leaves the tier exactly as reported', () => {
    expect(
      signInLine({ provider: 'openai', method: 'account', plan: 'pro', tier: 'gpt-5.4 priority' }),
    ).toBe('OpenAI account · Pro · gpt-5.4 priority');
  });

  it('draws nothing for what the device did not report', () => {
    expect(signInLine({ provider: 'xai', method: 'account', plan: null })).toBe('xAI account');
  });

  it('leads a key with the vendor too, and names a third-party host when there is one', () => {
    expect(signInLine({ provider: 'anthropic', method: 'api_key' })).toBe('Anthropic API key');
    expect(signInLine({ provider: 'openai', method: 'api_key', endpoint: 'api.relay.example' })).toBe(
      'OpenAI API key · api.relay.example',
    );
    expect(signInLine({ provider: 'mistral', method: 'api_key' })).toBe('mistral API key');
  });

  it('keeps the plan, the tier and the email untranslated in Chinese', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(signInLine({ provider: 'anthropic', method: 'account', plan: 'max', email: 'me@example.com' })).toBe(
      'Anthropic 账户 · Max · me@example.com',
    );
  });
});

describe('a window is named by its length', () => {
  it.each([
    [300, '5-hour'],
    [1440, '24-hour'],
    [10080, '7-day'],
    [60, '1-hour'],
    [20160, '14-day'],
    [90, '90-minute'],
  ])('reads %i minutes as %s', (minutes, expected) => {
    expect(windowName({ window_minutes: minutes, used_percent: 0 })).toBe(expected);
  });

  it('puts what the window is confined to after it', () => {
    expect(windowName({ window_minutes: 10080, scope: 'Fable', used_percent: 64 })).toBe(
      '7-day · Fable',
    );
  });

  it('names the same window in Chinese', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(windowName({ window_minutes: 300, used_percent: 0 })).toBe('5 小时');
  });
});

describe('the meter', () => {
  it('is the ink colour up to 80, the warning colour past it, the danger colour at 100', () => {
    expect(meterTone(0)).toBe('ink');
    expect(meterTone(80)).toBe('ink');
    expect(meterTone(80.5)).toBe('warn');
    expect(meterTone(99)).toBe('warn');
    expect(meterTone(100)).toBe('danger');
  });

  it('draws a whole percentage inside the track', () => {
    expect(usedPercent({ window_minutes: 300, used_percent: 16.4 })).toBe(16);
    expect(usedPercent({ window_minutes: 300, used_percent: 120 })).toBe(100);
    expect(usedPercent({ window_minutes: 300, used_percent: -1 })).toBe(0);
  });
});

describe('when a window resets', () => {
  const now = new Date(2026, 8, 15, 9, 0).getTime();

  it('gives the clock alone for a reset later today', () => {
    expect(resetsText(new Date(2026, 8, 15, 15, 40).getTime(), now)).toBe('resets 15:40');
  });

  it('names the day for a reset on another one', () => {
    expect(resetsText(new Date(2026, 8, 22, 22, 0).getTime(), now)).toBe('resets Tue 22:00');
  });

  it('says it in Chinese with the same clock', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(resetsText(new Date(2026, 8, 15, 15, 40).getTime(), now)).toBe('15:40 重置');
    expect(strings.devicePage.checking).toBe('检查中…');
  });
});
