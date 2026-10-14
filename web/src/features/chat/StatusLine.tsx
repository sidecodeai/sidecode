import { agentLabel, strings } from '../../strings';
import type { AgentInfo, Session } from '../../protocol/types';

interface Props {
  session: Session;
  agent: AgentInfo | null;
  deviceOnline: boolean;
  /**
   * A43: the composer holds a queued message being edited, which goes back
   * into the line even behind a steering agent — so the line says queued, as
   * the button beside the field does.
   */
  editingQueued?: boolean;
  onTakeover: () => void;
}

/** The single line under the timeline that explains what happens next. */
export function StatusLine({
  session,
  agent,
  deviceOnline,
  editingQueued = false,
  onTakeover,
}: Props) {
  const name = agentLabel(session.agent);
  const steers = (agent?.capabilities.includes('steer') ?? false) && !editingQueued;
  const canTakeover = agent?.capabilities.includes('takeover') ?? false;

  if (!deviceOnline) return <Line tone="muted" text={strings.status.offline} />;

  if (session.control === 'terminal') {
    const busy = session.state === 'running' || session.state === 'starting';
    // `docs/DESIGN.md` § "The composer": this line says who holds the session,
    // what is happening there, and how to write to it — that last clause only
    // where the agent can be taken over, which is also where the button is.
    const clauses = [
      strings.status.terminalControlled,
      busy ? strings.status.terminalBusy : null,
      canTakeover ? strings.status.takeOverToSend : null,
    ];
    return (
      <Line tone={busy ? 'running' : 'muted'} text={clauses.filter((c) => c !== null).join(' · ')}>
        {canTakeover ? (
          <button type="button" className="link-btn" onClick={onTakeover}>
            {strings.chat.takeOver}
          </button>
        ) : null}
      </Line>
    );
  }

  switch (session.state) {
    case 'running':
      return (
        <Line
          tone="running"
          text={steers ? strings.status.workingSteer(name) : strings.status.workingQueued(name)}
        />
      );
    case 'starting':
      return <Line tone="running" text={strings.status.starting} />;
    case 'needs_approval':
      return <Line tone="attention" text={strings.status.needsApproval} />;
    case 'needs_input':
      return <Line tone="attention" text={strings.status.needsInput} />;
    case 'error':
      return <Line tone="error" text={session.state_detail ?? strings.status.errored} />;
    case 'stopped':
      return <Line tone="muted" text={strings.status.stopped} />;
    default:
      return null;
  }
}

function Line({
  tone,
  text,
  children,
}: {
  tone: 'running' | 'attention' | 'error' | 'muted';
  text: string;
  children?: React.ReactNode;
}) {
  return (
    <p className={`status-line ${tone}`} aria-live="polite">
      <span className={`dot ${tone === 'muted' ? '' : tone}${tone === 'running' ? ' pulse' : ''}`} aria-hidden />
      {text}
      {children}
    </p>
  );
}
