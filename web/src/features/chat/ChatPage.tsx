import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { errorText, queueRemoveText, refusalText } from '../../lib/errors';
import { rpc } from '../../lib/gateway';
import { strings } from '../../strings';
import { useChat } from '../../stores/chat';
import { commandsOf, useCommands } from '../../stores/commands';
import { useConnection } from '../../stores/connection';
import { useDevices } from '../../stores/devices';
import { useDrafts } from '../../stores/drafts';
import { useOutbox } from '../../stores/outbox';
import { sessionKey, useSessions } from '../../stores/sessions';
import { emptyTimeline, selectPendingQuestion } from '../../stores/timeline';
import type { SendMode } from '../../protocol/frames';
import type { Command, QuestionAnswers } from '../../protocol/types';
import { polishContext } from '../voice/polish';
import { NewSessionDrawer } from '../sessions/NewSessionDrawer';
import { ChatHeader } from './ChatHeader';
import { Composer } from './Composer';
import { ResumeNotice } from './ResumeNotice';
import { Sidebar } from './Sidebar';
import { applyOptions, type SessionOptions } from './sessionOptions';
import { StatusLine } from './StatusLine';
import { Timeline } from './Timeline';
import { useFrontConversation } from './useFrontConversation';
import type { AttachmentDraft } from './attachments';
import './chat.css';

/** Stable identity, so an unopened session does not rebuild the view each render. */
const NO_TIMELINE = emptyTimeline();

/** A27: what an agent without capability `commands` offers, once and for all. */
const EMPTY_COMMANDS: Command[] = [];

