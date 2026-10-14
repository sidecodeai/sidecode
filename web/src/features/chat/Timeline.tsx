import { memo, useCallback, useMemo, type ReactNode } from 'react';
import { ArrowDown } from 'lucide-react';
import { strings } from '../../strings';
import { useSettings } from '../../stores/settings';
import { selectView, type TimelineItem, type TimelineState } from '../../stores/timeline';
import type {
  ApprovalEvent,
  AssistantTextEvent,
  ErrorEvent,
  NoticeEvent,
  QuestionAnswers,
  QuestionEvent,
  ResumeEvent,
  ThinkingEvent,
  ToolCallEvent,
  TurnCompletedEvent,
  UserMessageEvent,
} from '../../protocol/types';
import { MarkdownText } from './MarkdownText';
import { ApprovalCard } from './blocks/ApprovalCard';
import { ErrorRow, NoticeRow, ResumeRow, TurnEndRow } from './blocks/NoticeRow';
import { QuestionCard } from './blocks/QuestionCard';
import { ThinkingRow } from './blocks/ThinkingRow';
import { ToolRow } from './blocks/ToolRow';
import { AgentMessageRow } from './blocks/AgentMessageRow';
import { UserMessageRow } from './blocks/UserMessageRow';
import { useScrollFollow } from './useScrollFollow';

export interface TimelineHandlers {
  onOpenFull: (blockId: string) => Promise<void>;
  onApprove: (requestId: string, optionId: string) => Promise<void>;
  onAnswer: (requestId: string, answers: QuestionAnswers) => Promise<void>;
  onLoadOlder: () => void;
}

interface Props extends TimelineHandlers {
  timeline: TimelineState;
  historyLoading: boolean;
  historyHasMore: boolean;
  footer?: ReactNode;
}

export function Timeline({
  timeline,
  historyLoading,
  historyHasMore,
  footer,
  onOpenFull,
  onApprove,
  onAnswer,
  onLoadOlder,
}: Props) {
  const detail = useSettings((s) => s.timelineDetail);
  const view = useMemo(() => selectView(timeline, detail), [timeline, detail]);

  const onReachTop = useCallback(() => {
    if (historyHasMore && !historyLoading) onLoadOlder();
  }, [historyHasMore, historyLoading, onLoadOlder]);

  const { ref, following, missed, scrollToBottom, onScroll } = useScrollFollow({
    // A pending send carries no `seq`, so count it into the revision as well.
    revision: `${timeline.lastSeq}.${timeline.optimistic.length}`,
    redraw: detail,
    // The rows this level draws, so a burst of tool calls the reader has chosen
    // not to see is nothing to come back to.
    firstKey: view.roots[0]?.key ?? null,
    lastKey: view.roots.at(-1)?.key ?? null,
    onReachTop,
  });

  // Stable so memoised rows do not re-render on every streaming delta.
  const handlers = useMemo<TimelineHandlers>(
    () => ({ onOpenFull, onApprove, onAnswer, onLoadOlder }),
    [onOpenFull, onApprove, onAnswer, onLoadOlder],
  );

  return (
    <div className="timeline-wrap">
      <div className="timeline scroll-thin" ref={ref} onScroll={onScroll}>
        <div className="timeline-inner">
          {historyLoading ? (
            <p className="timeline-note">{strings.chat.loadingHistory}</p>
          ) : !historyHasMore && timeline.order.length > 0 ? (
            <p className="timeline-note">{strings.chat.historyStart}</p>
          ) : null}

          {timeline.order.length === 0 && !historyLoading ? (
            <p className="timeline-empty hint">{strings.chat.emptyTimeline}</p>
          ) : null}

          {view.roots.map((item) => (
            <ItemView
              key={item.key}
              item={item}
              handlers={handlers}
              nested={view.children[item.key]}
            />
          ))}
          {footer}
        </div>
      </div>

      {following ? null : <BackToLatest missed={missed} onClick={() => scrollToBottom('smooth')} />}
    </div>
  );
}

/**
 * The way back down, centred at the foot of the timeline above the composer. It
 * is on screen whenever the reader is away from the tail — paging up through
 * history needs a way back too — and widens into a capsule carrying what arrived
 * while they were away. The words live in the accessible name, as on the phone.
 */
function BackToLatest({ missed, onClick }: { missed: number; onClick: () => void }) {
  const count = missed > 0 ? strings.chat.newUpdates(missed) : null;
  return (
    <button
      type="button"
      className={count ? 'back-to-latest counted' : 'back-to-latest'}
      title={strings.chat.backToLatest}
      aria-label={count ? strings.chat.backToLatestCount(count) : strings.chat.backToLatest}
      onClick={onClick}
    >
      <ArrowDown size={16} aria-hidden />
      {count ? <span>{count}</span> : null}
    </button>
  );
}

interface ItemProps {
  item: TimelineItem;
  handlers: TimelineHandlers;
  nested?: TimelineItem[];
}

/**
 * Rows are memoised on the item identity the reducer preserves for untouched
 * blocks, so a delta re-renders only the block it belongs to.
 */
const ItemView = memo(function ItemView({ item, handlers, nested }: ItemProps) {
  const event = item.event;
  switch (event.kind) {
    case 'user_message': {
      const message = event as UserMessageEvent;
      // A34: nobody typed these words, so they are drawn with the agent's own
      // output rather than in the person's bubble.
      if (message.source === 'agent') return <AgentMessageRow event={message} />;
      return <UserMessageRow event={message} pending={item.pending} />;
    }
    case 'assistant_text': {
      const text = (event as AssistantTextEvent).text ?? '';
      return text ? <MarkdownText text={text} /> : null;
    }
    case 'thinking':
      return <ThinkingRow event={event as ThinkingEvent} />;
    case 'tool_call':
      return (
        <ToolRow event={event as ToolCallEvent} onOpenFull={handlers.onOpenFull}>
          {nested?.map((child) => (
            <ItemView key={child.key} item={child} handlers={handlers} />
          ))}
        </ToolRow>
      );
    case 'approval':
      return <ApprovalCard event={event as ApprovalEvent} onDecide={handlers.onApprove} />;
    case 'question':
      return <QuestionCard event={event as QuestionEvent} onAnswer={handlers.onAnswer} />;
    case 'notice':
      return <NoticeRow event={event as NoticeEvent} />;
    case 'error':
      return <ErrorRow event={event as ErrorEvent} />;
    case 'turn_completed':
      return <TurnEndRow event={event as TurnCompletedEvent} />;
    case 'resume':
      return <ResumeRow event={event as ResumeEvent} />;
    default:
      return null;
  }
});
