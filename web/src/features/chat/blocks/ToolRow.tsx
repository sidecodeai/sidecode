import { useState, type ReactNode } from 'react';
import { useNow } from '../../../lib/useNow';
import { duration } from '../../../lib/format';
import { strings } from '../../../strings';
import type { ToolCallEvent } from '../../../protocol/types';
import { DiffStat, DiffView } from './DiffView';
import { OutputBox } from './OutputBox';
import { ToolIcon } from './toolIcons';
import { toolCategory } from './toolCategory';

interface Props {
  event: ToolCallEvent;
  onOpenFull: (blockId: string) => Promise<void>;
  children?: ReactNode;
}

export function ToolRow({ event, onOpenFull, children }: Props) {
  const [open, setOpen] = useState(false);
  const running = event.status === 'running';
  const now = useNow(running ? 1000 : 0);
  const failed = event.status === 'failed';
  const category = toolCategory(event.tool, event.tool_kind);
  const hasDetail =
    event.input !== undefined || event.output !== undefined || event.diff?.patch !== undefined;
  // A running tool shows just its live output; the full input and diff appear
  // only when the row is opened deliberately.
  const expanded = open || running;
  const showInput = open && event.input !== undefined;
  const showOutput = open ? event.output !== undefined : running && Boolean(event.output);

  return (
    <div className={`tool${running ? ' running' : ''}${failed ? ' failed' : ''}`}>
      <button
        type="button"
        className="tool-head"
        aria-expanded={expanded}
        disabled={!hasDetail}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="tool-marker" aria-hidden>
          {running ? <span className="dot running pulse" /> : <ToolIcon category={category} />}
        </span>
        <span className="tool-name">{event.tool}</span>
        {/* A27: a slash command's block is titled with the command itself, so
            the name and the title are one and the same word; printing it twice
            would only say it twice. */}
        {event.title === event.tool ? null : (
          <span className="tool-title mono">{event.title}</span>
        )}
        {event.diff ? <DiffStat diff={event.diff} /> : null}
        {event.summary ? (
          <span className={`badge${failed ? ' error' : ''}`}>{event.summary}</span>
        ) : null}
        <span className="tool-right">
          {running
            ? `${strings.chat.running} ${duration(Math.max(0, now - event.started_at))}`
            : event.duration_ms !== undefined
              ? duration(event.duration_ms)
              : hasDetail
                ? expanded
                  ? strings.common.collapse
                  : strings.common.expand
                : ''}
        </span>
      </button>

      {expanded ? (
        <div className="tool-body">
          {showInput ? (
            <div className="tool-section">
              <span className="tool-section-label">{strings.chat.input}</span>
              <OutputBox
                text={JSON.stringify(event.input, null, 2)}
                truncated={event.input_truncated ?? false}
                onOpenFull={() => onOpenFull(event.block_id)}
              />
            </div>
          ) : null}
          {open && event.diff?.patch ? <DiffView diff={event.diff} /> : null}
          {showOutput ? (
            <div className="tool-section">
              {showInput ? (
                <span className="tool-section-label">{strings.chat.output}</span>
              ) : null}
              <OutputBox
                text={event.output ?? ''}
                truncated={event.output_truncated ?? false}
                live={running}
                onOpenFull={() => onOpenFull(event.block_id)}
              />
            </div>
          ) : null}
          {children ? <div className="tool-children">{children}</div> : null}
        </div>
      ) : children ? (
        <div className="tool-children collapsed-children">{children}</div>
      ) : null}
    </div>
  );
}