export function ChatPage() {
  const { deviceId = '', sessionId = '' } = useParams();
  const key = sessionKey(deviceId, sessionId);
  const navigate = useNavigate();

  const openChat = useChat((s) => s.open);
  const closeChat = useChat((s) => s.close);
  const chat = useChat((s) => s.sessions[key]);
  const sendMessage = useChat((s) => s.send);
  const retrySend = useChat((s) => s.retrySend);
  const stopTurn = useChat((s) => s.stop);
  const approveRequest = useChat((s) => s.approve);
  const answerQuestion = useChat((s) => s.answer);
  const expandBlock = useChat((s) => s.expandBlock);
  const loadOlder = useChat((s) => s.loadOlder);
  const removeQueued = useChat((s) => s.removeQueued);
  const runCommand = useChat((s) => s.runCommand);
  const commands = useCommands(commandsOf(sessionId));
  const session = useSessions((s) => s.sessions[key]);
  const takeoverSession = useSessions((s) => s.takeover);
  const devices = useDevices((s) => s.devices);
  const socketStatus = useConnection((s) => s.status);
  const stt = useConnection((s) => s.stt);
  const polish = useConnection((s) => s.polish);
  const pending = useOutbox((s) => s.pending);
  // A43: the composer holds a queued message, which goes back into the line.
  const editingQueued = useDrafts((s) => (s.drafts[key]?.editing ?? null) !== null);

  const [stopping, setStopping] = useState(false);
  const [creating, setCreating] = useState(false);
  // Every request the timeline can issue reports its failure here rather than
  // rejecting into an unhandled promise.
  const [actionError, setActionError] = useState<string | null>(null);

  const device = useMemo(
    () => devices.find((d) => d.device_id === deviceId) ?? null,
    [devices, deviceId],
  );
  const agent = useMemo(
    () => device?.agents.find((a) => a.agent === session?.agent) ?? null,
    [device, session?.agent],
  );

  useEffect(() => {
    if (!deviceId || !sessionId) return;
    if (socketStatus !== 'open') return;
    openChat(deviceId, sessionId);
    return () => closeChat(deviceId, sessionId);
  }, [deviceId, sessionId, socketStatus, openChat, closeChat]);

  // A47: an open conversation is a seen one only while the page is in front.
  useFrontConversation(deviceId, sessionId);

  // A27: the list is fetched when the conversation opens, so the panel is on
  // screen the moment `/` is typed. An agent without the capability is never
  // asked — the device would answer `unsupported`.
  const hasCommands = agent?.capabilities.includes('commands') ?? false;
  useEffect(() => {
    if (!sessionId || !hasCommands || socketStatus !== 'open') return;
    useCommands.getState().open(sessionId);
  }, [sessionId, hasCommands, socketStatus]);

  const onCommandsNeeded = useCallback(() => {
    if (!hasCommands) return;
    useCommands.getState().refresh(sessionId);
  }, [hasCommands, sessionId]);

  /**
   * A27: the refusal travels on to the composer, which reports it under the
   * field the way a failed send does, in the device's own words.
   */
  const onRunCommand = useCallback(
    async (name: string, argument?: string) => {
      setActionError(null);
      await runCommand(key, name, argument);
    },
    [runCommand, key],
  );

  const unconfirmed = useMemo(
    () => Object.values(pending).filter((p) => p.sessionKey === key && p.error !== null),
    [pending, key],
  );

  // A20: the composer answers the question the timeline is waiting on.
  const timeline = chat?.timeline ?? NO_TIMELINE;
  const question = useMemo(() => selectPendingQuestion(timeline), [timeline]);

  // A29: the conversation a dictation is polished against, read at the moment
  // the dictation ends rather than held by the composer.
  const buildPolishContext = useCallback(() => polishContext(timeline), [timeline]);

  const onSend = useCallback(
    async (text: string, attachments: AttachmentDraft[], mode: SendMode, queueTs?: number) => {
      await sendMessage(key, {
        text,
        mode,
        attachments: attachments.map(({ name, mime, data_base64 }) => ({
          name,
          mime,
          data_base64,
        })),
        ...(queueTs === undefined ? {} : { queueTs }),
      });
    },
    [sendMessage, key],
  );

  /**
   * Every change the model card makes is drawn the moment it is made: the
   * lightning fills, the word changes, the model name switches, and the
   * device's reply confirms it. A refusal puts the previous session back —
   * unless something newer has already replaced what we wrote, which is a
   * `session.updated` that arrived in between or the reply itself, so the
   * check is on the stored object's identity rather than on its fields.
   */
  const onSetOption = useCallback(
    (patch: SessionOptions) => {
      const store = useSessions.getState();
      const previous = store.sessions[key];
      if (!previous) return;
      /**
       * A40: a shared session has a terminal showing the same thing, and the
       * device may have to type the change into it. The card follows the
       * reply there — which comes once the terminal has taken it — rather
       * than running ahead of the screen beside it. Everywhere else the
       * change is drawn the moment it is made.
       */
      const optimistic = previous.control === 'shared' ? null : applyOptions(previous, patch);
      if (optimistic) store.upsert(optimistic);
      void rpc('session.set', { session_id: sessionId, ...patch })
        .then((result) => useSessions.getState().upsert(result.session))
        .catch((err: unknown) => {
          const sessions = useSessions.getState();
          if (optimistic && sessions.sessions[key] === optimistic) sessions.upsert(previous);
          setActionError(refusalText(err, strings.errors.setFailed));
        });
    },
    [key, sessionId],
  );

  const onTakeover = useCallback(() => {
    if (!session) return;
    void takeoverSession(session).catch((err: unknown) =>
      setActionError(errorText(err, strings.errors.takeoverFailed)),
    );
  }, [session, takeoverSession]);

  /**
   * A42: Stop on a terminal the device types into is an Escape, which the
   * device refuses to type while the CLI has a prompt up. That `conflict` is
   * the device's own sentence — the canned one, about taking the session over,
   * would send the reader the wrong way.
   */
  const onStop = useCallback(() => {
    setStopping(true);
    setActionError(null);
    void stopTurn(key)
      .catch((err: unknown) => setActionError(refusalText(err, strings.errors.stopFailed)))
      .finally(() => setStopping(false));
  }, [stopTurn, key]);

  const onApprove = useCallback(
    async (requestId: string, optionId: string) => {
      setActionError(null);
      try {
        await approveRequest(key, requestId, optionId);
      } catch (err) {
        setActionError(errorText(err, strings.errors.approveFailed));
      }
    },
    [approveRequest, key],
  );

  /**
   * The banner reports the failure; the rejection travels on so the card and
   * the composer keep what was filled in rather than clearing it (A20).
   */
  const onAnswer = useCallback(
    async (requestId: string, answers: QuestionAnswers) => {
      setActionError(null);
      try {
        await answerQuestion(key, requestId, answers);
      } catch (err) {
        setActionError(errorText(err, strings.errors.answerFailed));
        throw err;
      }
    },
    [answerQuestion, key],
  );

  const onOpenFull = useCallback(
    async (blockId: string) => {
      setActionError(null);
      try {
        await expandBlock(key, blockId);
      } catch (err) {
        setActionError(errorText(err, strings.errors.expandFailed));
      }
    },
    [expandBlock, key],
  );

  // A43: a message the device sent before the Remove reached it is not an
  // error of ours, and the banner says what happened to it.
  const onRemoveQueued = useCallback(
    (queuedId: string) => {
      void removeQueued(key, queuedId).catch((err: unknown) =>
        setActionError(queueRemoveText(err)),
      );
    },
    [removeQueued, key],
  );

  /**
   * A43: an edit takes the message out of the line before the field takes it.
   * The refusal travels on to the composer, which says under the field why
   * nothing opened — most often that the device had already sent it.
   */
  const onTakeQueued = useCallback(
    async (queuedId: string) => {
      setActionError(null);
      await removeQueued(key, queuedId);
    },
    [removeQueued, key],
  );

  const onLoadOlder = useCallback(() => void loadOlder(key), [loadOlder, key]);

  /**
   * A35: both resume actions answer with the session, so the notice follows the
   * device rather than this app's guess. Change reports its own failure inside
   * the popover, so the rejection travels on.
   */
  const onResumeSet = useCallback(
    async (at: number) => {
      setActionError(null);
      const result = await rpc('session.resume_set', { session_id: sessionId, at });
      useSessions.getState().upsert(result.session);
    },
    [sessionId],
  );

  const onResumeCancel = useCallback(() => {
    setActionError(null);
    void rpc('session.resume_cancel', { session_id: sessionId })
      .then((result) => useSessions.getState().upsert(result.session))
      .catch((err: unknown) => setActionError(errorText(err, strings.errors.resumeCancelFailed)));
  }, [sessionId]);

  if (!session) {
    return (
      <div className="chat-layout">
        <Sidebar activeKey={key} onNewSession={() => setCreating(true)} />
        <div className="chat-main">
          <div className="empty card chat-missing">
            <strong>{strings.errors.sessionMissing}</strong>
            <button type="button" className="btn small" onClick={() => navigate('/sessions')}>
              {strings.nav.backToSessions}
            </button>
          </div>
        </div>
        <NewSessionDrawer open={creating} onClose={() => setCreating(false)} />
      </div>
    );
  }

  return (
    <div className="chat-layout">
      <Sidebar activeKey={key} onNewSession={() => setCreating(true)} />

      <div className="chat-main">
        <ChatHeader
          session={session}
          agent={agent}
          deviceName={device?.name ?? deviceId}
          todos={chat?.todos ?? []}
          stopping={stopping}
          onStop={onStop}
        />

        {/*
          A35, §8 rule 17: a session whose `resume` is set says so above its
          transcript, and the notice goes when `resume` does.
        */}
        {session.resume ? (
          <ResumeNotice
            resume={session.resume}
            onSet={onResumeSet}
            onCancel={onResumeCancel}
          />
        ) : null}

        <Timeline
          timeline={timeline}
          historyLoading={chat?.historyLoading ?? false}
          historyHasMore={chat?.historyHasMore ?? false}
          onOpenFull={onOpenFull}
          onApprove={onApprove}
          onAnswer={onAnswer}
          onLoadOlder={onLoadOlder}
          footer={
            <StatusLine
              session={session}
              agent={agent}
              deviceOnline={device?.online ?? false}
              editingQueued={editingQueued}
              onTakeover={onTakeover}
            />
          }
        />

        {actionError ? (
          <div className="action-error" role="alert">
            <span>{actionError}</span>
            <button type="button" className="btn small ghost" onClick={() => setActionError(null)}>
              {strings.common.dismiss}
            </button>
          </div>
        ) : null}

        {unconfirmed.length > 0 ? (
          <div className="unconfirmed" role="alert">
            <span>
              <strong>{strings.composer.deliveryUnconfirmed}</strong>{' '}
              {strings.composer.deliveryUnconfirmedBody}
            </span>
            <button
              type="button"
              className="btn small"
              onClick={() => {
                for (const entry of unconfirmed) {
                  retrySend(entry.id).catch((err: unknown) =>
                    setActionError(errorText(err, strings.composer.sendFailed)),
                  );
                }
              }}
            >
              {strings.common.retry}
            </button>
            <button
              type="button"
              className="btn small ghost"
              onClick={() => {
                for (const entry of unconfirmed) useOutbox.getState().clear(entry.id);
              }}
            >
              {strings.common.dismiss}
            </button>
          </div>
        ) : null}

        {/*
          `docs/DESIGN.md` § "The composer" — **A draft belongs to its
          session**. The route keeps one `ChatPage` across a switch, so the key
          is what remounts the composer: a dictation running in the session
          being left ends there (`useVoice` tears down on unmount) and its words
          stay in that session's draft, which the store kept.
        */}
        <Composer
          key={key}
          session={session}
          agent={agent}
          deviceOnline={device?.online ?? false}
          queue={chat?.queue ?? []}
          question={question}
          sttEnabled={stt.enabled}
          polishEnabled={polish.enabled}
          polishContext={buildPolishContext}
          commands={hasCommands ? commands : EMPTY_COMMANDS}
          onCommandsNeeded={onCommandsNeeded}
          onRunCommand={onRunCommand}
          onSend={onSend}
          onAnswer={onAnswer}
          onSetOption={onSetOption}
          onRemoveQueued={onRemoveQueued}
          onTakeQueued={onTakeQueued}
          onTakeover={onTakeover}
        />
      </div>

      <NewSessionDrawer
        open={creating}
        onClose={() => setCreating(false)}
        {...(deviceId ? { presetDeviceId: deviceId } : {})}
      />
    </div>
  );
}
