import { useState } from 'react';
import { Button } from '../../components/Button';
import { duration } from '../../lib/format';
import { strings } from '../../strings';
import { endReason } from './callState';
import type { CallRecord } from './history';
import { clearSaved } from './liveCall';
import { liveStrings as t } from './strings';
import { TranscriptList } from './TranscriptList';
import { endedText } from './words';

function when(timestamp: number): string {
  const date = new Date(timestamp);
  // `format` throws on a date that is not one, and nothing above this would catch it.
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(strings.format.dateLocale, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
}

const label = (record: CallRecord): string =>
  [when(record.startedAt), duration(record.endedAt - record.startedAt)].filter(Boolean).join(' · ');

/** A call that ended in the ordinary way needs no line; one that was cut short or ran out does. */
function endNote(record: CallRecord): string | null {
  const reason = endReason(record.reason);
  return reason === 'other' ? null : endedText(reason);
}

function SavedCall({ record, latest }: { record: CallRecord; latest: boolean }) {
  const note = endNote(record);
  return (
    <section className="live-record">
      <h3 className="live-section">{latest ? t.lastConversation : t.earlierConversation}</h3>
      <p className="live-record-when">{label(record)}</p>
      <TranscriptList entries={record.entries} className="live-record-transcript" />
      {note ? <p className="live-record-note">{note}</p> : null}
    </section>
  );
}

interface ListProps {
  records: CallRecord[];
  shown: CallRecord | undefined;
  onOpen: (id: string) => void;
}

function CallList({ records, shown, onOpen }: ListProps) {
  return (
    <section>
      <h3 className="live-section">{t.conversations}</h3>
      <ul className="live-calls">
        {records.map((record) => (
          <li key={record.id}>
            <button
              type="button"
              className="live-call"
              aria-current={record === shown}
              onClick={() => onOpen(record.id)}
            >
              {label(record)}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ClearHistory() {
  const [asking, setAsking] = useState(false);
  if (!asking) {
    return (
      <button type="button" className="live-clear" onClick={() => setAsking(true)}>
        {t.clearHistory}
      </button>
    );
  }
  return (
    <div className="live-clear-ask" role="group" aria-label={t.clearHistory}>
      <span>{t.clearAsk}</span>
      <Button small onClick={() => setAsking(false)}>
        {t.cancel}
      </Button>
      <Button small variant="danger" onClick={clearSaved}>
        {t.clearConfirm}
      </Button>
    </div>
  );
}

/**
 * With no call on: the latest conversation, the list of the calls kept to open another from, and
 * what clearing does. The conversations are this browser's, and the line says so.
 */
export function HistoryPanel({ records }: { records: CallRecord[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const shown = records.find((record) => record.id === openId) ?? records[0];

  return (
    <div className="live-history">
      {shown ? <SavedCall record={shown} latest={shown === records[0]} /> : null}
      {records.length > 1 ? <CallList records={records} shown={shown} onOpen={setOpenId} /> : null}
      <footer className="live-history-foot">
        <p className="live-kept">{t.keptHere}</p>
        {records.length > 0 ? <ClearHistory /> : null}
      </footer>
    </div>
  );
}
