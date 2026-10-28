import { AudioLines, MicOff } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../../stores/auth';
import { announcement, blockEnd } from './callState';
import { loadSaved } from './liveCall';
import { LivePanel } from './LivePanel';
import { useLive } from './store';
import { liveStrings as t } from './strings';
import { useLiveCall } from './useLiveCall';
import { useLiveStatus } from './useLiveStatus';
import './live.css';

/**
 * The voice assistant's entry: a button, and the panel it opens. It is mounted in the top bar and
 * in the headers of the chat and terminal pages, one at a time as the route changes, and every one
 * shows the same call. It renders nothing unless the gateway answers `/api/live/status` and offers
 * the assistant to the account, so a gateway without the feature shows no trace of it. An account
 * that is offered it but cannot have a call now is told why in the panel, in place of Start. While
 * a call is on, the button says so even with the panel closed, and a draft the assistant is
 * waiting on a yes for brings the panel up: the person decides on what they can see. So does a
 * call the account itself ended, once, with the gateway asked how things stand.
 */
export function LiveEntry() {
  const username = useAuth((s) => s.username) ?? '';
  // A page that comes up with a draft waiting for a yes shows it, whatever the page before did with it.
  const [open, setOpen] = useState(() => useLive.getState().view.draft !== null);
  const { status, refresh } = useLiveStatus(username, open);
  const enabled = status?.enabled === true;
  // What the panel was last brought up for, anywhere in the app: a new page starts from it.
  const [announced, setAnnounced] = useState(() => useLive.getState().announced);
  const anchor = useRef<HTMLButtonElement>(null);
  const call = useLiveCall();

  const waiting = announcement(call.view);
  const ended = blockEnd(call.view);
  if (waiting !== null && waiting !== announced) {
    // Shown once per draft or ended call: closing the panel on it does not bring it back, a new one does.
    setAnnounced(waiting);
    setOpen(true);
  }
  useEffect(() => {
    if (waiting === null) return;
    const first = useLive.getState().announced !== waiting;
    useLive.getState().announce(waiting);
    // The first sight of a call the account itself ended: the gateway is asked how things stand,
    // so that the panel says why in place of Start.
    if (first && waiting === ended) refresh();
  }, [waiting, ended, refresh]);

  useEffect(() => {
    if (enabled) loadSaved();
  }, [enabled, open, username]);

  if (!status?.enabled) return null;
  const { phase, speaker, muted } = call.view;
  const inCall = phase === 'live' || phase === 'connecting';
  const quiet = inCall && muted;
  const label = quiet ? t.openInCallMuted : inCall ? t.openInCall : t.open;

  return (
    <>
      <button
        type="button"
        ref={anchor}
        className="live-entry"
        data-active={inCall}
        data-muted={quiet}
        aria-label={label}
        aria-expanded={open}
        title={label}
        onClick={() => setOpen((value) => !value)}
      >
        {quiet ? <MicOff size={16} /> : <AudioLines size={16} />}
        {inCall ? <span className="live-dot" data-speaker={speaker} aria-hidden /> : null}
      </button>
      {open ? (
        <LivePanel call={call} status={status} anchor={anchor} onClose={() => setOpen(false)} />
      ) : null}
    </>
  );
}
