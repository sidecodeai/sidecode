import { Mic, MicOff, PhoneOff, X } from 'lucide-react';
import { useLayoutEffect, useRef, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { Button } from '../../components/Button';
import type { LiveStatus } from './api';
import { DraftCard } from './DraftCard';
import { HistoryPanel } from './HistoryPanel';
import { liveStrings as t } from './strings';
import { Note } from './Note';
import { TranscriptList } from './TranscriptList';
import type { LiveCall } from './useLiveCall';
import { statusText } from './words';
import './live.css';

/** The space between the bar the entry sits in and the panel under it. */
const GAP_PX = 8;

interface Props {
  call: LiveCall;
  /** What the gateway says of the assistant for this account: with no call on, a "not now" takes Start's place. */
  status: LiveStatus;
  /** The entry's button: the panel hangs under the bar it is in, whichever page the entry is on. */
  anchor: RefObject<HTMLElement | null>;
  onClose: () => void;
}

export function LivePanel({ call, status, anchor, onClose }: Props) {
  const { phase, speaker, secondsLeft, transcript, notice, muted } = call.view;
  const inCall = phase === 'live' || phase === 'ending' || phase === 'connecting';
  const panel = useRef<HTMLElement>(null);

  // A portal on the body, so no bar's own clipping or filter moves it: it is placed against the
  // viewport, under the bar the button sits in.
  useLayoutEffect(() => {
    const place = (): void => {
      const el = panel.current;
      const at = anchor.current;
      if (!el || !at) return;
      const bar = at.closest('header') ?? at;
      const top = Math.round(bar.getBoundingClientRect().bottom + GAP_PX);
      el.style.setProperty('--live-top', `${top}px`);
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [anchor]);

  return createPortal(
    <section className="live-panel" role="dialog" aria-label={t.title} ref={panel}>
      <header className="live-head">
        <div>
          <h2 className="live-title">{t.title}</h2>
          <span className="live-tag">{t.experimental}</span>
        </div>
        <button type="button" className="live-close" aria-label={t.close} onClick={onClose}>
          <X size={16} />
        </button>
      </header>

      {inCall ? (
        <div className="live-status" data-speaker={speaker} data-muted={muted} aria-live="polite">
          <span className="live-orb" aria-hidden />
          <span className="live-status-text">{statusText(call.view)}</span>
          {secondsLeft !== null && phase === 'live' ? (
            <span className="live-left">{t.timeLeft(Math.ceil(secondsLeft / 60))}</span>
          ) : null}
        </div>
      ) : (
        <Note view={call.view} status={status} onFollow={onClose} />
      )}

      {notice && inCall ? (
        <p className="live-notice" role="status">
          {t.notice}
        </p>
      ) : null}

      {inCall ? <TranscriptList entries={transcript} follow /> : null}

      <DraftCard call={call} />

      {inCall || status.available ? (
        <footer className="live-actions">
          {inCall ? (
            <>
              <Button onClick={() => call.setMuted(!muted)} disabled={phase !== 'live'}>
                {muted ? <MicOff size={16} /> : <Mic size={16} />}
                {muted ? t.unmute : t.mute}
              </Button>
              <Button
                variant="danger"
                className="live-hangup"
                onClick={call.hangUp}
                busy={phase === 'ending'}
              >
                <PhoneOff size={16} />
                {t.hangUp}
              </Button>
            </>
          ) : (
            <Button variant="primary" block onClick={() => void call.start()}>
              <Mic size={16} />
              {t.start}
            </Button>
          )}
        </footer>
      ) : null}

      {inCall || (!status.available && call.records.length === 0) ? null : (
        <HistoryPanel records={call.records} />
      )}
    </section>,
    document.body,
  );
}
