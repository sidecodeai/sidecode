import { AlertTriangle, CircleAlert, Clock, Info } from 'lucide-react';
import { strings } from '../../../strings';
import { limitEndText, resumeRowText } from '../resume';
import type {
  ErrorEvent,
  NoticeEvent,
  ResumeEvent,
  TurnCompletedEvent,
} from '../../../protocol/types';

export function NoticeRow({ event }: { event: NoticeEvent }) {
  const Icon = event.level === 'error' ? CircleAlert : event.level === 'warn' ? AlertTriangle : Info;
  return (
    <p className={`notice ${event.level}`}>
      <Icon size={13} aria-hidden />
      {event.text}
    </p>
  );
}

export function ErrorRow({ event }: { event: ErrorEvent }) {
  return (
    <p className="notice error" role="alert">
      <CircleAlert size={13} aria-hidden />
      {event.message}
      {event.code ? <span className="notice-code mono">{event.code}</span> : null}
    </p>
  );
}

export function TurnEndRow({ event }: { event: TurnCompletedEvent }) {
  // A35: a turn the vendor's usage limit ended is not a failure the reader can
  // act on, so it reads as a notice and says when the limit resets.
  if (event.limit) {
    return (
      <p className="notice">
        <Clock size={13} aria-hidden />
        {limitEndText(event.limit)}
      </p>
    );
  }
  const label =
    event.stop_reason === 'interrupted' ? strings.chat.turnInterrupted : strings.chat.turnFailed;
  return (
    <p className={`notice ${event.stop_reason === 'interrupted' ? 'info' : 'error'}`}>
      <AlertTriangle size={13} aria-hidden />
      {label}
    </p>
  );
}

/**
 * A35 (5.15): what the device did about the resume, in the notice voice. The
 * `fired` step draws nothing — the prompt itself appears in the person's
 * bubble, captioned, and the turn it starts reads as a remote turn does.
 */
export function ResumeRow({ event }: { event: ResumeEvent }) {
  const text = resumeRowText(event);
  if (text === null) return null;
  return (
    <p className="notice">
      <Clock size={13} aria-hidden />
      {text}
    </p>
  );
}
