import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { duration } from '../../../lib/format';
import { strings } from '../../../strings';
import type { ThinkingEvent } from '../../../protocol/types';

export function ThinkingRow({ event }: { event: ThinkingEvent }) {
  const [open, setOpen] = useState(false);
  const text = event.text ?? '';
  const label = event.done
    ? strings.chat.thoughtFor(duration(event.duration_ms ?? 0) || '—')
    : strings.chat.thinking;

  return (
    <div className="thinking">
      <button
        type="button"
        className="thinking-head"
        aria-expanded={open}
        disabled={text.length === 0}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronRight size={12} aria-hidden className={open ? 'rot' : ''} />
        <span>{label}</span>
      </button>
      {open && text ? <div className="thinking-body">{text}</div> : null}
    </div>
  );
}
