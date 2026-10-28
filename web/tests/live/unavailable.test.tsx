/**
 * An account the gateway offers the assistant to and cannot take a call from. The gateway has
 * three reasons (no subscription, no allowance left, billing not set up), and the panel tells each
 * the same way wherever it comes from: the status, in place of Start; a refusal before the call;
 * the end of a call, which keeps what was said.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initialView } from '../../src/features/live/callState';
import { keepRecord, type CallRecord } from '../../src/features/live/history';
import { LiveEntry } from '../../src/features/live/LiveEntry';
import { liveTables } from '../../src/features/live/strings';
import { endedText, errorText, noteText } from '../../src/features/live/words';
import { FakeWebSocket } from './audioFakes';
import {
  afterLive,
  available,
  beforeLive,
  cannot,
  context,
  dial,
  en,
  endCall,
  gatewaySays,
  type LiveEnv,
  openPanel,
  openUnavailable,
  redial,
  renderEntry,
  startCall,
  status,
} from './harness';

let env: LiveEnv;

beforeEach(() => {
  env = beforeLive();
});

afterEach(afterLive);

/**
 * The three reasons the gateway gives about the account, the words each is told in, and the close
 * code each comes with. What a build offers for `subscription_required` is its own (without
 * anything, the account is said not to be offered the assistant): here it only has to be told,
 * and not as the assistant's intro.
 */
const REASONS = [
  ['subscription_required', /\S/, 4402],
  ['quota_exceeded', en.paused, 4402],
  ['billing_not_configured', en.unavailableNow, 4503],
] as const;

const said = (ws: FakeWebSocket, speaker: 'user' | 'assistant', delta: string): void =>
  act(() => ws.serverSend({ type: 'caption', speaker, delta }));

const bar = () => (
  <MemoryRouter>
    <LiveEntry />
  </MemoryRouter>
);

