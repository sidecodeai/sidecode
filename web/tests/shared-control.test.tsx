/**
 * Amendments A10, A11, A19, A20, A40, A42 and A51 — `control: "shared"`, a
 * terminal session the device is attached to. Covers the composer state, the
 * Stop and takeover rules, the delivery chip, the `shared_*` agent booleans, a
 * held message that is a queue entry rather than a block, a question answered
 * from here, a message that steers the terminal's turn, and the fixtures the
 * contract describes.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { ChatHeader } from '../src/features/chat/ChatHeader';
import { Composer } from '../src/features/chat/Composer';
import { StatusLine } from '../src/features/chat/StatusLine';
import { UserMessageRow } from '../src/features/chat/blocks/UserMessageRow';
import { ApprovalCard } from '../src/features/chat/blocks/ApprovalCard';
import { QuestionCard } from '../src/features/chat/blocks/QuestionCard';
import { composeAnswer } from '../src/features/chat/answering';
import {
  attachHint,
  canAttachShared,
  canInterruptShared,
  canSetShared,
  canSteer,
} from '../src/features/chat/attach';
import { addOptimistic, applyEvent, emptyTimeline, selectView } from '../src/stores/timeline';
import { foldChat, foldSession, type ChatSession } from '../src/stores/chat';
import { emptyDraft, useAnswers } from '../src/stores/answers';
import { refusalText } from '../src/lib/errors';
import { RequestError } from '../src/lib/ws';
import { sessionOriginLabel, strings } from '../src/strings';
import {
  claudeAgent,
  claudeNoShim,
  codexAgent,
  commandsFor,
  codexNoDaemon,
  grokAgent,
  grokNoLeader,
  piAgent,
  sessions,
} from '../mock/fixtures';
import { fixtureOrEmpty, fixturesAvailable } from './fixtures';
import type {
  AgentInfo,
  ApprovalEvent,
  MetaEvent,
  QuestionEvent,
  QuestionSpec,
  Session,
  SessionEvent,
  SharedSettingKey,
  UserMessageEvent,
} from '../src/protocol/types';

const CONTROLS = ['remote', 'terminal', 'shared', 'none'];
const DELIVERIES = ['delivered', 'absorbed'];
/** A40 §4.2: the four settings an attachment may carry one by one. */
const SETTING_KEYS: SharedSettingKey[] = ['model', 'permission_mode', 'effort', 'speed'];
/** A40 §6.3: what the device answers when it cannot type into the terminal. */
const BUSY = 'the terminal is busy; try again in a moment';

const sharedIdle = fixtureOrEmpty<Session>('objects/session.shared-idle.json');
const sharedRunning = fixtureOrEmpty<Session>('objects/session.shared-running.json');
const attachAgent = fixtureOrEmpty<AgentInfo>('objects/agent.claude-attach.json');
const deliveredMessage = fixtureOrEmpty<UserMessageEvent>('events/user_message.delivered.json');
const absorbedMessage = fixtureOrEmpty<UserMessageEvent>('events/user_message.absorbed.json');
const sharedApproval = fixtureOrEmpty<ApprovalEvent>('events/approval.shared-pending.json');
const terminalApproval = fixtureOrEmpty<ApprovalEvent>('events/approval.shared-terminal.json');
const daemonAgent = fixtureOrEmpty<AgentInfo>('objects/agent.codex-daemon.json');
const codexShared = fixtureOrEmpty<Session>('objects/session.codex-shared-running.json');
const codexApproval = fixtureOrEmpty<ApprovalEvent>('events/approval.codex-shared-pending.json');
const codexElsewhere = fixtureOrEmpty<ApprovalEvent>('events/approval.codex-elsewhere.json');
const pendingQuestion = fixtureOrEmpty<QuestionEvent>('events/question.pending.json');
const terminalQuestion = fixtureOrEmpty<QuestionEvent>('events/question.resolved.terminal.json');

/**
 * A51: the Claude channel as a device that says nothing about steering reports
 * it — every device before the amendment — and as one that hands a message to
 * the running turn. Both are spelled out, whatever the fixture carries.
 */
const { shared_steer: _unsaid, ...attachNoSteer } = attachAgent;
const attachSteer: AgentInfo = { ...attachAgent, shared_steer: true };

const composerProps = (session: Session, agent: AgentInfo | null) => ({
  session,
  agent,
  deviceOnline: true,
  queue: [],
  question: null,
  sttEnabled: false,
  onSend: vi.fn().mockResolvedValue(undefined),
  onAnswer: vi.fn().mockResolvedValue(undefined),
  onSetOption: vi.fn(),
  onRemoveQueued: vi.fn(),
  onTakeover: vi.fn(),
});

const terminalSession = (overrides: Partial<Session> = {}): Session => ({
  ...sharedIdle,
  control: 'terminal',
  state: 'readonly',
  ...overrides,
});

