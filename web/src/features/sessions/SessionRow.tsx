import { useId } from 'react';
import { Folder } from 'lucide-react';
import { AgentLogo } from '../../components/AgentLogo';
import { StatusDot } from '../../components/StatusDot';
import { UnseenDot } from '../../components/UnseenDot';
import { relativeTime, tildePath } from '../../lib/format';
import { agentLabel, sessionOriginLabel, sessionTitle, strings } from '../../strings';
import { keyOf, showsUnseenDot } from '../../stores/sessions';
import { DictationRowLabel } from '../voice/DictationRowLabel';
import type { Session } from '../../protocol/types';
import { SessionCloseButton } from './SessionCloseButton';
import { useSessionMenu } from './useSessionMenu';

interface Props {
  session: Session;
  online: boolean;
  onOpen: () => void;
  /**
   * A55: present only where the device can rename this session. The row then
   * has a context menu with Rename, which opens the dialog the page owns, so
   * the dialog outlives the row if a status change moves it.
   */
  onRename?: (session: Session) => void;
}

export function SessionRow({ session, online, onOpen, onRename }: Props) {
  // A39: only a session the device drives can be closed. A terminal holds its
  // own row until it exits, and a row in the Archive comes back by being
  // written to, so neither offers anything.
  const offersClose = session.control === 'remote' && !session.archived;
  // Where the session came from, whatever it is doing: the state is the dot's
  // colour alone (docs/DESIGN.md § "The session row says where it came from").
  // A hand-archived row says so before its origin.
  const origin = sessionOriginLabel(session);
  const word = session.archived ? `${strings.sessions.archived} · ${origin}` : origin;
  const unseenId = useId();
  const unseen = showsUnseenDot(session);
  const { triggerProps, menu } = useSessionMenu(session, onRename);

  return (
    <li className="session-row" {...triggerProps}>
      {/* Three lines (docs/DESIGN.md § "The session row"): title and time,
          agent and origin, then the working directory alone after a folder,
          so the path has the whole width and no line says three things. */}
      <button
        type="button"
        className="session-open"
        onClick={onOpen}
        aria-label={strings.sessions.open}
        aria-describedby={unseen ? unseenId : undefined}
      >
        <span className="session-line">
          {unseen ? <UnseenDot id={unseenId} /> : null}
          <span className="session-title">{sessionTitle(session)}</span>
          <span className="session-time">{relativeTime(session.updated_at)}</span>
        </span>
        <span className="session-line">
          <span className="agent-chip">
            <AgentLogo agent={session.agent} />
            {agentLabel(session.agent)}
          </span>
          <span className="session-state">
            <StatusDot state={session.state} control={session.control} online={online} />
            {word}
          </span>
          <DictationRowLabel sessionKey={keyOf(session)} />
        </span>
        <span className="session-cwd">
          <Folder className="session-folder" strokeWidth={1.5} aria-hidden />
          {/* Right-to-left on the outside and an isolated left-to-right run
              inside: the text reads as written and the ellipsis lands at the
              head, so the folder the path ends in is what survives. */}
          <span className="session-path mono">
            <bdi>{tildePath(session.cwd)}</bdi>
          </span>
        </span>
      </button>
      {offersClose ? <SessionCloseButton session={session} online={online} /> : null}
      {menu}
    </li>
  );
}