describe('the status says the assistant cannot be had', () => {
  it.each(REASONS)('tells %s in place of Start, and dials nothing', async (reason, words) => {
    gatewaySays(cannot(reason));
    await openUnavailable();
    expect(screen.getByText(words, { selector: '.live-note' })).toBeInTheDocument();
    expect(screen.queryByText(en.intro)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.close })).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('keeps the title and the Experimental tag, and shows the entry in the bar', async () => {
    gatewaySays(cannot('quota_exceeded'));
    await openUnavailable();
    expect(screen.getByRole('heading', { name: en.title })).toBeInTheDocument();
    expect(screen.getByText(en.experimental)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.open })).toBeInTheDocument();
  });

  // `not_allowed` says it outright; the others are a reason this page does not know, and none at all.
  it.each(['not_allowed', 'something_new', null])('says it is not offered to the account for %s', async (reason) => {
    gatewaySays(cannot(reason));
    await openUnavailable();
    expect(screen.getByText(en.errorNotAllowed)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument();
  });

  it('offers Start to an account that can have a call, and does not ask again when the panel opens', async () => {
    gatewaySays(available);
    await openPanel();
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
    expect(screen.getByText(en.intro)).toBeInTheDocument();
    // A "yes" is not asked again when the panel opens.
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('offers no Start to an account that the answer does not say can have a call', async () => {
    gatewaySays({ enabled: true });
    await openUnavailable();
    expect(screen.getByText(en.errorNotAllowed)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument();
  });

  it('shows no entry for an account that is not offered the assistant, whatever reason comes with it', async () => {
    gatewaySays({ enabled: false, available: false, reason: 'quota_exceeded' });
    const { container } = render(bar());
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('shows no line about where conversations are kept when there are none and no call can be made', async () => {
    gatewaySays(cannot('quota_exceeded'));
    await openUnavailable();
    expect(screen.queryByText(en.keptHere)).not.toBeInTheDocument();
    expect(screen.queryByText(en.clearHistory)).not.toBeInTheDocument();
  });

  it('still shows the conversations this browser kept, with the reason above them', async () => {
    const startedAt = Date.UTC(2023, 10, 14, 12, 0);
    const kept: CallRecord = {
      id: String(startedAt),
      startedAt,
      endedAt: startedAt + 30_000,
      reason: 'hangup',
      entries: [
        { kind: 'turn', speaker: 'user', text: 'An old question' },
        { kind: 'turn', speaker: 'assistant', text: 'An old answer' },
      ],
    };
    keepRecord('admin', kept);
    gatewaySays(cannot('quota_exceeded'));
    await openUnavailable();
    expect(screen.getByText(en.paused)).toBeInTheDocument();
    expect(screen.getByText('An old question')).toBeInTheDocument();
    expect(screen.getByText(en.keptHere)).toBeInTheDocument();
    expect(screen.getByText(en.clearHistory)).toBeInTheDocument();
  });

  describe('opening the panel on a "not now"', () => {
    it('asks again, and offers Start when the answer has changed', async () => {
      const fetcher = gatewaySays(cannot('quota_exceeded'), available);
      await openPanel();
      expect(await screen.findByRole('button', { name: en.start })).toBeInTheDocument();
      expect(screen.queryByText(en.paused)).not.toBeInTheDocument();
      expect(fetcher).toHaveBeenCalledTimes(2);
    });

    it('keeps the reason when the gateway cannot be asked, and does not ask for ever', async () => {
      const fetcher = vi
        .fn()
        .mockResolvedValueOnce(status(cannot('quota_exceeded')))
        .mockResolvedValue(status({ error: 'boom' }, { status: 500 }));
      vi.stubGlobal('fetch', fetcher);
      await openUnavailable();
      expect(screen.getByText(en.paused)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument();
      expect(fetcher).toHaveBeenCalledTimes(2);
    });

    it('is told a different reason when the gateway now says one', async () => {
      gatewaySays(cannot('quota_exceeded'), cannot('billing_not_configured'));
      await openUnavailable();
      expect(screen.getByText(en.unavailableNow)).toBeInTheDocument();
      expect(screen.queryByText(en.paused)).not.toBeInTheDocument();
    });

    it('takes the entry away when the gateway now says it is not offered', async () => {
      gatewaySays(cannot('quota_exceeded'), { enabled: false });
      await openUnavailable();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: en.open })).not.toBeInTheDocument();
    });

    it('asks on opening, not on the page coming up: a remounted bar asks nothing', async () => {
      const fetcher = gatewaySays(cannot('quota_exceeded'));
      const first = render(bar());
      await screen.findByRole('button', { name: en.open });
      first.unmount();
      render(bar());
      expect(screen.getByRole('button', { name: en.open })).toBeInTheDocument();
      expect(fetcher).toHaveBeenCalledTimes(1);
    });
  });
});

describe('a refusal before the call', () => {
  it.each(REASONS)('tells %s, lets the microphone go, and lets Start be pressed again', async (reason, words, code) => {
    await openPanel();
    const ws = await dial();
    act(() => {
      ws.serverSend({ type: 'ended', reason });
      ws.serverClose(code);
    });
    expect(screen.getByText(words, { selector: '.live-note' })).toBeInTheDocument();
    expect(screen.queryByText(en.intro)).not.toBeInTheDocument();
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    // The gateway's check is the enforcement, so a second try is the person's to make.
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
  });

  it('tells a refusal that comes with no word before the close as a service that could not be reached', async () => {
    await openPanel();
    const ws = await dial();
    act(() => ws.serverClose(4402));
    expect(screen.getByText(en.errorService)).toBeInTheDocument();
  });
});

describe('a call ended by the account itself', () => {
  it.each(REASONS)('tells %s and keeps what was said, in the panel and in the history', async (reason, words, code) => {
    const ws = await startCall();
    said(ws, 'user', 'What is running?');
    said(ws, 'assistant', 'Two sessions.');
    endCall(ws, reason, code);

    expect(screen.getByText(words, { selector: '.live-note' })).toBeInTheDocument();
    expect(screen.queryByText(en.intro)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.hangUp })).not.toBeInTheDocument();
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);

    expect(screen.getByText(en.lastConversation)).toBeInTheDocument();
    const log = screen.getByRole('log');
    expect(within(log).getByText('What is running?')).toBeInTheDocument();
    expect(within(log).getByText('Two sessions.')).toBeInTheDocument();
    const kept = JSON.parse(env.storage.get('live.history:admin') ?? '[]') as CallRecord[];
    expect(kept).toHaveLength(1);
    expect(kept[0]).toMatchObject({ reason });
    expect(kept[0]?.entries).toHaveLength(2);
  });

  it.each([
    ['subscription_required', en.endedNotAllowed],
    ['quota_exceeded', en.endedPaused],
    ['billing_not_configured', en.endedUnavailable],
  ])('words the saved call under its conversation for %s, of the call and not of the account now', async (reason, words) => {
    const ws = await startCall();
    said(ws, 'user', 'Anything pending?');
    endCall(ws, reason);
    expect(screen.getByText(words, { selector: '.live-record-note' })).toBeInTheDocument();
  });

  it('takes the call off the bar and keeps the entry', async () => {
    const ws = await startCall();
    endCall(ws, 'quota_exceeded', 4402);
    const entry = screen.getByRole('button', { name: en.open });
    expect(entry).toHaveAttribute('data-active', 'false');
    expect(entry.querySelector('.live-dot')).toBeNull();
  });
});

const closePanel = (): void => void fireEvent.click(screen.getByRole('button', { name: en.close }));

