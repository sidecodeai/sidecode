import { useState, type ReactNode } from 'react';
import { useNow } from '../../../lib/useNow';
import { duration } from '../../../lib/format';
import { strings } from '../../../strings';
import type { ToolCallEvent } from '../../../protocol/types';
import { DiffStat, DiffView } from './DiffView';
import { OutputBox } from './OutputBox';
import { ToolIcon } from './toolIcons';
import { toolCategory } from './toolCategory';
import { isCut, lacksContent } from './toolCut';

interface Props {
  event: ToolCallEvent;
  /** A60: the block is held as `session.block` returned it, so nothing more is fetched. */
  full: boolean;
  onOpenFull: (blockId: string) => Promise<void>;
  children?: ReactNode;
}

export function ToolRow({ event, full, onOpenFull, children }: Props) {
  const [open, setOpen] = useState(false);
  const [fetching, setFetching] = useState(false);
  const running = event.status === 'running';
  const now = useNow(running ? 1000 : 0);
  const failed = event.status === 'failed';
  const category = toolCategory(event.tool, event.tool_kind);
  const cut = isCut(event);
  // A60: a brief page leaves the output and the patch out, so a cut is detail
  // to open too, even with nothing of it held yet.
  const hasDetail =
    event.input !== undefined || event.output !== undefined || event.diff?.patch !== undefined || cut;
  // A running tool shows just its live output; the full input and diff appear
  // only when the row is opened deliberately.
  const expanded = open || running;
  const showInput = open && event.input !== undefined;
  const showOutput = open ? event.output !== undefined : running && Boolean(event.output);

  const fetchFull = () => {
    setFetching(true);
    // `onOpenFull` reports its own failure, and the row offers the fetch again.
    void onOpenFull(event.block_id).finally(() => setFetching(false));
  };

  // A60: a finished block the device cut is fetched whole the moment the
  // person opens it. A running one is still being written, and its next event
  // replaces whatever a fetch would bring.
  const toggle = () => {
    const opening = !open;
    setOpen(opening);
    if (opening && cut && !full && !running && !fetching) fetchFull();
  };

  return (
    <div className={`tool${running ? ' running' : ''}${failed ? ' failed' : ''}`}>
      <button
        type="button"
        className="tool-head"
        aria-expanded={expanded}
        disabled={!hasDetail}
        onClick={toggle}
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
          {open && !full && lacksContent(event) ? (
            <p className="tool-note">
              {fetching ? (
                strings.common.loading
              ) : (
                <button type="button" className="link-btn" onClick={fetchFull}>
                  {strings.chat.openFullOutput}
                </button>
              )}
            </p>
          ) : null}
          {children ? <div className="tool-children">{children}</div> : null}
        </div>
      ) : children ? (
        <div className="tool-children collapsed-children">{children}</div>
      ) : null}
    </div>
  );
}
