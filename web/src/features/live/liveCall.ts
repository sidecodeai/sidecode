/**
 * The one call this page can hold, and everything it holds: the `/ws/live` socket, the microphone,
 * the audio context and the voice. It is owned by the page, not by a screen, so no route change
 * touches it. A call ends when the person hangs up, when the gateway ends it, when the account
 * changes or signs out, or when the page is closed or reloaded; nothing else lets go of it.
 */
import { useAuth } from '../../stores/auth';
import { MicCapture, MicError, openMicrophone } from './capture';
import type { CallAction } from './callState';
import { clearHistory, forgetHistory, readHistory } from './history';
import { LiveSocket, type ServerMessage } from './liveSocket';
import { PcmPlayer } from './player';
import { CallRecorder } from './recorder';
import { useLive } from './store';

/** After a hang-up the gateway gets this long to say goodbye before the socket is closed anyway. */
const FAREWELL_MS = 1_500;
/** The second click of a double click on Start lands on Hang up; for this long it is not one. */
const HANG_UP_GUARD_MS = 700;
/** How long a notice from the voice service stays on screen. */
const NOTICE_MS = 6_000;
/** How often, at most, the gateway is told what the voice has been through, when that has changed. */
const PLAYBACK_EVERY_MS = 5_000;

interface WithWebkit {
  webkitAudioContext?: typeof AudioContext;
}

interface Focus {
  device_id: string | null;
  session_id: string | null;
}

/** Everything one call holds. */
interface Call {
  context: AudioContext;
  stream: MediaStream;
  player: PcmPlayer;
  socket: LiveSocket;
  capture: MicCapture | null;
  farewell: ReturnType<typeof setTimeout> | null;
  /** The account the call belongs to: where its record is kept. */
  owner: string;
  /** Writes the call down once it is live; null before that. */
  recorder: CallRecorder | null;
  /** The focus last sent on this socket, so it is said only when it changes. */
  focus: string | null;
  /** What the gateway was last told of the voice, so it is told again only when that changed. */
  reported: string | null;
  /** Looks at it every `PLAYBACK_EVERY_MS` while the call is live. */
  reporter: ReturnType<typeof setInterval> | null;
  /** The gateway has said `ended`: nothing more plays or goes up, though the socket is not closed yet. */
  over: boolean;
}

let current: Call | null = null;
let starting = false;
let startedAt = 0;
/** Bumped when the page lets go or the person gives up, so a start still waiting knows. */
let attempt = 0;
let noticeTimer: ReturnType<typeof setTimeout> | null = null;
/** Where the person is, as the gateway is told: a session's chat, or nowhere in particular. */
let focus: Focus = { device_id: null, session_id: null };

const view = () => useLive.getState().view;
const apply = (action: CallAction): void => useLive.getState().dispatch(action);

function micCode(error: unknown): string {
  if (error instanceof MicError) return error.reason === 'denied' ? 'mic' : 'micUnavailable';
  return 'generic';
}

function stopMicrophone(call: Call): void {
  call.capture?.stop();
  call.stream.getTracks().forEach((track) => track.stop());
}

/** Let go of everything the call holds: socket, microphone, voice and audio context. */
function release(): void {
  const call = current;
  current = null;
  if (noticeTimer !== null) clearTimeout(noticeTimer);
  noticeTimer = null;
  if (!call) return;
  if (call.farewell !== null) clearTimeout(call.farewell);
  stopReporting(call);
  call.socket.close();
  stopMicrophone(call);
  call.player.flush();
  void call.context.close().catch(() => undefined);
}

/** Write the call down for good, and show it in the account's list when that account is on screen. */
function finishRecording(call: Call, reason: string | null): void {
  const recorder = call.recorder;
  call.recorder = null;
  const records = recorder?.finish(view().transcript, reason);
  if (records && call.owner === useAuth.getState().username) {
    useLive.getState().keep(call.owner, records);
  }
}

/** Tell the gateway where the person is, when that is not what it was last told. */
function sendFocus(call: Call): void {
  const key = JSON.stringify(focus);
  if (call.over || call.focus === key) return;
  call.focus = key;
  call.socket.send({ type: 'focus', ...focus });
}

