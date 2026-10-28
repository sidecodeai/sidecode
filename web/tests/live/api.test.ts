import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchLiveStatus,
  forgetLiveStatus,
  knownLiveStatus,
  refreshLiveStatus,
  type LiveStatus,
} from '../../src/features/live/api';

function reply(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function gateway(...answers: (Response | Error)[]) {
  const queue = [...answers];
  const fetcher = vi.fn(async () => {
    const next = queue.shift() ?? reply(200, ON);
    if (next instanceof Error) throw next;
    return next;
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

const ON: LiveStatus = { enabled: true, available: true, reason: null };
const OFF: LiveStatus = { enabled: false, available: false, reason: null };
const unavailable = (reason: string | null): LiveStatus => ({ enabled: true, available: false, reason });

beforeEach(() => {
  forgetLiveStatus();
  vi.unstubAllGlobals();
});

describe('the status answer', () => {
  it('asks the gateway once for an account and remembers a yes', async () => {
    const fetcher = gateway(reply(200, ON));
    expect(knownLiveStatus('admin')).toBeUndefined();
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(knownLiveStatus('admin')).toEqual(ON);
  });

  it('remembers a no, and a gateway without the feature (404) as a no', async () => {
    const fetcher = gateway(reply(200, { enabled: false }), reply(404, { error: 'not_found' }));
    expect(await fetchLiveStatus('erin')).toEqual(OFF);
    expect(await fetchLiveStatus('erin')).toEqual(OFF);
    expect(await fetchLiveStatus('frank')).toEqual(OFF);
    expect(await fetchLiveStatus('frank')).toEqual(OFF);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(knownLiveStatus('erin')).toEqual(OFF);
    expect(knownLiveStatus('frank')).toEqual(OFF);
  });

  it('keeps one answer per account', async () => {
    const fetcher = gateway(reply(200, ON), reply(200, { enabled: false }));
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    expect(await fetchLiveStatus('dana')).toEqual(OFF);
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('shares one request between callers that ask at the same time', async () => {
    const fetcher = gateway(reply(200, ON));
    const answers = await Promise.all([fetchLiveStatus('admin'), fetchLiveStatus('admin')]);
    expect(answers).toEqual([ON, ON]);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a network failure', new Error('offline')],
    ['a server error', reply(500, { error: 'boom' })],
    ['a login that has lapsed', reply(401, { error: 'unauthorized' })],
    ['an answer that is not JSON', new Response('<html>', { status: 200 })],
    ['an answer that is not an object', reply(200, null)],
  ])('reads %s as off for now and asks again next time', async (_name, failure) => {
    const fetcher = gateway(failure, reply(200, ON));
    expect(await fetchLiveStatus('admin')).toEqual(OFF);
    expect(knownLiveStatus('admin')).toBeUndefined();
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('forgets everything on request', async () => {
    const fetcher = gateway(reply(200, ON), reply(200, { enabled: false }));
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    forgetLiveStatus();
    expect(await fetchLiveStatus('admin')).toEqual(OFF);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});

describe('what the answer says', () => {
  it('reads an answer that says offered and available as an account that can have calls', async () => {
    gateway(reply(200, { enabled: true, available: true, reason: null }));
    expect(await fetchLiveStatus('admin')).toEqual(ON);
  });

  it.each([
    ['names no `available`', { enabled: true }],
    ['says `available` is null', { enabled: true, available: null }],
    ['says `available` is the text "true"', { enabled: true, available: 'true' }],
  ])('offers no call when the answer %s', async (_name, body) => {
    gateway(reply(200, body));
    expect(await fetchLiveStatus('admin')).toEqual(unavailable(null));
  });

  it.each(['subscription_required', 'quota_exceeded', 'billing_not_configured', 'not_allowed'])(
    'reads an account that is offered the assistant and cannot have it (%s) with its reason',
    async (reason) => {
      gateway(reply(200, { enabled: true, available: false, reason }));
      expect(await fetchLiveStatus('admin')).toEqual(unavailable(reason));
    },
  );

  it('keeps a reason this page does not know, and none when the gateway gives none', async () => {
    gateway(
      reply(200, { enabled: true, available: false, reason: 'something_new' }),
      reply(200, { enabled: true, available: false }),
      reply(200, { enabled: true, available: false, reason: 7 }),
    );
    expect(await fetchLiveStatus('a')).toEqual(unavailable('something_new'));
    expect(await fetchLiveStatus('b')).toEqual(unavailable(null));
    expect(await fetchLiveStatus('c')).toEqual(unavailable(null));
  });

  it('shows no entry for an account that is not offered the assistant, whatever else the answer says', async () => {
    gateway(reply(200, { enabled: false, available: true, reason: null }));
    expect(await fetchLiveStatus('admin')).toEqual(OFF);
  });

  it('drops a reason from an answer that says available', async () => {
    gateway(reply(200, { enabled: true, available: true, reason: 'quota_exceeded' }));
    expect(await fetchLiveStatus('admin')).toEqual(ON);
  });
});

describe('asking again', () => {
  it('asks whatever was kept, and keeps the new answer', async () => {
    const fetcher = gateway(
      reply(200, { enabled: true, available: false, reason: 'subscription_required' }),
      reply(200, { enabled: true, available: true }),
    );
    expect(await fetchLiveStatus('admin')).toEqual(unavailable('subscription_required'));
    expect(await refreshLiveStatus('admin')).toEqual(ON);
    expect(knownLiveStatus('admin')).toEqual(ON);
    expect(await fetchLiveStatus('admin')).toEqual(ON);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('keeps what was said before when the gateway does not answer', async () => {
    gateway(
      reply(200, { enabled: true, available: false, reason: 'quota_exceeded' }),
      reply(500, { error: 'boom' }),
    );
    expect(await fetchLiveStatus('admin')).toEqual(unavailable('quota_exceeded'));
    expect(await refreshLiveStatus('admin')).toEqual(unavailable('quota_exceeded'));
    expect(knownLiveStatus('admin')).toEqual(unavailable('quota_exceeded'));
  });

  it('shares a request already out', async () => {
    const fetcher = gateway(reply(200, ON));
    const answers = await Promise.all([fetchLiveStatus('admin'), refreshLiveStatus('admin')]);
    expect(answers).toEqual([ON, ON]);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