describe('a call the account itself ended, with the panel closed', () => {
  it.each(REASONS)('brings the panel up for %s, and then says why in place of Start', async (reason, words, code) => {
    const fetcher = gatewaySays(available, cannot(reason));
    const ws = await startCall();
    said(ws, 'user', 'What is running?');
    closePanel();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    endCall(ws, reason, code);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(words, { selector: '.live-note' })).toBeInTheDocument();
    // The gateway is asked how things stand, and its answer takes Start away.
    await waitFor(() => expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument());
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(screen.getByText(words, { selector: '.live-note' })).toBeInTheDocument();
    expect(within(screen.getByRole('log')).getByText('What is running?')).toBeInTheDocument();
  });

  it('brings it up once: closing it, and a page drawn after, do not bring it back', async () => {
    const fetcher = gatewaySays(available, cannot('quota_exceeded'));
    const ws = await startCall();
    closePanel();
    endCall(ws, 'quota_exceeded', 4402);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await act(async () => undefined);

    closePanel();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    cleanup();
    renderEntry();
    await screen.findByRole('button', { name: en.open });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('brings it up for a page that is drawn after the call ended, the first page to see it', async () => {
    const fetcher = gatewaySays(available, cannot('quota_exceeded'));
    const ws = await startCall();
    closePanel();
    cleanup();
    // The call ends while no page is drawn, as when the route changes.
    endCall(ws, 'quota_exceeded', 4402);
    renderEntry();
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(en.paused, { selector: '.live-note' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument());
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('brings it up again for the next call that the account ends', async () => {
    gatewaySays(available, cannot('quota_exceeded'), available, cannot('quota_exceeded'));
    const first = await startCall();
    closePanel();
    endCall(first, 'quota_exceeded', 4402);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await act(async () => undefined);
    closePanel();

    // The account can call again by the time the panel is opened; a second call is cut off the same way.
    fireEvent.click(screen.getByRole('button', { name: en.open }));
    await screen.findByRole('button', { name: en.start });
    const second = await redial();
    closePanel();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    endCall(second, 'quota_exceeded', 4402);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(en.paused, { selector: '.live-note' })).toBeInTheDocument();
  });

  it('leaves the panel closed, and the gateway unasked, for an end that is not about the account', async () => {
    const fetcher = gatewaySays(available);
    const ws = await startCall();
    closePanel();
    endCall(ws, 'idle');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe('a refusal before the call, after the panel was closed', () => {
  it.each(REASONS)('brings the panel up for %s, and then says why in place of Start', async (reason, words, code) => {
    const fetcher = gatewaySays(available, cannot(reason));
    await openPanel();
    const ws = await dial();
    closePanel();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    act(() => {
      ws.serverSend({ type: 'ended', reason });
      ws.serverClose(code);
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(words, { selector: '.live-note' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument());
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('asks the gateway too when the panel was open all the time, so Start does not sit beside the reason', async () => {
    gatewaySays(available, cannot('billing_not_configured'));
    await openPanel();
    const ws = await dial();
    act(() => {
      ws.serverSend({ type: 'ended', reason: 'billing_not_configured' });
      ws.serverClose(4503);
    });
    expect(screen.getByText(en.unavailableNow, { selector: '.live-note' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: en.start })).not.toBeInTheDocument());
  });

  it('keeps Start when the gateway still says the account can have a call', async () => {
    gatewaySays(available);
    await openPanel();
    const ws = await dial();
    act(() => {
      ws.serverSend({ type: 'ended', reason: 'quota_exceeded' });
      ws.serverClose(4402);
    });
    await act(async () => undefined);
    expect(screen.getByText(en.paused, { selector: '.live-note' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.start })).toBeInTheDocument();
  });

  it('leaves the panel closed for a refusal that is not about the account', async () => {
    const fetcher = gatewaySays(available);
    await openPanel();
    const ws = await dial();
    closePanel();
    act(() => {
      ws.serverSend({ type: 'ended', reason: 'cooldown' });
      ws.serverClose(4429);
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe('the words of the live feature itself', () => {
  it('name no plan: without a plan gate, the account is said not to be offered the assistant', () => {
    expect(JSON.stringify(liveTables)).not.toMatch(/\bPro\b|subscri|订阅/i);
    expect(errorText('subscription')).toBe(en.errorNotAllowed);
    expect(noteText(initialView, 'subscription')).toBe(en.errorNotAllowed);
    expect(endedText('subscription')).toBe(en.endedNotAllowed);
  });

  it('say the quota and the billing reasons softly, in both languages, with no number and no quota word', () => {
    for (const table of Object.values(liveTables)) {
      const told = [table.paused, table.unavailableNow, table.endedPaused, table.endedUnavailable, table.endedNotAllowed];
      for (const text of told) expect(text).not.toMatch(/\d|quota|allowance|token|额度|配额/i);
    }
    expect(liveTables['zh-Hans'].paused).toBe('此账户的语音助手暂时不可用，控制 Agent 与手动输入不受影响。');
    expect(liveTables['zh-Hans'].unavailableNow).toBe('语音助手暂时不可用，请稍后再试。');
  });
});
