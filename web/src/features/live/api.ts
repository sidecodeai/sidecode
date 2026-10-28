/**
 * The one HTTP route of the voice assistant: whether the gateway offers it to this account, and
 * whether it can take a call from the account now. A gateway without the feature answers 404,
 * which reads as "off". The call itself is the `/ws/live` socket (`liveSocket.ts`).
 */

export interface LiveDraft {
  id: string;
  session: string;
  text: string;
}

/** What the gateway says of the assistant for an account. */
export interface LiveStatus {
  /** The account is offered the assistant: the entry is shown. */
  enabled: boolean;
  /** A call can be taken now. An account that is offered it and cannot have it is told why, in place of Start. */
  available: boolean;
  /** Why not, in the gateway's words (`subscription_required`, `quota_exceeded`, ...); null when it said none. */
  reason: string | null;
}

const OFF: LiveStatus = { enabled: false, available: false, reason: null };

/** Answers worth keeping for the life of the page, by account: a 200, or a 404 (no feature). */
const known = new Map<string, LiveStatus>();
const asking = new Map<string, Promise<LiveStatus>>();

/** Calls are offered only by an answer that says `available: true`; the reason is the gateway's word for why not. */
function read(body: Record<string, unknown>): LiveStatus {
  if (body.enabled !== true) return OFF;
  if (body.available === true) return { enabled: true, available: true, reason: null };
  return {
    enabled: true,
    available: false,
    reason: typeof body.reason === 'string' ? body.reason : null,
  };
}

/** What the gateway said, or `null` when it did not answer: not worth keeping. */
async function ask(): Promise<LiveStatus | null> {
  try {
    const res = await fetch('/api/live/status', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    if (res.status === 404) return OFF;
    if (!res.ok) return null;
    return read((await res.json()) as Record<string, unknown>);
  } catch {
    return null;
  }
}

/**
 * Ask the gateway, sharing a request already out. A good answer is kept for the account; when
 * there is none, the answer in force is the one kept before, or "off" for now.
 */
function request(username: string): Promise<LiveStatus> {
  const pending = asking.get(username);
  if (pending) return pending;
  const sent: Promise<LiveStatus> = ask().then((answer) => {
    if (asking.get(username) === sent) {
      asking.delete(username);
      if (answer !== null) known.set(username, answer);
    }
    return answer ?? known.get(username) ?? OFF;
  });
  asking.set(username, sent);
  return sent;
}

/** What an earlier answer said for this account, if the gateway has been asked and answered. */
export function knownLiveStatus(username: string): LiveStatus | undefined {
  return known.get(username);
}

/**
 * What the gateway says of the assistant for this account. It is asked once per page load for
 * each account; a failure reads as off for now and is asked again next time.
 */
export function fetchLiveStatus(username: string): Promise<LiveStatus> {
  const answered = known.get(username);
  return answered ? Promise.resolve(answered) : request(username);
}

/**
 * Ask again, whatever was kept: a "not now" can stop being true while the page is open (an
 * account change made elsewhere, a month that turned). A failure keeps the answer given before.
 */
export function refreshLiveStatus(username: string): Promise<LiveStatus> {
  return request(username);
}

/** Forget every answer, so the gateway is asked again. */
export function forgetLiveStatus(): void {
  known.clear();
  asking.clear();
}
