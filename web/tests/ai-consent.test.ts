/**
 * The permission, in this browser, for the AI features that send what the person says or writes
 * to a provider: dictation, polish and the voice assistant. It is kept per account and feature as
 * `<text version>|<provider name>`, asked again when the text has a new version or the provider
 * changes, withdrawn one feature at a time, and removed when the account signs out or is deleted
 * in this browser — not when the gateway ends the session. Who receives what comes from
 * `GET /api/config`, and absent names leave the unnamed words.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { providerNamesOf } from '../src/features/consent/providerNames';
import type { ConfigResponse } from '../src/lib/api';
import { providerName } from '../src/lib/providerName';
import {
  CONSENT_VERSIONS,
  forgetConsents,
  grantConsent,
  hasConsent,
  storedConsent,
  useConsent,
  withdrawConsent,
} from '../src/stores/aiConsent';
import { useAuth } from '../src/stores/auth';
import { storage } from '../src/stores/browserStorage';

beforeEach(() => {
  for (const account of ['alice', 'bob']) forgetConsents(account);
});

afterEach(() => {
  vi.unstubAllGlobals();
  useAuth.setState({ status: 'unknown', username: null, role: null });
});

describe('the stored permission', () => {
  it('has a version for each feature’s text', () => {
    expect(CONSENT_VERSIONS).toEqual({ dictation: 1, polish: 1, assistant: 2 });
  });

  it('keeps what was agreed to per account and feature: the text’s version and the provider named', () => {
    grantConsent('alice', 'dictation', 'OpenAI');
    grantConsent('alice', 'assistant', null);

    expect(storedConsent('alice', 'dictation')).toBe('1|OpenAI');
    expect(storedConsent('alice', 'assistant')).toBe('2|');
    expect(storedConsent('alice', 'polish')).toBeNull();
    expect(storedConsent('bob', 'dictation')).toBeNull();
  });

  it('holds for the same text naming the same provider, and asks again for any other', () => {
    grantConsent('alice', 'polish', 'OpenAI');
    expect(hasConsent('alice', 'polish', 'OpenAI')).toBe(true);
    expect(hasConsent('alice', 'polish', 'Anthropic')).toBe(false);
    expect(hasConsent('alice', 'polish', null)).toBe(false);

    grantConsent('alice', 'polish', null);
    expect(hasConsent('alice', 'polish', null)).toBe(true);
    expect(hasConsent('alice', 'polish', 'OpenAI')).toBe(false);
  });

  it('asks again when the text has a new version', () => {
    void storage.setItem('rc.consent.assistant.alice', '1|OpenAI');

    expect(hasConsent('alice', 'assistant', 'OpenAI')).toBe(false);
  });

  it('withdraws one feature and leaves the others', () => {
    grantConsent('alice', 'dictation', 'OpenAI');
    grantConsent('alice', 'polish', 'OpenAI');

    withdrawConsent('alice', 'dictation');

    expect(hasConsent('alice', 'dictation', 'OpenAI')).toBe(false);
    expect(hasConsent('alice', 'polish', 'OpenAI')).toBe(true);
  });

  it('follows every change on screen, and holds nothing for nobody signed in', () => {
    const { result, rerender } = renderHook(
      ({ username }: { username: string | null }) => useConsent(username, 'dictation', 'OpenAI'),
      { initialProps: { username: 'alice' as string | null } },
    );
    expect(result.current).toBe(false);

    act(() => grantConsent('alice', 'dictation', 'OpenAI'));
    expect(result.current).toBe(true);

    act(() => withdrawConsent('alice', 'dictation'));
    expect(result.current).toBe(false);

    act(() => grantConsent('alice', 'dictation', 'OpenAI'));
    rerender({ username: null });
    expect(result.current).toBe(false);
  });
});

describe('leaving the account', () => {
  beforeEach(() => {
    grantConsent('alice', 'dictation', 'OpenAI');
    grantConsent('alice', 'assistant', 'OpenAI');
    grantConsent('bob', 'dictation', 'OpenAI');
    useAuth.setState({ status: 'signed-in', username: 'alice', role: 'member' });
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"ok":true}', { status: 200 })));
  });

  it('removes every permission of the account at sign-out, and nobody else’s', async () => {
    await useAuth.getState().logout();

    expect(storedConsent('alice', 'dictation')).toBeNull();
    expect(storedConsent('alice', 'assistant')).toBeNull();
    expect(storedConsent('bob', 'dictation')).toBe('1|OpenAI');
  });

  it('removes them even when the gateway cannot be told of the sign-out', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    await expect(useAuth.getState().logout()).rejects.toThrow('offline');

    expect(storedConsent('alice', 'dictation')).toBeNull();
  });

  it('keeps them when the gateway ended the session', () => {
    useAuth.getState().markSignedOut();

    expect(storedConsent('alice', 'dictation')).toBe('1|OpenAI');
  });
});

describe('who receives what', () => {
  const config = (over: Partial<ConfigResponse> = {}): ConfigResponse => ({
    public_origin: 'https://gateway.example.com',
    stt: { enabled: true, languages: ['auto'] },
    push: { web_enabled: true, apns_enabled: false },
    version: '1.22.1',
    ...over,
  });

  it('reads the speech, polish and board providers the configuration names', () => {
    expect(
      providerNamesOf(
        config({
          stt: { enabled: true, languages: ['auto'], provider_name: 'OpenAI' },
          polish: { enabled: true, provider_name: 'OpenAI' },
          board: { enabled: true, provider_name: 'OpenAI' },
        }),
      ),
    ).toEqual({ dictation: 'OpenAI', polish: 'OpenAI', board: 'OpenAI' });
  });

  it('names nobody for a configuration that names none, or that the app has not read', () => {
    const none = { dictation: null, polish: null, board: null };
    expect(providerNamesOf(config({ polish: { enabled: true }, board: { enabled: false } }))).toEqual(none);
    expect(providerNamesOf(null)).toEqual(none);
  });

  it.each([
    [' OpenAI ', 'OpenAI'],
    ['', null],
    ['   ', null],
    [7, null],
    [null, null],
    ['x'.repeat(40), 'x'.repeat(40)],
    ['x'.repeat(41), null],
    ['Open\nAI', null],
    ['Open\u007fAI', null],
  ])('reads %j as %j', (value, name) => {
    expect(providerName(value)).toBe(name);
  });
});
