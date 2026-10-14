import { AgentLogo } from '../../components/AgentLogo';
import { agentLabel } from '../../strings';

/**
 * One agent in a list that has room for its name: the logo, then the name.
 * `docs/DESIGN.md` § "Agents" — the logo is what the agent filter and the
 * new-session form show, and the name follows it wherever a line is wide
 * enough to carry both.
 */
export function AgentOption({ agent }: { agent: string }) {
  return (
    <span className="agent-option">
      <AgentLogo agent={agent} />
      {agentLabel(agent)}
    </span>
  );
}
