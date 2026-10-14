import { ArrowLeft, CheckSquare, Square } from 'lucide-react';
import { Link } from 'react-router';
import { Popover } from '../../components/Popover';
import { compactNumber, duration, tildePath } from '../../lib/format';
import { sessionTitle, strings } from '../../strings';
import { useSettings } from '../../stores/settings';
import { useNow } from '../../lib/useNow';
import { canInterruptShared } from './attach';
import type { AgentInfo, Session, TodoItem } from '../../protocol/types';

interface Props {
  session: Session;
  agent: AgentInfo | null;
  deviceName: string;
  todos: TodoItem[];
  stopping: boolean;
  onStop: () => void;
}

export function ChatHeader({ session, agent, deviceName, todos, stopping, onStop }: Props) {
  // A7: a terminal-driven turn also reports `running`, but only the terminal
  // can stop it — the user has to take over first. A10: an attached session can
  // be stopped only when the device says the attachment carries an interrupt.
  const stoppable =
    session.control === 'shared' ? canInterruptShared(agent) : session.control !== 'terminal';
  const running = (session.state === 'running' || session.state === 'starting') && stoppable;
  // A checklist is part of the agent's workings: Simple does not draw it.
  const showsTodos = useSettings((s) => s.timelineDetail) === 'detailed';
  const now = useNow(session.turn ? 1000 : 0);
  const doneCount = todos.filter((t) => t.status === 'completed').length;
  const total = session.todos?.total ?? todos.length;
  const done = todos.length > 0 ? doneCount : (session.todos?.done ?? 0);
  const tokens = session.usage?.total_tokens ?? 0;
  const elapsed = session.turn ? duration(now - session.turn.started_at) : null;

  return (
    <header className="chat-header">
      <Link to="/sessions" className="icon-btn chat-back" aria-label={strings.nav.backToSessions}>
        <ArrowLeft size={17} />
      </Link>
      <div className="chat-heading">
        <h1>{sessionTitle(session)}</h1>
        <p className="mono chat-sub">
          {deviceName}:{tildePath(session.cwd)}
          {session.git ? ` · ${session.git.branch}` : ''}
        </p>
      </div>

      <div className="chat-header-actions">
        {showsTodos && total > 0 ? (
          <Popover
            align="end"
            ariaLabel={strings.chat.todosTitle}
            label={
              <>
                <CheckSquare size={13} aria-hidden />
                {strings.chat.todos(done, total)}
              </>
            }
          >
            {() => (
              <ul className="todo-list">
                {todos.map((todo) => (
                  <li key={todo.id} className={todo.status}>
                    {todo.status === 'completed' ? <CheckSquare size={13} /> : <Square size={13} />}
                    <span>{todo.text}</span>
                  </li>
                ))}
              </ul>
            )}
          </Popover>
        ) : null}

        {tokens > 0 || elapsed ? (
          <span className="pill quiet usage-chip">
            {tokens > 0 ? compactNumber(tokens) : null}
            {tokens > 0 && elapsed ? ' · ' : null}
            {elapsed}
          </span>
        ) : null}

        {running ? (
          <button type="button" className="btn small" onClick={onStop} disabled={stopping}>
            {stopping ? strings.chat.stopping : strings.chat.stop}
          </button>
        ) : null}
      </div>
    </header>
  );
}