/**
 * Which session's chat is on screen: both ids, or both null anywhere else. Kept for a call that
 * starts later, and sent on a live one when it changes.
 */
export function setFocus(deviceId: string | null, sessionId: string | null): void {
  focus =
    deviceId && sessionId
      ? { device_id: deviceId, session_id: sessionId }
      : { device_id: null, session_id: null };
  if (current && view().phase === 'live') sendFocus(current);
}

/**
 * Tell the gateway what the voice has been through (stalls, the silence they made, restarts, the
 * delay in force), when that is not what it was last told, or always when `force`.
 */
function reportPlayback(call: Call, force = false): void {
  if (call.over) return;
  const stats = call.player.stats;
  const key = JSON.stringify(stats);
  if (!force && key === call.reported) return;
  call.reported = key;
  call.socket.send({ type: 'playback', ...stats });
}

function stopReporting(call: Call): void {
  if (call.reporter !== null) clearInterval(call.reporter);
  call.reporter = null;
}

/** The last word to the gateway: where the voice stands, if the call got as far as having one, then the hang-up. */
function sayHangup(call: Call): void {
  if (call.reporter !== null) reportPlayback(call, true);
  stopReporting(call);
  call.socket.send({ type: 'hangup' });
}

/** Mute replaces the microphone's frames with silence of the same length: the cadence goes on. */
function frameToSend(frame: ArrayBuffer): ArrayBuffer {
  return view().muted ? new ArrayBuffer(frame.byteLength) : frame;
}

async function beginCapture(call: Call): Promise<void> {
  const capture = new MicCapture(call.context, call.stream, (frame) => {
    call.socket.sendAudio(frameToSend(frame));
  });
  call.capture = capture;
  try {
    await capture.start();
  } catch (error) {
    if (current === call) {
      release();
      finishRecording(call, 'error');
      apply({ type: 'failed', code: micCode(error) });
    }
    return;
  }
  if (current !== call) capture.stop();
}

function expireNoticeSoon(): void {
  if (noticeTimer !== null) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    noticeTimer = null;
    apply({ type: 'noticeExpired' });
  }, NOTICE_MS);
}

/** The gateway has said `ready`: the whole record is wanted, and the microphone may be tapped. */
function begin(call: Call): void {
  call.socket.send({ type: 'captions', enabled: true });
  sendFocus(call);
  reportPlayback(call, true);
  call.reporter = setInterval(() => reportPlayback(call), PLAYBACK_EVERY_MS);
  if (call.owner) call.recorder = new CallRecorder(call.owner, Date.now());
  void beginCapture(call);
}

function onMessage(call: Call, message: ServerMessage): void {
  if (current !== call) return;
  if (message.type === 'flush') {
    call.player.flush();
    return;
  }
  if (message.type === 'ready') begin(call);
  if (message.type === 'ended') {
    call.over = true;
    stopReporting(call);
    stopMicrophone(call);
  }
  const before = view().transcript;
  apply({ type: 'message', message });
  const after = view().transcript;
  if (after !== before) call.recorder?.note(after);
  if (message.type === 'notice') expireNoticeSoon();
}

/**
 * A browser can refuse the audio context, the microphone or the socket, the first and last by
 * throwing: the person is told, and nothing is left held.
 */
async function open(Context: typeof AudioContext, mine: number): Promise<void> {
  let context: AudioContext | null = null;
  let stream: MediaStream;
  try {
    // Made inside the click, so the browser lets it play.
    context = new Context({ latencyHint: 'interactive' });
    await context.resume();
    stream = await openMicrophone();
  } catch (error) {
    void context?.close().catch(() => undefined);
    if (attempt === mine) apply({ type: 'failed', code: micCode(error) });
    return;
  }
  if (attempt !== mine) {
    // The person gave up, or the page left, while the browser was still asking for the microphone.
    stream.getTracks().forEach((track) => track.stop());
    void context.close().catch(() => undefined);
    return;
  }
  const slot: { call: Call | null } = { call: null };
  const live = (): Call | null => (slot.call && current === slot.call ? slot.call : null);
  const socket = new LiveSocket({
    onMessage: (message) => {
      if (slot.call) onMessage(slot.call, message);
    },
    onAudio: (frame) => {
      const call = live();
      if (call && !call.over && view().phase === 'live') call.player.enqueue(frame);
    },
    onClose: ({ ended }) => {
      const call = live();
      if (!call) return;
      release();
      finishRecording(call, ended);
      apply({ type: 'closed', ended, call: mine });
    },
  });
  const call: Call = {
    context,
    stream,
    player: new PcmPlayer(context),
    socket,
    capture: null,
    farewell: null,
    owner: useAuth.getState().username ?? '',
    recorder: null,
    focus: null,
    reported: null,
    reporter: null,
    over: false,
  };
  slot.call = call;
  current = call;
  try {
    socket.open();
  } catch {
    release();
    apply({ type: 'failed', code: 'service' });
  }
}