describe.runIf(fixturesAvailable())('A10 fixtures', () => {
  it('decodes the shared session objects', () => {
    for (const value of [sharedIdle, sharedRunning]) {
      expect(value.control).toBe('shared');
      expect(CONTROLS).toContain(value.control);
      // §4.5: `readonly` stays reserved for `control: "terminal"`.
      expect(value.state).not.toBe('readonly');
      expect(value.origin).toBe('terminal');
    }
    expect(sharedRunning.state).toBe('running');
    expect(sharedRunning.turn).not.toBeNull();
  });

  it('decodes the attach-capable agent', () => {
    const agent: AgentInfo = attachAgent;
    expect(agent.attach).toBe('channel');
    expect(agent.attach_ready).toBe(true);
    // A42: the shim's pseudo-terminal takes a typed Escape, so the channel
    // carries the interrupt after all.
    expect(agent.shared_interrupt).toBe(true);
    expect(agent.capabilities).toContain('takeover');
  });

  it('decodes the two delivery states A19 leaves', () => {
    expect(deliveredMessage.delivery).toBe('delivered');
    expect(absorbedMessage.delivery).toBe('absorbed');
    for (const value of [deliveredMessage, absorbedMessage]) {
      expect(DELIVERIES).toContain(value.delivery);
      // §5.2: `source` stays `remote` for anything an app sent.
      expect(value.source).toBe('remote');
    }
    // A19: the block appears when the CLI takes the message, so `first_seq`
    // places it after the output of the turn it waited for.
    expect(deliveredMessage.first_seq).toBeLessThan(deliveredMessage.seq);
  });

  it('decodes a relayed approval with allow and deny only', () => {
    for (const value of [sharedApproval, terminalApproval]) {
      expect(value.options.map((o) => o.id)).toEqual(['allow', 'deny']);
      expect(value.diff).toBeUndefined();
      expect(Object.keys(value.input ?? {}).sort()).toEqual([
        'description',
        'input_preview',
        'tool_name',
      ]);
    }
    expect(sharedApproval.status).toBe('pending');
    expect(terminalApproval.decision?.by).toBe('terminal');
  });

  it('rejects the values the contract calls out as invalid', () => {
    expect(CONTROLS).not.toContain('attached');
    expect(DELIVERIES).not.toContain('queued');
    // A19 took `pending` out of the protocol along with its fixture.
    expect(DELIVERIES).not.toContain('pending');
  });
});

