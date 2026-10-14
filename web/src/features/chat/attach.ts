/**
 * Amendment A10/A11 helpers: how a session shared with a live terminal behaves.
 *
 * `shared` means a CLI process owns the session and the device is attached to
 * it, so the composer, approvals and the queue work exactly as for `remote`.
 * What else the attachment carries is per agent: the device reports
 * `shared_interrupt`, `shared_settings` and `shared_attachments`, each
 * defaulting to false. The Codex app-server daemon and pi's extension carry
 * all three; Grok Build's leader carries the interrupt and the settings but
 * takes no images (A28); the Claude channel takes no images, and carries the
 * interrupt (A42) and the settings only as far as the device can type them
 * into the pseudo-terminal it owns — `shared_settings_keys` says which (A40).
 */
import { strings } from '../../strings';
import type { AgentInfo, Session, SharedSettingKey } from '../../protocol/types';

/** True when the device is attached to a terminal-owned session. */
export const isShared = (session: Session): boolean => session.control === 'shared';

/** True when only the terminal can drive the session. */
export const isTerminalOnly = (session: Session): boolean => session.control === 'terminal';

/**
 * A10 §4.4: Stop needs the `interrupt` capability *and* a device that reports
 * `shared_interrupt`. The Codex daemon calls it; an attached Claude terminal
 * is typed an Escape (A42), and a device with no shim reports neither.
 */
export function canInterruptShared(agent: AgentInfo | null): boolean {
  return agent?.shared_interrupt === true && agent.capabilities.includes('interrupt');
}

/**
 * A11/A40 §4.2: one setting's picker on a shared session. The device must
 * forward `session.set` to the CLI at all, and the setting must be one it
 * forwards: `shared_settings_keys` names the subset, and its absence means all
 * four. Claude's pseudo-terminal takes a typed `/model` and `/effort` but has
 * nothing to type for the permission mode, so that one stays the terminal's.
 */
export function canSetShared(agent: AgentInfo | null, key: SharedSettingKey): boolean {
  if (agent?.shared_settings !== true) return false;
  const keys = agent.shared_settings_keys;
  return keys === undefined || keys.includes(key);
}

/**
 * A11 §4.2: whether attachments reach the CLI through the attachment. When
 * they cannot, the attachment button is not rendered at all.
 */
export function canAttachShared(agent: AgentInfo | null): boolean {
  return agent?.shared_attachments === true;
}

/**
 * The one-line hint under "Controlled by the terminal", for a `terminal`
 * session whose agent could have been attached. `attach_ready === false` means
 * the device is not set up yet; `true` means this CLI was started without it.
 */
export function attachHint(agent: AgentInfo | null): string | null {
  if (!agent?.attach) return null;
  if (agent.attach_ready === true) return strings.chat.attachHintRestart;
  if (agent.attach_ready === false) {
    if (agent.attach === 'daemon') return strings.chat.attachHintDaemon;
    // A26: pi's attachment is an extension the device installs into pi itself.
    if (agent.attach === 'extension') return strings.chat.attachHintExtension;
    // A28: Grok Build joins a leader only when the person's own config says so,
    // so the hint asks for the setting and for the CLI to be started again.
    if (agent.attach === 'leader') return strings.chat.attachHintLeader;
    return strings.chat.attachHintChannel;
  }
  return null;
}

/**
 * The chip on a user bubble that says where the message got to. A19 leaves one
 * case: a message the CLI read as mid-turn data, which the device will send
 * again. A message the device is still holding is a queue entry, not a bubble.
 */
export function deliveryLabel(delivery: string | undefined): string | null {
  if (delivery === 'absorbed') return strings.chat.deliveryAbsorbed;
  return null;
}