export async function startCall(): Promise<void> {
  if (current || starting) return;
  starting = true;
  startedAt = Date.now();
  attempt += 1;
  const mine = attempt;
  apply({ type: 'connecting' });
  try {
    const Context =
      globalThis.AudioContext ?? (globalThis as unknown as WithWebkit).webkitAudioContext;
    if (!Context) {
      apply({ type: 'failed', code: 'micUnavailable' });
      return;
    }
    await open(Context, mine);
  } finally {
    if (attempt === mine) starting = false;
  }
}

export function hangUp(): void {
  const { phase } = view();
  if (phase === 'connecting') {
    if (Date.now() - startedAt < HANG_UP_GUARD_MS) return;
    // Nothing has been said yet: give up at once. A microphone still being asked for is let
    // go of when it is granted, because the attempt it belongs to is over.
    attempt += 1;
    starting = false;
    release();
    apply({ type: 'cancelled' });
    return;
  }
  const call = current;
  if (!call || phase !== 'live') return;
  apply({ type: 'ending' });
  sayHangup(call);
  // The microphone and the voice stop at once; the gateway's goodbye is waited for briefly.
  stopMicrophone(call);
  call.player.flush();
  call.farewell = setTimeout(() => {
    if (current !== call) return;
    release();
    finishRecording(call, 'hangup');
    apply({ type: 'closed', ended: 'hangup' });
  }, FAREWELL_MS);
}

export function sendDraft(): void {
  const { draft } = view();
  if (!draft || !current) return;
  apply({ type: 'drafting', id: draft.id });
  current.socket.send({ type: 'draft.send', draft_id: draft.id });
}

export function cancelDraft(): void {
  const { draft } = view();
  if (!draft || !current) return;
  current.socket.send({ type: 'draft.cancel', draft_id: draft.id });
}

export function setMuted(on: boolean): void {
  if (current && view().phase === 'live') apply({ type: 'muted', on });
}

/**
 * The account or the page is going: tell the gateway, then let go of everything at once. The view
 * follows, for a page that comes back from the browser's cache to find the call gone.
 */
function leave(reason: string): void {
  attempt += 1;
  starting = false;
  const call = current;
  if (call) sayHangup(call);
  release();
  if (call) finishRecording(call, reason);
  if (view().phase === 'connecting') apply({ type: 'cancelled' });
  else if (call) apply({ type: 'closed', ended: reason });
}

function accountChanged(): void {
  leave('signed_out');
  forgetHistory();
  useLive.getState().reset();
}

/**
 * The two things that end a call from outside it: the signed-in account changing (or going), and
 * the page being closed or reloaded. Installed by the host, once; the call itself never depends on
 * who is watching.
 */
export function watchLifecycle(): () => void {
  const unsubscribe = useAuth.subscribe((state, previous) => {
    if (state.username !== previous.username) accountChanged();
  });
  const onPageHide = (): void => leave('page_closed');
  window.addEventListener('pagehide', onPageHide);
  return () => {
    unsubscribe();
    window.removeEventListener('pagehide', onPageHide);
  };
}

/** Show the signed-in account's saved conversations, as the browser holds them now. */
export function loadSaved(): void {
  const username = useAuth.getState().username;
  if (username) useLive.getState().keep(username, readHistory(username));
}

export function clearSaved(): void {
  const username = useAuth.getState().username;
  if (!username) return;
  clearHistory(username);
  useLive.getState().keep(username, []);
}