describe.runIf(fixturesAvailable())('A10 composer on a shared session', () => {
  it('enables the composer exactly like a remote session', () => {
    render(<Composer {...composerProps(sharedIdle, attachAgent)} />);
    const input = screen.getByLabelText('Message the agent…');
    expect(input).toBeEnabled();
    expect(input).toHaveAttribute('placeholder', 'Message the agent…');
  });

  it('never offers Take over', () => {
    render(<Composer {...composerProps(sharedIdle, attachAgent)} />);
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('repeats nothing the header already says', () => {
    render(<Composer {...composerProps(sharedIdle, attachAgent)} />);
    expect(screen.queryByText(/Attached to the terminal/)).not.toBeInTheDocument();
    expect(document.querySelector('.takeover-bar')).toBeNull();
  });

  it('hides the model card and the permission-mode picker on an agent that carries neither', () => {
    // A11: the shape of a shared session before any of its settings are the
    // device's. A40 gives Claude two of them; this is the other case.
    const noSettings: AgentInfo = { ...attachAgent, shared_settings: false };
    delete noSettings.shared_settings_keys;
    render(<Composer {...composerProps(sharedIdle, noSettings)} />);
    for (const name of ['Model and effort', 'Permission mode', 'Model', 'Effort']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    // A17: what the terminal chose is still shown, as chips that open nothing.
    expect(document.querySelectorAll('.composer-chip.readonly')).toHaveLength(2);
  });

  it('shows the attachment button only while the device reports shared_attachments', () => {
    // A49: the channel carries text, so the device writes each file to disk
    // and the words it injects name it. An agent that does not say so takes none.
    const { unmount } = render(<Composer {...composerProps(sharedIdle, attachAgent)} />);
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled();
    unmount();

    const older: AgentInfo = { ...attachAgent, shared_attachments: false };
    render(<Composer {...composerProps(sharedIdle, older)} />);
    expect(screen.queryByRole('button', { name: 'Attach files' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Attach files')).not.toBeInTheDocument();
  });

  it('hides "Interrupt & send" while the attachment cannot interrupt', () => {
    const agent: AgentInfo = { ...attachAgent, shared_interrupt: false };
    render(<Composer {...composerProps(sharedRunning, agent)} />);
    expect(screen.queryByRole('button', { name: 'Send options' })).not.toBeInTheDocument();
  });

  it('offers "Interrupt & send" when the device reports shared_interrupt', () => {
    render(<Composer {...composerProps(sharedRunning, attachAgent)} />);
    expect(screen.getByRole('button', { name: 'Send options' })).toBeInTheDocument();
  });

  it('leaves the pickers alone on a remote session', () => {
    const session: Session = { ...sharedIdle, control: 'remote', origin: 'remote' };
    render(<Composer {...composerProps(session, claudeAgent)} />);
    expect(screen.getByRole('button', { name: 'Model and effort' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled();
  });
});

describe.runIf(fixturesAvailable())('A10 takeover bar on a terminal session', () => {
  it('offers Take over only when the agent has the capability', () => {
    const { unmount } = render(
      <Composer {...composerProps(terminalSession(), claudeAgent)} />,
    );
    expect(screen.getByRole('button', { name: 'Take over' })).toBeInTheDocument();
    unmount();

    render(<Composer {...composerProps(terminalSession({ agent: 'codex' }), codexAgent)} />);
    expect(screen.getByText('Controlled by the terminal')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('asks for the shim when the device is not ready to attach', () => {
    render(<Composer {...composerProps(terminalSession(), claudeNoShim)} />);
    expect(
      screen.getByText('Start claude through the SideCode shim to control it from here'),
    ).toBeInTheDocument();
  });

  it('asks for a restart when the device is ready but this CLI was not attached', () => {
    render(<Composer {...composerProps(terminalSession(), attachAgent)} />);
    expect(
      screen.getByText(
        'This terminal session was started without the attachment; restart it to control it from here',
      ),
    ).toBeInTheDocument();
  });

  it('picks the hint from the attachment kind and readiness', () => {
    expect(attachHint(null)).toBeNull();
    expect(attachHint(claudeAgent)).toContain('restart it');
    expect(attachHint(claudeNoShim)).toContain('SideCode shim');
    expect(attachHint(codexNoDaemon)).toContain('rc-client codex setup');
    // No `attach` and no readiness flag mean there is nothing to hint at.
    expect(attachHint({ ...codexAgent, attach: null })).toBeNull();
    expect(attachHint({ ...claudeAgent, attach_ready: undefined })).toBeNull();
  });

  it('shows no hint on a shared session, which is already attached', () => {
    render(<Composer {...composerProps(sharedIdle, attachAgent)} />);
    expect(screen.queryByText(/control it from here/)).not.toBeInTheDocument();
  });
});

describe.runIf(fixturesAvailable())('A10 Stop', () => {
  const header = (session: Session, agent: AgentInfo | null) =>
    render(
      <MemoryRouter>
        <ChatHeader
          session={session}
          agent={agent}
          deviceName="mac-studio-office"
          todos={[]}
          stopping={false}
          onStop={vi.fn()}
        />
      </MemoryRouter>,
    );

  it('is hidden on a shared session the attachment cannot interrupt', () => {
    header(sharedRunning, { ...attachAgent, shared_interrupt: false });
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it('appears once the device reports shared_interrupt', () => {
    header(sharedRunning, attachAgent);
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });

  it('needs the interrupt capability as well as the flag', () => {
    const agent: AgentInfo = {
      ...attachAgent,
      shared_interrupt: true,
      capabilities: attachAgent.capabilities.filter((c) => c !== 'interrupt'),
    };
    expect(canInterruptShared(agent)).toBe(false);
    header(sharedRunning, agent);
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it('still appears on a running remote session', () => {
    header({ ...sharedRunning, control: 'remote' }, claudeAgent);
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });
});

describe.runIf(fixturesAvailable())('A10 status line and list labels', () => {
  it('treats a shared session like a remote one, with no Take over', () => {
    render(
      <StatusLine
        session={sharedRunning}
        agent={attachNoSteer}
        deviceOnline
        onTakeover={vi.fn()}
      />,
    );
    expect(
      screen.getByText('Claude Code is working · your message will be queued'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('says the message will steer once the device reports shared_steer (A51)', () => {
    render(
      <StatusLine
        session={sharedRunning}
        agent={attachSteer}
        deviceOnline
        onTakeover={vi.fn()}
      />,
    );
    expect(
      screen.getByText('Claude Code is working · your message will steer the turn'),
    ).toBeInTheDocument();
  });

  it('still says queued on a session the device started, whatever the flag says (A51)', () => {
    render(
      <StatusLine
        session={{ ...sharedRunning, control: 'remote', origin: 'remote' }}
        agent={attachSteer}
        deviceOnline
        onTakeover={vi.fn()}
      />,
    );
    expect(
      screen.getByText('Claude Code is working · your message will be queued'),
    ).toBeInTheDocument();
  });

  it('labels a shared session by where it came from, like any other row', () => {
    // A shared session was started by a terminal, so its row reads "Terminal"
    // whether it is idle or running; only the dot tells them apart.
    expect(sessionOriginLabel(sharedIdle)).toBe('Terminal');
    expect(sessionOriginLabel(sharedRunning)).toBe('Terminal');
    expect(sessionOriginLabel(terminalSession())).toBe('Terminal');
    expect(sessionOriginLabel({ origin: 'remote' })).toBe('Remote launch');
  });
});

describe.runIf(fixturesAvailable())('A10 delivery chip', () => {
  const chip = (event: UserMessageEvent) => {
    render(<UserMessageRow event={event} />);
    return document.querySelector('.delivery-chip')?.textContent ?? null;
  };

  it('says an absorbed message will be re-sent', () => {
    expect(chip(absorbedMessage)).toBe('will be re-sent');
  });

  it('shows nothing once the message is delivered, or on a normal prompt', () => {
    const { unmount } = render(<UserMessageRow event={deliveredMessage} />);
    expect(document.querySelector('.delivery-chip')).toBeNull();
    unmount();
    const plain: UserMessageEvent = { ...deliveredMessage };
    delete plain.delivery;
    expect(chip(plain)).toBeNull();
  });

  it('clears the "will be re-sent" chip when the device re-injects', () => {
    const absorbed = absorbedMessage;
    let timeline = applyEvent(emptyTimeline(), absorbed);
    expect(
      (timeline.items[absorbed.block_id]?.event as UserMessageEvent).delivery,
    ).toBe('absorbed');

    // §5.2: the re-injection keeps the block id and reports `delivered`.
    timeline = applyEvent(timeline, {
      ...absorbed,
      seq: absorbed.seq + 4,
      first_seq: absorbed.seq,
      delivery: 'delivered',
    });
    const item = timeline.items[absorbed.block_id];
    expect((item?.event as UserMessageEvent).delivery).toBe('delivered');
    expect(timeline.order).toEqual([absorbed.block_id]);

    render(<UserMessageRow event={item?.event as UserMessageEvent} />);
    expect(document.querySelector('.delivery-chip')).toBeNull();
  });
});

describe.runIf(fixturesAvailable())('A10 control transitions', () => {
  // §6.5: a new owner arrives as `meta.control`; `status` follows only when the
  // state moves too, so `control` must never be inferred from a status event.
  const meta = (control: Session['control'], seq: number): MetaEvent => ({
    seq,
    ts: 1788946200000 + seq,
    kind: 'meta',
    control,
  });

  it('follows the attachment registering and dropping', () => {
    let session: Session = { ...sharedIdle, control: 'terminal', state: 'readonly' };

    session = foldSession(session, [meta('shared', 40)]);
    expect(session.control).toBe('shared');
    // The state did not move, so nothing else on the summary may change.
    expect(session.state).toBe('readonly');

    session = foldSession(session, [meta('terminal', 41)]);
    expect(session.control).toBe('terminal');
  });

  it('applies the state only when a status event carries one', () => {
    const session = foldSession({ ...sharedIdle, state: 'running' }, [
      meta('none', 42),
      { seq: 43, ts: 1788946200043, kind: 'status', state: 'stopped' },
    ]);
    expect(session.control).toBe('none');
    expect(session.state).toBe('stopped');
  });

  it('leaves the owner alone when meta carries no control', () => {
    const session = foldSession(sharedIdle, [
      { seq: 44, ts: 1788946200044, kind: 'meta', title: 'Renamed from the terminal' },
    ]);
    expect(session.control).toBe('shared');
    expect(session.title).toBe('Renamed from the terminal');
  });
});

describe.runIf(fixturesAvailable())('A11 fixtures', () => {
  it('decodes the Codex daemon agent', () => {
    const agent: AgentInfo = daemonAgent;
    expect(agent.agent).toBe('codex');
    expect(agent.attach).toBe('daemon');
    expect(agent.attach_ready).toBe(true);
    expect(agent.shared_interrupt).toBe(true);
    expect(agent.shared_settings).toBe(true);
    expect(agent.shared_attachments).toBe(true);
    // §4.2: the capability list is unchanged, and Codex still has no takeover.
    expect(agent.capabilities).toEqual(
      expect.arrayContaining(['worktree', 'interrupt', 'queue', 'steer', 'attachments', 'effort', 'history']),
    );
    expect(agent.capabilities).not.toContain('takeover');
  });

  it('gives the Claude channel agent attachments as files, and two of four settings', () => {
    // A40 replaced A11's flat "no settings" with a named subset; a channel
    // still carries no bytes, so A49 names the files the device wrote in it.
    expect(attachAgent.shared_settings).toBe(true);
    expect(attachAgent.shared_settings_keys).toEqual(['model', 'effort']);
    expect(attachAgent.shared_attachments).toBe(true);
  });

  it('decodes a shared Codex session', () => {
    expect(codexShared.agent).toBe('codex');
    expect(codexShared.origin).toBe('terminal');
    expect(codexShared.control).toBe('shared');
    expect(codexShared.state).toBe('running');
    expect(codexShared.turn).not.toBeNull();
  });

  it('decodes the four daemon decisions', () => {
    expect(codexApproval.options.map((o) => o.id)).toEqual([
      'allow',
      'allow_session',
      'allow_always',
      'deny',
    ]);
    expect(codexApproval.options.map((o) => o.style)).toEqual([
      'primary',
      'secondary',
      'secondary',
      'danger',
    ]);
    // §5.7: a command request carries the daemon's own command payload.
    expect(Object.keys(codexApproval.input ?? {}).sort()).toEqual([
      'command',
      'command_actions',
      'cwd',
    ]);
    expect(codexApproval.status).toBe('pending');
  });

  it('decodes a request answered in the terminal', () => {
    expect(codexElsewhere.status).toBe('resolved');
    expect(codexElsewhere.decision).toEqual({ option_id: 'elsewhere', by: 'terminal' });
    // The id matches none of the options on purpose.
    expect(codexElsewhere.options.map((o) => o.id)).not.toContain('elsewhere');
    expect(codexElsewhere.block_id).toBe(codexApproval.block_id);
    expect(codexElsewhere.first_seq).toBe(codexApproval.seq);
  });
});

describe.runIf(fixturesAvailable())('A11 composer on a shared Codex session', () => {
  it('reads the two booleans off the agent', () => {
    // The daemon names no subset, so every setting is the device's.
    for (const key of SETTING_KEYS) expect(canSetShared(daemonAgent, key)).toBe(true);
    expect(canAttachShared(daemonAgent)).toBe(true);
    // A49: the Claude channel reports it too.
    expect(canAttachShared(attachAgent)).toBe(true);
    // Both default to false when the device says nothing.
    expect(canSetShared(codexNoDaemon, 'model')).toBe(false);
    expect(canAttachShared({ ...claudeAgent, shared_attachments: undefined })).toBe(false);
    expect(canSetShared(null, 'model')).toBe(false);
  });

  it('shows the pickers when the device reports shared_settings', () => {
    render(<Composer {...composerProps(codexShared, daemonAgent)} />);
    for (const name of ['Model and effort', 'Permission mode']) {
      expect(screen.getByRole('button', { name })).toBeEnabled();
    }
  });

  it('shows attachments when the device reports shared_attachments', () => {
    render(<Composer {...composerProps(codexShared, daemonAgent)} />);
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled();
  });

  it('hides whichever half the device does not report', () => {
    const settingsOnly: AgentInfo = { ...daemonAgent, shared_attachments: false };
    const { unmount } = render(<Composer {...composerProps(codexShared, settingsOnly)} />);
    expect(screen.getByRole('button', { name: 'Model and effort' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Attach files' })).not.toBeInTheDocument();
    unmount();

    const attachmentsOnly: AgentInfo = { ...daemonAgent, shared_settings: false };
    render(<Composer {...composerProps(codexShared, attachmentsOnly)} />);
    expect(screen.queryByRole('button', { name: 'Model and effort' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled();
  });

  it('leaves every control live, and says nothing extra, on a shared Codex session', () => {
    render(<Composer {...composerProps(codexShared, daemonAgent)} />);
    expect(screen.queryByText(/Attached to the terminal/)).not.toBeInTheDocument();
    expect(document.querySelector('.takeover-bar')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('keeps Stop and "Interrupt & send" on the interrupt flag alone', () => {
    expect(canInterruptShared(daemonAgent)).toBe(true);
    const { unmount } = render(<Composer {...composerProps(codexShared, daemonAgent)} />);
    expect(screen.getByRole('button', { name: 'Send options' })).toBeInTheDocument();
    unmount();

    // Settings and attachments say nothing about interrupting.
    const noInterrupt: AgentInfo = { ...daemonAgent, shared_interrupt: false };
    render(<Composer {...composerProps(codexShared, noInterrupt)} />);
    expect(screen.queryByRole('button', { name: 'Send options' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Model and effort' })).toBeEnabled();
  });

  it('says the prompt will steer, matching the status line and the Send label', () => {
    render(<Composer {...composerProps(codexShared, daemonAgent)} />);
    const input = screen.getByLabelText('Message the agent…');
    expect(input).toHaveAttribute('placeholder', 'Message will steer the turn…');
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('still says queued for an agent that cannot steer', () => {
    render(<Composer {...composerProps(sharedRunning, attachNoSteer)} />);
    expect(screen.getByLabelText('Message the agent…')).toHaveAttribute(
      'placeholder',
      'Message will be queued…',
    );
    expect(screen.getByRole('button', { name: 'Queue' })).toBeInTheDocument();
  });

  it('steers a shared Claude turn with Send once the device reports shared_steer (A51)', async () => {
    const props = composerProps(sharedRunning, attachSteer);
    render(<Composer {...props} />);
    const input = screen.getByLabelText('Message the agent…');
    expect(input).toHaveAttribute('placeholder', 'Message will steer the turn…');
    const user = userEvent.setup();
    await user.click(input);
    await user.keyboard('also cover the reconnect');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    // The wire mode is the same `auto` as ever: the device decides it steers.
    await waitFor(() =>
      expect(props.onSend).toHaveBeenCalledWith('also cover the reconnect', [], 'auto'),
    );
  });

  it('keeps a session the device started queueing, whatever the flag says (A51)', () => {
    const session: Session = { ...sharedRunning, control: 'remote', origin: 'remote' };
    render(<Composer {...composerProps(session, attachSteer)} />);
    expect(screen.getByLabelText('Message the agent…')).toHaveAttribute(
      'placeholder',
      'Message will be queued…',
    );
    expect(screen.getByRole('button', { name: 'Queue' })).toBeInTheDocument();
  });

  it('hints for the daemon on a terminal Codex session the device cannot attach', () => {
    render(
      <Composer
        {...composerProps({ ...codexShared, control: 'terminal', state: 'readonly' }, codexNoDaemon)}
      />,
    );
    expect(
      screen.getByText('Run rc-client codex setup on the device to attach its Codex sessions'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });
});

describe.runIf(fixturesAvailable())('A11 approval cards', () => {
  const decide = vi.fn().mockResolvedValue(undefined);

  it('renders all four daemon decisions, ordered by style', () => {
    render(<ApprovalCard event={codexApproval} onDecide={decide} />);
    const labels = [...document.querySelectorAll('.approval-actions .btn')].map(
      (b) => b.textContent,
    );
    expect(labels).toEqual([
      'Allow',
      'Allow for this session',
      'Always allow commands like this',
      'Deny',
    ]);
    // The row wraps rather than overflowing at narrow widths.
    expect(document.querySelector('.approval-actions')).not.toBeNull();
  });

  it('sends back the option id the block offered', async () => {
    const onDecide = vi.fn().mockResolvedValue(undefined);
    render(<ApprovalCard event={codexApproval} onDecide={onDecide} />);
    await userEvent.click(screen.getByRole('button', { name: 'Always allow commands like this' }));
    expect(onDecide).toHaveBeenCalledWith(codexApproval.request_id, 'allow_always');
  });

  it('says a request answered in the terminal was answered there', () => {
    render(<ApprovalCard event={codexElsewhere} onDecide={decide} />);
    expect(screen.getByText('Answered in the terminal')).toBeInTheDocument();
    // The unknown option id must never leak into the label.
    expect(screen.queryByText(/elsewhere/)).not.toBeInTheDocument();
    expect(screen.queryByText(/decided by/)).not.toBeInTheDocument();
    expect(document.querySelector('.approval-actions')).toBeNull();
  });

  it('still names the option for a decision the app made', () => {
    render(<ApprovalCard event={terminalApproval} onDecide={decide} />);
    expect(screen.getByText('Allow · decided by terminal')).toBeInTheDocument();
  });
});

/**
 * A19 — a message sent into a running attached turn is a queue entry until the
 * CLI takes it. No bubble waits in the middle of the turn; the block arrives
 * where the terminal draws it.
 */
describe.runIf(fixturesAvailable())('A19 a held message is a queue entry', () => {
  const queueEvent = (seq: number, ids: string[]): SessionEvent => ({
    seq,
    ts: 1788946148000 + seq,
    kind: 'queue',
    pending: ids.map((id) => ({ id, text: 'and then lint', ts: 1788946148000 })),
  });

  it('leaves no bubble in the timeline and one entry in the queue', () => {
    const id = 'req-held-1';
    let chat: ChatSession = {
      key: 'dev/ses',
      deviceId: 'dev',
      sessionId: 'ses',
      timeline: addOptimistic(emptyTimeline(), {
        id,
        text: 'and then lint',
        attachments: [],
        at: 1788946148000,
      }),
      todos: [],
      queue: [],
      usage: null,
      ready: true,
      historyLoading: false,
      historyHasMore: false,
      error: null,
      closedSeq: null,
    };
    // `accepted: "queued"` retires the row; the device's snapshot names it too.
    chat = foldChat(chat, queueEvent(12, [id]));

    expect(chat.queue.map((q) => q.id)).toEqual([id]);
    expect(chat.timeline.optimistic).toEqual([]);
    expect(selectView(chat.timeline, 'detailed').roots).toEqual([]);
  });

  it('draws the bubble where the CLI took it, after the turn it waited for', () => {
    const id = 'req-held-1';
    let timeline = applyEvent(emptyTimeline(), {
      seq: 20,
      ts: 1788946148200,
      kind: 'assistant_text',
      block_id: 'a1',
      text: 'Done with the refactor.',
      done: true,
    });
    // A19: the device emits the block at injection, with `first_seq` after the
    // last block of the turn, and never a `pending` one before it.
    timeline = applyEvent(timeline, {
      seq: 22,
      ts: 1788946148400,
      kind: 'user_message',
      first_seq: 21,
      block_id: id,
      text: 'and then lint',
      source: 'remote',
      delivery: 'delivered',
    });

    expect(timeline.order).toEqual(['a1', id]);
    const message = timeline.items[id]?.event as UserMessageEvent;
    expect(message.delivery).toBe('delivered');
    render(<UserMessageRow event={message} />);
    expect(document.querySelector('.delivery-chip')).toBeNull();
  });
});

/**
 * A20 — a question is answered where you are. The card works on a shared
 * session, the composer becomes an Answer button, and a question the terminal
 * answered first says so.
 */
describe.runIf(fixturesAvailable())('A20 answering a question', () => {
  const sharedWaiting: Session = { ...sharedIdle, state: 'needs_input' };

  beforeEach(() => {
    useAnswers.setState({ drafts: {} });
  });

  it('decodes the question the terminal answered', () => {
    expect(terminalQuestion.status).toBe('resolved');
    expect(terminalQuestion.by).toBe('terminal');
    expect(terminalQuestion.answers?.['q1']).toEqual(['clamp']);
    // The same block, resolved: one row throughout.
    expect(terminalQuestion.block_id).toBe(pendingQuestion.block_id);
    expect(terminalQuestion.first_seq).toBe(pendingQuestion.seq);
    expect(pendingQuestion.by).toBeUndefined();
  });

  it('says a question answered in the terminal was answered there', () => {
    render(<QuestionCard event={terminalQuestion} onAnswer={vi.fn()} />);
    expect(screen.getByText('Answered in the terminal')).toBeInTheDocument();
    expect(document.querySelector('.approval-actions')).toBeNull();
  });

  it('says only "Answered" when the device names nobody', () => {
    const resolved: QuestionEvent = { ...terminalQuestion };
    delete resolved.by;
    render(<QuestionCard event={resolved} onAnswer={vi.fn()} />);
    expect(screen.getByText('Answered')).toBeInTheDocument();
  });

  // The card carries no `control` gate at all, and the composer beside it is
  // enabled on a shared session, so a question is answerable from both there.
  it('answers from the card itself', async () => {
    const onAnswer = vi.fn().mockResolvedValue(undefined);
    render(<QuestionCard event={pendingQuestion} onAnswer={onAnswer} />);
    await userEvent.click(screen.getByRole('button', { name: /Clamp skew/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));
    expect(onAnswer).toHaveBeenCalledWith(pendingQuestion.request_id, { q1: ['clamp'] });
  });

  it('reads Answer while the question is pending', () => {
    render(
      <Composer
        {...composerProps(sharedWaiting, attachAgent)}
        question={pendingQuestion}
      />,
    );
    expect(screen.getByRole('button', { name: 'Answer' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Queue' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Message the agent…')).toHaveAttribute(
      'placeholder',
      'Type your answer…',
    );
  });

  it('submits the draft as the free text of the first unanswered question', async () => {
    const props = composerProps(sharedWaiting, attachAgent);
    render(<Composer {...props} question={pendingQuestion} />);
    const field = screen.getByLabelText('Message the agent…');
    await userEvent.click(field);
    await userEvent.keyboard('clamp it, but log the skew');
    await userEvent.click(screen.getByRole('button', { name: 'Answer' }));

    expect(props.onAnswer).toHaveBeenCalledWith(pendingQuestion.request_id, {
      q1: 'clamp it, but log the skew',
    });
    expect(props.onSend).not.toHaveBeenCalled();
    expect(field).toHaveValue('');
  });

  it('keeps the draft when the first unanswered question refuses free text', async () => {
    const noText: QuestionEvent = {
      ...pendingQuestion,
      questions: [{ ...(pendingQuestion.questions[0] as QuestionSpec), allow_text: false }],
    };
    const props = composerProps(sharedWaiting, attachAgent);
    render(<Composer {...props} question={noText} />);
    const field = screen.getByLabelText('Message the agent…');
    await userEvent.click(field);
    await userEvent.keyboard('clamp it');
    await userEvent.click(screen.getByRole('button', { name: 'Answer' }));

    expect(props.onAnswer).not.toHaveBeenCalled();
    expect(field).toHaveValue('clamp it');
  });

  it('shows a free-text answer back on the resolved card, but never a secret', () => {
    const q = pendingQuestion.questions[0] as QuestionSpec;
    const resolved: QuestionEvent = {
      ...pendingQuestion,
      status: 'resolved',
      by: 'remote',
      answers: { q1: 'clamp it, but log the skew' },
    };
    const { unmount } = render(<QuestionCard event={resolved} onAnswer={vi.fn()} />);
    expect(screen.getByLabelText(q.prompt)).toHaveValue('clamp it, but log the skew');
    unmount();

    render(
      <QuestionCard
        event={{ ...resolved, questions: [{ ...q, secret: true }] }}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByLabelText(q.prompt)).toHaveValue('');
  });

  it('sends the card selections alongside the draft', () => {
    const two: QuestionEvent = {
      ...pendingQuestion,
      questions: [
        pendingQuestion.questions[0] as QuestionSpec,
        {
          id: 'q2',
          prompt: 'Anything else?',
          options: [],
          multi: false,
          allow_text: true,
        },
      ],
    };
    const draft = { selection: { q1: ['widen'] }, text: {} };
    expect(composeAnswer(two, draft, 'and raise the timeout')).toEqual({
      q1: ['widen'],
      q2: 'and raise the timeout',
    });
    // Nothing selected anywhere: the draft answers the first question.
    expect(composeAnswer(two, emptyDraft, 'clamp it')).toEqual({ q1: 'clamp it' });
  });
});

/**
 * A28 — Grok Build attaches through its leader, the one backend process a
 * machine runs when `[cli] use_leader` is on. The app needs no Grok logic:
 * `control` and the five attachment fields of §4.2 say the whole of it.
 */
describe('A28 Grok Build on the leader', () => {
  const LEADER_HINT =
    'Run rc-client grok setup on the device, then restart Grok to attach its sessions';
  const RESTART_HINT =
    'This terminal session was started without the attachment; restart it to control it from here';

  const mockSession = (id: string): Session => {
    const found = sessions.find((s) => s.session_id === id);
    if (!found) throw new Error(`no such mock session ${id}`);
    return found;
  };

  const grokShared = mockSession('ses-grok-shared');
  const grokTerminal = mockSession('ses-grok-no-leader');

  it('reports the leader attachment with every flag set, attachments included (A49)', () => {
    expect(grokAgent.attach).toBe('leader');
    expect(grokAgent.attach_ready).toBe(true);
    expect(grokAgent.shared_interrupt).toBe(true);
    expect(grokAgent.shared_settings).toBe(true);
    expect(grokAgent.capabilities).toContain('attachments');
    expect(grokAgent.shared_attachments).toBe(true);
  });

  it('asks for the setup command while the config has not turned the leader on', () => {
    expect(grokNoLeader.attach).toBe('leader');
    expect(attachHint(grokNoLeader)).toBe(LEADER_HINT);
  });

  it('asks for a restart once the device is ready to attach', () => {
    expect(attachHint(grokAgent)).toBe(RESTART_HINT);
  });

  it('words the hint under the terminal bar, with no Take over beside it', () => {
    render(<Composer {...composerProps(grokTerminal, grokNoLeader)} />);
    expect(screen.getByText(LEADER_HINT)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('leaves the composer and both pickers live on a shared Grok session', () => {
    expect(grokShared.control).toBe('shared');
    expect(grokShared.origin).toBe('terminal');
    expect(canSetShared(grokAgent, 'model')).toBe(true);
    expect(canSetShared(grokAgent, 'permission_mode')).toBe(true);
    render(<Composer {...composerProps(grokShared, grokAgent)} />);
    expect(screen.getByLabelText('Message the agent…')).toBeEnabled();
    for (const name of ['Model and effort', 'Permission mode']) {
      expect(screen.getByRole('button', { name })).toBeEnabled();
    }
    expect(screen.queryByText(LEADER_HINT)).not.toBeInTheDocument();
  });

  it('draws the attachment button, because a prompt names the files the device wrote', () => {
    expect(canAttachShared(grokAgent)).toBe(true);
    render(<Composer {...composerProps(grokShared, grokAgent)} />);
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled();
  });

  it('shows Stop while the turn the terminal started is running', () => {
    expect(canInterruptShared(grokAgent)).toBe(true);
    expect(grokShared.state).toBe('running');
    render(
      <MemoryRouter>
        <ChatHeader
          session={grokShared}
          agent={grokAgent}
          deviceName="mac-studio-office"
          todos={[]}
          stopping={false}
          onStop={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });
});

/**
 * A40 — the device types into an attached Claude Code terminal. The shim runs
 * the CLI inside a pseudo-terminal the device owns, so `/model`, `/effort` and
 * `/compact` can be typed in as the person would. `shared_settings_keys` says
 * which settings that covers; the rest stay the terminal's (A17). Nothing here
 * is Claude-specific in the app: it reads the list and draws each setting one
 * way or the other.
 */
describe.runIf(fixturesAvailable())('A40 the device types into a Claude terminal', () => {
  const claudeShared = (agent: AgentInfo) => render(<Composer {...composerProps(sharedIdle, agent)} />);

  it('decodes the two settings the terminal gives up, and the command capability', () => {
    expect(attachAgent.attach).toBe('channel');
    expect(attachAgent.shared_settings).toBe(true);
    expect(attachAgent.shared_settings_keys).toEqual(['model', 'effort']);
    expect(attachAgent.capabilities).toContain('commands');
    // A42 added the interrupt to the same pseudo-terminal; A49 the attachments,
    // as files the words the device injects name.
    expect(attachAgent.shared_interrupt).toBe(true);
    expect(attachAgent.shared_attachments).toBe(true);
  });

  it('answers per key, and falls back to all four when the list is absent', () => {
    expect(canSetShared(attachAgent, 'model')).toBe(true);
    expect(canSetShared(attachAgent, 'effort')).toBe(true);
    expect(canSetShared(attachAgent, 'permission_mode')).toBe(false);
    expect(canSetShared(attachAgent, 'speed')).toBe(false);

    const noList: AgentInfo = { ...attachAgent };
    delete noList.shared_settings_keys;
    for (const key of SETTING_KEYS) expect(canSetShared(noList, key)).toBe(true);

    // The boolean still decides first: a list without it carries nothing.
    const off: AgentInfo = { ...attachAgent, shared_settings: false };
    for (const key of SETTING_KEYS) expect(canSetShared(off, key)).toBe(false);
  });

  it('opens the model card and leaves the permission mode a value', () => {
    claudeShared(attachAgent);
    expect(screen.getByRole('button', { name: 'Model and effort' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Permission mode' })).not.toBeInTheDocument();
    expect(
      screen.getByLabelText('Permission mode · Ask before edits · set in the terminal'),
    ).toBeInTheDocument();
    // A49: the channel names the files the device wrote, so the attachment
    // button is there too.
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled();
  });

  it('gives the card a live model list and a live effort slider', async () => {
    claudeShared(attachAgent);
    await userEvent.click(screen.getByRole('button', { name: 'Model and effort' }));
    expect(screen.getByRole('slider', { name: 'Effort' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Model, Sonnet 4.5' }));
    expect(screen.getByRole('listbox', { name: 'Model' })).toBeInTheDocument();
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Opus 4.6',
      'Sonnet 4.5',
      'Haiku 4.5',
    ]);
  });

  it('sends the model the card picked', async () => {
    const props = composerProps(sharedIdle, attachAgent);
    render(<Composer {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Model and effort' }));
    await userEvent.click(screen.getByRole('button', { name: 'Model, Sonnet 4.5' }));
    await userEvent.click(screen.getByRole('option', { name: 'Haiku 4.5' }));
    expect(props.onSetOption).toHaveBeenCalledWith({ model: 'claude-haiku-4-5' });
  });

  it('reads what the terminal confirmed, not what was asked for', () => {
    const shown = () => document.querySelector('.model-card-chip .sized-box-shown')?.textContent;
    const { rerender } = claudeShared(attachAgent);
    expect(shown()).toBe('Sonnet 4.5 Medium');

    // What the socket carries once the typed `/model` is in the transcript.
    const next = foldSession(sharedIdle, [
      { seq: 12, ts: 1788946200012, kind: 'meta', model: 'claude-haiku-4-5' },
    ]);
    rerender(<Composer {...composerProps(next, attachAgent)} />);
    expect(shown()).toBe('Haiku 4.5 Medium');
  });

  it('draws only the rows the agent named, never a control that would fail', async () => {
    const modelOnly: AgentInfo = { ...attachAgent, shared_settings_keys: ['model'] };
    const { unmount } = claudeShared(modelOnly);
    await userEvent.click(screen.getByRole('button', { name: 'Model and effort' }));
    expect(screen.getByRole('button', { name: 'Model, Sonnet 4.5' })).toBeEnabled();
    expect(screen.queryByRole('slider', { name: 'Effort' })).not.toBeInTheDocument();
    unmount();

    const effortOnly: AgentInfo = { ...attachAgent, shared_settings_keys: ['effort'] };
    claudeShared(effortOnly);
    await userEvent.click(screen.getByRole('button', { name: 'Model and effort' }));
    expect(screen.getByRole('slider', { name: 'Effort' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Model, Sonnet 4.5' })).not.toBeInTheDocument();
  });

  it('keeps every setting the terminal\'s on a session no attachment reaches', () => {
    render(<Composer {...composerProps(terminalSession(), attachAgent)} />);
    expect(screen.queryByRole('button', { name: 'Model and effort' })).not.toBeInTheDocument();
    expect(document.querySelectorAll('.composer-chip.readonly')).toHaveLength(2);
  });

  it('offers the one command the terminal can be typed', () => {
    expect(commandsFor('claude')).toEqual([
      {
        name: 'compact',
        description: 'Summarise the conversation so far to free context',
        group: 'Built-in',
      },
    ]);
    // A40: the shim is what provides the terminal, so a device without it
    // offers neither the commands nor the settings.
    expect(claudeAgent.capabilities).toContain('commands');
    expect(claudeNoShim.capabilities).not.toContain('commands');
    expect(claudeNoShim.shared_settings).toBe(false);
    expect(claudeNoShim.shared_settings_keys).toBeUndefined();
  });

  it('shows a busy terminal in the device\'s own words', () => {
    const busy = new RequestError({ code: 'conflict', message: BUSY });
    expect(refusalText(busy, strings.errors.setFailed)).toBe(BUSY);
    // Every other code keeps the app's sentence, in the app's language.
    expect(refusalText(new RequestError({ code: 'timeout', message: 'gone' }), 'fallback')).toBe(
      strings.errors.timeout,
    );
    // A conflict with nothing to say still gets one.
    expect(refusalText(new RequestError({ code: 'conflict', message: '' }), 'fallback')).toBe(
      strings.errors.conflictTerminal,
    );
  });

  it('reports a refused command under the field, as the device worded it', async () => {
    const props = composerProps(sharedIdle, attachAgent);
    const onRunCommand = vi.fn().mockRejectedValue(
      new RequestError({ code: 'conflict', message: BUSY }),
    );
    render(
      <Composer {...props} commands={commandsFor('claude')} onRunCommand={onRunCommand} />,
    );
    const field = screen.getByLabelText('Message the agent…');
    await userEvent.click(field);
    await userEvent.keyboard('/compact');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(onRunCommand).toHaveBeenCalledWith('compact', undefined);
    expect(await screen.findByText(BUSY)).toBeInTheDocument();
  });
});

/**
 * A42 — Stop on an attached Claude Code terminal. The pseudo-terminal the
 * shim gave the CLI takes a typed Escape, so the channel now carries the
 * interrupt like every other attachment, and the app draws Stop and
 * "Interrupt & send" from the same flag it always read. The one thing the
 * keystroke cannot do is talk over a prompt: the device refuses, in its own
 * words. `docs/DESIGN.md` § "The device types into a Claude terminal (A40)".
 */
describe.runIf(fixturesAvailable())('A42 Stop on an attached Claude terminal', () => {
  /** What the device answers when Escape would answer the prompt instead. */
  const PROMPT = 'answer the prompt first';

  it('reads the interrupt off the channel, the way it reads a daemon\'s', () => {
    expect(canInterruptShared(attachAgent)).toBe(true);
    expect(canInterruptShared(daemonAgent)).toBe(true);
    // The shim is what provides the terminal, so a device without it carries
    // no interrupt either.
    expect(claudeNoShim.shared_interrupt).toBe(false);
    expect(canInterruptShared(claudeNoShim)).toBe(false);
  });

  it('matches the mock gateway, so the demo session behaves like the contract', () => {
    expect(claudeAgent.shared_interrupt).toBe(attachAgent.shared_interrupt);
    expect(claudeAgent.attach).toBe('channel');
  });

  it('draws Stop while the turn the terminal started is running', () => {
    render(
      <MemoryRouter>
        <ChatHeader
          session={sharedRunning}
          agent={attachAgent}
          deviceName="mac-studio-office"
          todos={[]}
          stopping={false}
          onStop={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });

  it('keeps an open prompt in the device\'s words rather than the canned one', () => {
    const refused = new RequestError({ code: 'conflict', message: PROMPT });
    expect(refusalText(refused, strings.errors.stopFailed)).toBe(PROMPT);
    expect(refusalText(refused, strings.errors.stopFailed)).not.toBe(
      strings.errors.conflictTerminal,
    );
  });
});

describe.runIf(fixturesAvailable())('A51 steering the turn of an attached Claude terminal', () => {
  const remoteRunning: Session = { ...sharedRunning, control: 'remote', origin: 'remote' };

  it('steers a shared session only when the device reports shared_steer', () => {
    expect(canSteer(attachSteer, sharedRunning)).toBe(true);
    expect(canSteer(attachNoSteer, sharedRunning)).toBe(false);
    expect(canSteer({ ...attachAgent, shared_steer: false }, sharedRunning)).toBe(false);
    expect(canSteer(null, sharedRunning)).toBe(false);
  });

  it('leaves a session the device started queueing behind the turn', () => {
    expect(canSteer(attachSteer, remoteRunning)).toBe(false);
    expect(canSteer(attachSteer, terminalSession({ state: 'running' }))).toBe(false);
  });

  it('changes nothing for Codex and pi, which steer by their capability', () => {
    for (const agent of [daemonAgent, codexAgent, piAgent]) {
      expect(agent.capabilities).toContain('steer');
      expect(canSteer(agent, codexShared)).toBe(true);
      expect(canSteer(agent, { ...codexShared, control: 'remote' })).toBe(true);
      expect(canSteer({ ...agent, shared_steer: false }, codexShared)).toBe(true);
    }
  });
});
