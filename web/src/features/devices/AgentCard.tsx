/**
 * A33 — one agent on a device page: its logo, its name and version, how it is
 * signed in, and the quota windows of every account.
 *
 * Nothing is drawn for what the agent does not report. An agent whose
 * `accounts` is absent came from a device that never looked, and says nothing
 * at all; an empty list is an agent installed and signed in nowhere. A key has
 * no meter, because a key has no plan window to measure.
 */
import { AgentLogo } from '../../components/AgentLogo';
import { agentLabel, strings } from '../../strings';
import { signInLine } from './accounts';
import { QuotaMeter } from './QuotaMeter';
import type { AgentAccount, AgentInfo } from '../../protocol/types';
import type { QuotaStatus } from './useDeviceQuota';

interface Props {
  agent: AgentInfo;
  /** The accounts to draw: the fresh reply's when it has arrived, else the stored ones. */
  accounts: AgentAccount[] | undefined;
  status: QuotaStatus;
  /** Why the request for fresh windows failed, when it did. */
  error: string | null;
}

export function AgentCard({ agent, accounts, status, error }: Props) {
  return (
    <li className="card device-page-agent">
      <div className="device-page-agent-head">
        <AgentLogo agent={agent.agent} />
        <span className="device-page-agent-name">{agentLabel(agent.agent)}</span>
        {agent.version ? (
          <span className="device-page-agent-version mono">{agent.version}</span>
        ) : null}
      </div>

      {accounts === undefined ? null : accounts.length === 0 ? (
        <p className="device-page-signin">{strings.devicePage.notSignedIn}</p>
      ) : (
        <ul className="device-page-accounts">
          {accounts.map((account, index) => (
            <li key={`${account.provider}-${account.method}-${index}`}>
              <p className="device-page-signin">{signInLine(account)}</p>
              <Quota account={account} status={status} error={error} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/** The meters, or the one line that stands where they would have been. */
function Quota({ account, status, error }: { account: AgentAccount; status: QuotaStatus; error: string | null }) {
  if (account.method === 'api_key') return null;
  if (status === 'checking') return <p className="device-page-note">{strings.devicePage.checking}</p>;
  if (status === 'offline') {
    return <p className="device-page-note">{strings.devicePage.offlineQuota}</p>;
  }
  if (status === 'failed') return <p className="device-page-note">{error}</p>;
  if (account.limits_error) return <p className="device-page-note">{account.limits_error}</p>;
  if (!account.limits || account.limits.length === 0) return null;
  return (
    <ul className="device-page-meters">
      {account.limits.map((limit, index) => (
        <QuotaMeter key={`${limit.window_minutes}-${limit.scope ?? ''}-${index}`} limit={limit} />
      ))}
    </ul>
  );
}
