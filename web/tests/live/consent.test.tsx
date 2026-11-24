/**
 * A65 — the voice assistant's permission, which the web had none of before. Start talking asks the
 * gateway again, then, without a permission for the service it names now in the second version of
 * the words, asks for one: nothing is asked of the microphone and no call is opened before Agree
 * and continue, which starts the call; Not now starts nothing. The words name the service the
 * gateway names. The Voice assistant switch in Settings reads the permission: on asks, off
 * withdraws it and ends a call that is open.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { LiveSettingRow } from '../../src/features/live/LiveSettingRow';
import { useLive } from '../../src/features/live/store';
import { forgetConsents, grantConsent, storedConsent } from '../../src/stores/aiConsent';
import { storage } from '../../src/stores/browserStorage';
import { FakeWebSocket } from './audioFakes';
import {
  afterLive,
  available,
  beforeLive,
  en,
  gatewaySays,
  type LiveEnv,
  openPanel,
  startCall,
} from './harness';

let env: LiveEnv;

beforeEach(() => {
  env = beforeLive();
  // This suite is about the permission itself, so the account starts without it.
  forgetConsents('admin');
});

afterEach(() => {
  afterLive();
});

const start = () => userEvent.click(screen.getByRole('button', { name: en.start }));
const consent = () => screen.findByRole('dialog', { name: en.consentHeading });

describe('the permission before the first call', () => {
  it('is asked at Start, before the microphone or the call, in the unnamed words of a gateway that names nobody', async () => {
    await openPanel();
    await start();

    const dialog = await consent();
    expect(within(dialog).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      en.consentVoice,
      en.consentSessions,
      en.consentDrafts,
    ]);
    const paragraphs = [...dialog.querySelectorAll('.consent-paragraph')].map((p) => p.textContent);
    expect(paragraphs).toEqual([en.consentGoesTo(null), en.consentSend, en.consentWithdraw]);
    expect(env.fakes.getUserMedia).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('starts the call on Agree and continue, for the service named', async () => {
    await openPanel();
    await start();
    await userEvent.click(within(await consent()).getByRole('button', { name: 'Agree and continue' }));

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(storedConsent('admin', 'assistant')).toBe('2|');
  });

  it('starts nothing on Not now, and asks again at the next Start', async () => {
    await openPanel();
    await start();
    await userEvent.click(within(await consent()).getByRole('button', { name: 'Not now' }));

    expect(env.fakes.getUserMedia).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(0);
    await start();
    expect(await consent()).toBeInTheDocument();
  });

  it('names the service the gateway names', async () => {
    gatewaySays({ ...available, provider_name: 'Example AI' });
    await openPanel();
    await start();

    const dialog = await consent();
    const paragraphs = [...dialog.querySelectorAll('.consent-paragraph')].map((p) => p.textContent);
    expect(paragraphs).toEqual([en.consentGoesTo('Example AI'), en.consentSend, en.consentWithdraw]);
    expect(within(dialog).queryByRole('link')).toBeNull();
  });

  it('asks everyone who agreed to the first version of the words again', async () => {
    void storage.setItem('rc.consent.assistant.admin', '1|');
    await openPanel();
    await start();

    expect(await consent()).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('asks again when the gateway names another service at Start than when the panel opened', async () => {
    grantConsent('admin', 'assistant', 'OpenAI');
    gatewaySays({ ...available, provider_name: 'OpenAI' }, { ...available, provider_name: 'Anthropic' });
    await openPanel();
    await start();

    const dialog = await consent();
    expect(within(dialog).getByText(en.consentGoesTo('Anthropic'))).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('starts with no question while the permission held is for the service named', async () => {
    grantConsent('admin', 'assistant', null);
    await openPanel();
    await start();

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(screen.queryByRole('dialog', { name: en.consentHeading })).toBeNull();
  });
});

describe('the Voice assistant switch in Settings', () => {
  const row = () =>
    render(
      <MemoryRouter>
        <LiveSettingRow />
      </MemoryRouter>,
    );
  const toggle = () => screen.findByRole('switch', { name: en.title });

  it('reads the permission and names what it sends, asking before it turns on', async () => {
    row();

    expect(await toggle()).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(en.settingNote(null))).toBeInTheDocument();
    await userEvent.click(await toggle());
    await userEvent.click(within(await consent()).getByRole('button', { name: 'Agree and continue' }));

    expect(await toggle()).toHaveAttribute('aria-checked', 'true');
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('withdraws the permission when turned off, and ends a call that is open', async () => {
    grantConsent('admin', 'assistant', null);
    await startCall();
    row();

    await userEvent.click(await toggle());

    expect(storedConsent('admin', 'assistant')).toBeNull();
    await waitFor(() => expect(useLive.getState().view.phase).toBe('ending'));
  });

  it('is not drawn where the gateway offers no assistant and nothing is held', async () => {
    gatewaySays({ enabled: false });
    row();
    await act(async () => undefined);

    expect(screen.queryByRole('switch', { name: en.title })).toBeNull();
  });
});
