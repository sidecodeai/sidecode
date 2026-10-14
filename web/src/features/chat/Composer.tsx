import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { ArrowUp, ChevronRight, Mic, Paperclip, X, Zap } from 'lucide-react';
import { Menu, Popover } from '../../components/Popover';
import { api } from '../../lib/api';
import { errorText, refusalText } from '../../lib/errors';
import { bytes } from '../../lib/format';
import { cx } from '../../lib/cx';
import { agentLabel, strings } from '../../strings';
import { useSettings } from '../../stores/settings';
import type { SendMode } from '../../protocol/frames';
import type {
  AgentInfo,
  Choice,
  Command,
  PolishContextItem,
  QuestionAnswers,
  QuestionEvent,
  QueuedMessage,
  Session,
  SharedSettingKey,
} from '../../protocol/types';
import { draftOf as answerDraftOf, useAnswers } from '../../stores/answers';
import { draftOf, useDrafts } from '../../stores/drafts';
import { useImeGuard } from './useImeGuard';
import { sessionKey } from '../../stores/sessions';
import { VoiceControls } from '../voice/VoiceControls';
import { WorkingPill } from '../voice/WorkingPill';
import { mergeDraft } from '../voice/draft';
import { primarySlot } from '../voice/primarySlot';
import {
  applyPolished,
  canPolish,
  polishRequest,
  undoPolished,
  type PolishSpan,
} from '../voice/polish';
import { useVoice } from '../voice/useVoice';
import { composeAnswer } from './answering';
import { attachHint, canAttachShared, canInterruptShared, canSetShared } from './attach';
import { MAX_ATTACHMENTS, readAttachments, textTooLong } from './attachments';
import { CommandHint, CommandMenu } from './CommandMenu';
import { commandQuery, completionFor, filterCommands, matchCommand } from './commands';
import { SizedBox } from './SizedBox';
import { labelPairs, type LabelPair } from './modelLabels';
import type { SessionOptions } from './sessionOptions';
import { EditingStrip, UpNext } from './UpNext';
import { useQueuedEdit, type Send } from './useQueuedEdit';

interface Props {
  session: Session;
  agent: AgentInfo | null;
  deviceOnline: boolean;
  queue: QueuedMessage[];
  /** A20: the question the session is waiting on, when there is one. */
  question: QuestionEvent | null;
  sttEnabled: boolean;
  /** A29: this gateway has a polish model, so the setting can take effect. */
  polishEnabled?: boolean;
  /**
   * A29: the conversation the polish model is given, read when a dictation ends
   * rather than on every render — the page owns the timeline, not the composer.
   */
  polishContext?: () => PolishContextItem[];
  /**
   * A27: the slash commands this session offers now. Empty for an agent
   * without capability `commands`, and the panel is then never drawn: `/` is
   * an ordinary character there.
   */
  commands?: Command[];
  /** `queueTs` only for an edited queued message going back into the line (A43). */
  onSend: Send;
  onAnswer: (requestId: string, answers: QuestionAnswers) => Promise<void>;
  onSetOption: (patch: SessionOptions) => void;
  onRemoveQueued: (queuedId: string) => void;
  /**
   * A43: take a queued message out of the line so the field can edit it;
   * rejects with the device's refusal. Without it the list offers Remove alone.
   */
  onTakeQueued?: (queuedId: string) => Promise<void>;
  onTakeover: () => void;
  /** A27: `/` was typed, so the list is asked for again if it is stale. */
  onCommandsNeeded?: () => void;
  /** A27: run the command the first word names. */
  onRunCommand?: (name: string, argument?: string) => Promise<void>;
}

const NO_COMMANDS: Command[] = [];

/**
 * A29 — where a dictation is between the recogniser and the model: nowhere,
 * waiting for the answer, holding one that can still be undone, or told that the
 * request failed and the words were left alone.
 */
type PolishState =
  | { phase: 'idle' }
  | { phase: 'polishing' }
  | { phase: 'polished'; span: PolishSpan; text: string }
  | { phase: 'failed' };

const POLISH_IDLE: PolishState = { phase: 'idle' };

/** How long the one line about a failed polish stays before it goes away. */
const POLISH_FAILED_MS = 5_000;

export function Composer({
  session,
  agent,
  deviceOnline,
  queue,
  question,
  sttEnabled,
  polishEnabled = false,
  polishContext,
  commands = NO_COMMANDS,
  onSend,
  onAnswer,
  onSetOption,
  onRemoveQueued,
  onTakeQueued,
  onTakeover,
  onCommandsNeeded,
  onRunCommand,
}: Props) {
  /**
   * `docs/DESIGN.md` § "The composer" — **A draft belongs to its session**. The
   * words and the files are the session's, not this component's, so opening
   * another conversation shows its own draft and can never send this one's.
   */
  const key = sessionKey(session.device_id, session.session_id);
  const draft = useDrafts(draftOf(key));
  const text = draft.text;
  const attachments = draft.attachments;
  /**
   * A43: the queued message the field holds, when it holds one. While it does
   * the field is that message and nothing else: no command, no answer.
   */
  const editing = draft.editing;
  const [errors, setErrors] = useState<string[]>([]);
  // A27: the row the keyboard is on, and whether Esc has put the panel away
  // until the draft changes again.
  const [highlight, setHighlight] = useState(0);
  const [menuClosed, setMenuClosed] = useState(false);
  const menuId = useId();
  const ime = useImeGuard();
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const textRef = useRef(text);
  /**
   * The draft dictation started from, and the value it last wrote. A transcript
   * arrives between renders, so the field is read from this ref rather than
   * from state, and `applied` is how a keystroke that landed in between is told
   * apart from our own write.
   */
  const dictation = useRef<{ base: string; applied: string } | null>(null);
  /**
   * `docs/DESIGN.md` § "The composer" — **While dictation runs, the field
   * follows the words**. A dictated write has no caret to keep the newest line
   * on screen, so the write itself asks for the tail and the next measurement
   * of the field grants it. Typing never sets this: a caret keeps itself
   * visible.
   */
  const followTail = useRef(false);

  /** Keeps `textRef` in step within the tick, which `useEffect` cannot. */
  const setDraft = useCallback(
    (value: string) => {
      textRef.current = value;
      useDrafts.getState().setText(key, value);
      // A27: a changed draft is a changed list, so the highlight goes back to
      // the first match and a panel that was dismissed is open again.
      setHighlight(0);
      setMenuClosed(false);
    },
    [key],
  );

  useEffect(() => {
    textRef.current = text;
  }, [text]);

  // A29: the reader's own choices. The gateway only says whether it can polish.
  const polishChosen = useSettings((s) => s.polishEnabled);
  const polishModel = useSettings((s) => s.polishModel);
  const polishStrength = useSettings((s) => s.polishStrength);
  // A20: what the card on screen already holds, so the draft completes it
  // rather than competing with it.
  const answerDraft = useAnswers(answerDraftOf(question?.request_id ?? ''));
  const clearAnswer = useAnswers((s) => s.clear);

  /**
   * A29: the polish run, and the counter that ends one. A send, an edit or a
   * new dictation bumps it, and an answer whose run is no longer current is
   * dropped — the words on screen are the person's, not a late model's.
   */
  const [polish, setPolish] = useState<PolishState>(POLISH_IDLE);
  const polishRun = useRef(0);
  const polishReady = polishEnabled && polishChosen && polishModel.length > 0;

  const dropPolish = useCallback(() => {
    polishRun.current += 1;
    setPolish((current) => (current.phase === 'idle' ? current : POLISH_IDLE));
  }, []);

  const terminalControlled = session.control === 'terminal';
  // A10: a shared session is a live CLI the device is attached to. Everything
  // the composer does works, except what has to go through the terminal.
  const shared = session.control === 'shared';
  const running =
    session.state === 'running' ||
    session.state === 'needs_approval' ||
    session.state === 'needs_input' ||
    session.state === 'starting';
  const canSteer = agent?.capabilities.includes('steer') ?? false;
  const canTakeover = agent?.capabilities.includes('takeover') ?? false;
  // A7: the composer is gated on `control`, never on `state` — a mirrored
  // session reports `running` while the terminal drives the turn.
  const disabled = terminalControlled || !deviceOnline;
  // A10: the channel cannot interrupt a running turn, so neither can we.
  const canInterrupt = shared ? canInterruptShared(agent) : true;
  // A11: the model, permission mode, effort and attachments belong to the
  // terminal unless the device reports that the attachment carries them. A
  // control the attachment cannot drive is hidden, never disabled with a
  // caption, so nothing in the composer explains an absence.
  //
  // A17/A40: where a picker cannot go — a terminal session, which refuses
  // `session.set` outright, and a shared one whose agent does not carry that
  // setting — the value the device read from the transcript is shown instead,
  // so the person can at least see what the terminal chose. The question is
  // asked one setting at a time: an attached Claude takes a typed `/model` and
  // `/effort` and leaves the permission mode where the terminal put it.
  const canSet = (key: SharedSettingKey): boolean =>
    !terminalControlled && (!shared || canSetShared(agent, key));
  const showAttach = !shared || canAttachShared(agent);

  // A20: while a question is pending the field is that question's free-text
  // answer, so nothing is sent and nothing is queued behind it. A43: a queued
  // message being edited keeps the field, and the question waits for it.
  const answering = question !== null && !disabled && editing === null;
  const answer = answering ? composeAnswer(question, answerDraft, text) : null;

  // A27 — the terminal's `/` menu. The field is a command's while it is a
  // slash and a partial name; a complete first word the session offers is what
  // Send runs, and anything else is text, the way a terminal treats an unknown
  // slash. Neither happens while a question is waiting for this field, nor
  // while the field holds a queued message (A43).
  const commandable = !answering && !disabled && editing === null;
  const query = commandable ? commandQuery(text) : null;
  const rows = query === null ? NO_COMMANDS : filterCommands(commands, query);
  const menuOpen = rows.length > 0 && !menuClosed;
  const highlighted = rows[Math.min(highlight, rows.length - 1)] ?? null;
  const commandMatch = commandable ? matchCommand(commands, text) : null;
  const optionId = useCallback((index: number) => `${menuId}-command-${index}`, [menuId]);

  // A43: a queued message taken back into the field, and put back again. The
  // field lets go of a dictation and of a polish note when it takes one.
  const {
    begin: editQueued,
    putBack,
    cancel: cancelEdit,
  } = useQueuedEdit({
    draftKey: key,
    editing,
    field: textarea,
    onTake: onTakeQueued,
    onSend,
    onErrors: setErrors,
    onBegin: () => {
      takeFieldBack();
      dropPolish();
    },
  });

  /** Take a row: `/name ` when it takes an argument, `/name` when it does not. */
  const takeCommand = useCallback(
    (command: Command) => {
      setDraft(completionFor(command));
      textarea.current?.focus();
    },
    [setDraft],
  );

  /**
   * A20: submit the draft as the free-text answer of the first question with
   * no selection, alongside whatever the card holds. There is no optimistic
   * row: an answer is not a message, and the card resolving is the receipt.
   */
  const submitAnswer = useCallback(() => {
    if (!question || answer === null) return;
    // A29: the "Polished · Undo" note is about a draft that has just left, so
    // it leaves with it. No request can still be out: the slot holds a spinner
    // while one is, and this runs from the slot.
    dropPolish();
    const requestId = question.request_id;
    const value = text;
    setDraft('');
    setErrors([]);
    onAnswer(requestId, answer)
      .then(() => clearAnswer(requestId))
      // The page reports the failure; a newer draft wins, as it does for a send.
      .catch(() => {
        if (textRef.current.length === 0) setDraft(value);
      });
  }, [question, answer, text, onAnswer, clearAnswer, setDraft, dropPolish]);

  /**
   * A12: the field is cleared and the message is put in the timeline in this
   * tick, before the request leaves. Only a refusal the gateway is certain
   * about comes back, and it hands the draft back if nothing was typed since.
   */
  const submit = useCallback(
    (mode: SendMode, source?: string) => {
      const value = (source ?? text).trim();
      if (disabled || (value.length === 0 && attachments.length === 0)) return;
      // A29: the "Polished · Undo" note belongs to the draft and leaves with
      // it. A request still out is not a case here: neither Send nor the ⋯
      // menu, the two ways into this, is drawn while one is.
      dropPolish();
      if (textTooLong(value)) {
        setErrors([strings.composer.textTooLong]);
        return;
      }
      // A43: a queued message being edited goes back into the line, even
      // behind a steering agent, unless the ⋯ menu interrupts with it.
      if (editing) {
        putBack(value, attachments, mode === 'interrupt' ? 'interrupt' : 'queue');
        return;
      }
      // A27: a first word the session offers is a command, not a message. The
      // device refuses one mid-turn with `conflict`, so the composer says so
      // itself rather than spending a round trip on a refusal it can predict.
      const command = commandable ? matchCommand(commands, value) : null;
      if (command && onRunCommand) {
        if (running) {
          setErrors([strings.commands.whileRunning]);
          return;
        }
        setDraft('');
        setErrors([]);
        onRunCommand(command.command.name, command.argument).catch((err: unknown) => {
          // A40: a command the device has to type reaches a busy terminal
          // sometimes, and only the device can say so.
          setErrors([refusalText(err, strings.commands.failed)]);
          if (textRef.current.length === 0) setDraft(value);
        });
        return;
      }
      const files = attachments;
      setDraft('');
      useDrafts.getState().clear(key);
      setErrors([]);
      onSend(value, files, mode).catch((err: unknown) => {
        setErrors([errorText(err, strings.composer.sendFailed)]);
        // A newer draft wins: a refusal only ever refills a field left empty.
        if (textRef.current.length === 0) setDraft(value);
        useDrafts.getState().restoreAttachments(key, files);
      });
    },
    [
      text,
      attachments,
      disabled,
      onSend,
      setDraft,
      key,
      commandable,
      commands,
      onRunCommand,
      running,
      dropPolish,
      editing,
      putBack,
    ],
  );

  /**
   * A29: the words a dictation just left go to the gateway's model with the
   * conversation the page already shows, and come back said cleanly. Only the
   * dictated span is ever replaced, and only while the field still holds it.
   */
  const startPolish = useCallback(
    (span: PolishSpan) => {
      if (!polishReady || !canPolish(span.dictated)) return;
      polishRun.current += 1;
      const run = polishRun.current;
      setPolish({ phase: 'polishing' });
      const body = polishRequest(
        span,
        { model: polishModel, strength: polishStrength },
        polishContext?.() ?? [],
      );
      api
        .polish(body)
        .then((result) => {
          if (run !== polishRun.current) return;
          const next = applyPolished(textRef.current, span, result.text);
          if (next === null) {
            setPolish(POLISH_IDLE);
            return;
          }
          setDraft(next);
          setPolish({ phase: 'polished', span, text: result.text.trim() });
        })
        .catch(() => {
          if (run !== polishRun.current) return;
          setPolish({ phase: 'failed' });
        });
    },
    [polishReady, polishModel, polishStrength, polishContext, setDraft],
  );

  /** Put the dictated words back, and take the note away with them. */
  const undoPolish = () => {
    if (polish.phase !== 'polished') return;
    const back = undoPolished(textRef.current, polish.span, polish.text);
    if (back !== null) setDraft(back);
    dropPolish();
  };

  // The one line about a failed polish says what happened and then goes away;
  // the words it is about are in the field, where they always were.
  useEffect(() => {
    if (polish.phase !== 'failed') return;
    const timer = window.setTimeout(() => setPolish(POLISH_IDLE), POLISH_FAILED_MS);
    return () => window.clearTimeout(timer);
  }, [polish.phase]);

  /**
   * Dictation writes into this field and nothing else: no utterance is sent by
   * the act of stopping the recording, and a failure keeps the words it did
   * recognise rather than dropping them.
   */
  const voice = useVoice({
    enabled: sttEnabled && !disabled,
    onTranscript: (transcript, isFinal) => {
      const run = dictation.current;
      if (!run || textRef.current !== run.applied) return;
      const next = mergeDraft(run.base, transcript);
      // Every transcript that changes the field leaves the end of the words in
      // view — while listening, and through the finishing spinner. A transcript
      // that writes nothing new redraws nothing, so it asks for nothing.
      if (next !== run.applied) followTail.current = true;
      run.applied = next;
      if (isFinal) dictation.current = null;
      setDraft(next);
      // A29: polishing starts on the last transcript of a dictation, once the
      // words are in the field and the field is the person's again.
      if (isFinal) startPolish({ base: run.base, dictated: transcript });
    },
  });

  const voiceBusy =
    voice.state === 'starting' || voice.state === 'listening' || voice.state === 'finishing';
  // A43: an edited message on its way back into the line. The field keeps its
  // words until the gateway answers, and holds still while it does.
  const returning = editing?.sending === true;
  /**
   * What the one primary slot of the control row holds. Everything that draws
   * or gates that slot — the row itself, the Enter key, the menu beside Send —
   * reads this and nothing else. A put-back still out is the app's move, like
   * a polish still out (A43).
   */
  const voiceSlot = primarySlot(voice.state, polish.phase);
  const slot = returning && voiceSlot === 'send' ? 'working' : voiceSlot;

  const startVoice = () => {
    dropPolish();
    dictation.current = { base: textRef.current, applied: textRef.current };
    voice.start();
  };

  /**
   * Reaching for the field takes it back: dictation stops and the words it did
   * recognise stay. A keystroke is one way to reach for it; a pointer down on
   * the field is the other, which is how a person reads back what was just
   * said — the field stops following the words the moment it is touched. The
   * field's own programmatic focus, taking a command row for instance, is not
   * a pointer and ends nothing.
   */
  const takeFieldBack = () => {
    if (!voiceBusy) return;
    dictation.current = null;
    voice.cancel();
  };

  /** A43: Cancel ends what the field was doing with the edit, then puts it back. */
  const cancelQueuedEdit = () => {
    takeFieldBack();
    dropPolish();
    cancelEdit();
  };

  /**
   * The field grows with its content up to eight lines and scrolls inside
   * after that. A write that asked to follow its tail is then shown from its
   * end, with no animation — the scroll position is set, never animated.
   */
  useEffect(() => {
    const el = textarea.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
    if (!followTail.current) return;
    followTail.current = false;
    el.scrollTop = el.scrollHeight;
  }, [text]);

  // PROTOCOL-FROZEN §5: `auto` is "send now if idle; if running, steer or queue".
  // Forcing `queue` here would make the `steer` capability unreachable. A43: a
  // queued message being edited is the exception — it goes back into the line,
  // so it steers nothing and the button says Queue while a turn runs.
  const primaryMode: SendMode = 'auto';
  const steers = canSteer && editing === null;
  const primaryLabel = answering
    ? strings.composer.answer
    : running
      ? steers
        ? strings.composer.send
        : strings.composer.queue
      : strings.composer.send;
  /**
   * Enter is Send, so it waits with Send: while the slot is a spinner there is
   * nothing to press, and a keystroke that sent anyway would be the button in
   * another guise (`docs/DESIGN.md` § "The composer").
   */
  const primarySubmit = () => {
    if (slot !== 'send') return;
    if (answering) submitAnswer();
    else submit(primaryMode);
  };
  const primaryDisabled = answering
    ? answer === null
    : disabled || (text.trim().length === 0 && attachments.length === 0);

  /**
   * A27: while the panel is open the arrows, Tab, Enter and Esc belong to it.
   * Enter takes the highlighted row; when the field already holds exactly what
   * taking it would write, Enter runs the command instead — the terminal's
   * second Enter. Returns true when the key was the panel's.
   */
  const onCommandKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!menuOpen) return false;
    const at = Math.min(highlight, rows.length - 1);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlight((at + 1) % rows.length);
      return true;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlight((at + rows.length - 1) % rows.length);
      return true;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      setMenuClosed(true);
      return true;
    }
    if (e.key === 'Tab' && !e.shiftKey && highlighted) {
      e.preventDefault();
      takeCommand(highlighted);
      return true;
    }
    if (e.key === 'Enter' && !e.shiftKey && !ime.ownsEnter(e)) {
      e.preventDefault();
      if (highlighted && completionFor(highlighted) !== text) takeCommand(highlighted);
      else ime.sendUnlessComposing(primarySubmit);
      return true;
    }
    return false;
  };

  // A10 §8: only a `terminal` session can be missing its attachment.
  const hint = terminalControlled ? attachHint(agent) : null;

  // `docs/DESIGN.md` § "The composer": the field only says who holds the
  // session. Taking it over belongs to the status line, which has the button.
  const placeholder = terminalControlled
    ? strings.composer.placeholderTerminal
    : !deviceOnline
      ? strings.composer.placeholderOffline
      : answering
        ? strings.composer.placeholderAnswer
        : running
          ? steers
            ? strings.composer.placeholderSteer
            : strings.composer.placeholderQueued
          : strings.composer.placeholder;

  return (
    <div className="composer-wrap">
      {attachments.length > 0 ? (
        <ul className="attachment-list">
          {attachments.map((file, index) => (
            <li key={`${file.name}-${index}`}>
              <Paperclip size={12} aria-hidden />
              <span className="mono">{file.name}</span>
              <span className="hint">{bytes(file.size)}</span>
              <button
                type="button"
                className="icon-btn"
                aria-label={strings.common.remove}
                onClick={() => useDrafts.getState().removeAttachment(key, index)}
              >
                <X size={13} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {errors.length > 0 ? (
        <div className="composer-errors" role="alert">
          {errors.map((message) => (
            <p key={message}>{message}</p>
          ))}
        </div>
      ) : null}

      {voice.state === 'error' && voice.error ? (
        <div className="composer-errors voice-error" role="alert">
          <p>{voice.error}</p>
          <button type="button" className="link-btn" onClick={voice.dismissError}>
            {strings.common.dismiss}
          </button>
        </div>
      ) : null}

      {terminalControlled ? (
        <div className="takeover-bar">
          <span className="takeover-text">
            {strings.status.terminalControlled}
            {hint ? <span className="takeover-hint">{hint}</span> : null}
          </span>
          {canTakeover ? (
            <button type="button" className="btn small" onClick={onTakeover}>
              {strings.chat.takeOver}
            </button>
          ) : null}
        </div>
      ) : null}

      {voiceBusy || polish.phase === 'polishing' ? (
        <p className="voice-status hint">
          {voiceBusy
            ? voice.state === 'starting'
              ? strings.voice.connecting
              : voice.state === 'finishing'
                ? strings.voice.finishing
                : strings.voice.transcribing
            : strings.voice.polishing}
        </p>
      ) : null}

      {editing ? <EditingStrip sending={returning} onCancel={cancelQueuedEdit} /> : null}

      <div className="composer-field">
        {menuOpen ? (
          <CommandMenu
            rows={rows}
            highlight={Math.min(highlight, rows.length - 1)}
            listId={menuId}
            optionId={optionId}
            running={running}
            onHighlight={setHighlight}
            onTake={takeCommand}
          />
        ) : commandMatch ? (
          <CommandHint command={commandMatch.command} />
        ) : null}

        <div className={cx('composer', disabled && 'disabled', voiceBusy && 'listening')}>
          <textarea
            ref={textarea}
            className="composer-input"
            rows={1}
            value={text}
            placeholder={placeholder}
            disabled={disabled}
            readOnly={returning}
            aria-label={strings.composer.placeholder}
            aria-expanded={menuOpen}
            aria-controls={menuOpen ? menuId : undefined}
            aria-activedescendant={
              menuOpen ? optionId(Math.min(highlight, rows.length - 1)) : undefined
            }
            onCompositionStart={ime.onCompositionStart}
            onCompositionEnd={ime.onCompositionEnd}
            onPointerDown={takeFieldBack}
            onChange={(e) => {
              takeFieldBack();
              // A29: an edit is the person taking the words back; the note goes,
              // and an answer still in flight is no longer wanted.
              dropPolish();
              const next = e.target.value;
              // A27: the list is asked for again on the keystroke that opens the
              // panel, so it is current the moment it is on screen.
              if (
                commandable &&
                commandQuery(next) !== null &&
                commandQuery(textRef.current) === null
              ) {
                onCommandsNeeded?.();
              }
              setDraft(next);
            }}
            onPaste={(e) => {
              const files = showAttach && !returning ? [...e.clipboardData.files] : [];
              if (files.length > 0) {
                e.preventDefault();
                void attach(files);
              }
            }}
            onKeyDown={(e) => {
              if (onCommandKey(e)) return;
              if (e.key !== 'Enter' || e.shiftKey || ime.ownsEnter(e)) return;
              e.preventDefault();
              ime.sendUnlessComposing(primarySubmit);
            }}
          />
          {voiceBusy ? (
            <VoiceControls voice={voice} slot={slot} onDone={voice.done} />
          ) : (
            <div className="composer-buttons">
            {showAttach ? (
              <>
                <input
                  ref={fileInput}
                  type="file"
                  multiple
                  className="sr-only"
                  aria-label={strings.composer.attach}
                  onChange={(e) => {
                    void attach([...(e.target.files ?? [])]);
                    e.target.value = '';
                  }}
                />
                <button
                  type="button"
                  className="icon-btn"
                  aria-label={strings.composer.attach}
                  disabled={disabled}
                  onClick={() => fileInput.current?.click()}
                >
                  <Paperclip size={16} />
                </button>
              </>
            ) : null}
            {sttEnabled ? (
              <button
                type="button"
                className="icon-btn"
                aria-label={strings.composer.micStart}
                disabled={disabled}
                onClick={startVoice}
              >
                <Mic size={16} />
              </button>
            ) : null}
            {/* The menu's only item is a send, so it goes with Send itself. */}
            {running && canInterrupt && !answering && slot === 'send' ? (
              <Popover
                align="end"
                side="top"
                chevron={false}
                ariaLabel={strings.composer.sendOptions}
                triggerClassName="send-alt"
                label={<span aria-hidden>⋯</span>}
              >
                {(close) => (
                  <ul className="menu">
                    <li>
                      <button
                        type="button"
                        className="menu-item"
                        onClick={() => {
                          close();
                          submit('interrupt');
                        }}
                      >
                        <span className="menu-label">{strings.composer.interruptAndSend}</span>
                      </button>
                    </li>
                  </ul>
                )}
              </Popover>
            ) : null}
            {slot === 'send' ? (
              <button
                type="button"
                className="btn primary small send-btn"
                disabled={primaryDisabled}
                onClick={primarySubmit}
              >
                {running ? primaryLabel : <ArrowUp size={15} aria-hidden />}
                {running ? null : <span className="sr-only">{strings.composer.send}</span>}
              </button>
            ) : (
              // Dictation is over and the model has the words, or an edited
              // message is on its way back into the line (A43): the slot waits
              // where Send was, and takes no click while it does.
              <WorkingPill label={returning ? strings.chat.sending : strings.voice.polishing} />
            )}
            </div>
          )}
        </div>
      </div>

      {polish.phase === 'polished' ? (
        <p className="polish-note hint">
          <span>{strings.voice.polished}</span>
          <span aria-hidden>·</span>
          <button type="button" className="link-btn" onClick={undoPolish}>
            {strings.voice.undo}
          </button>
        </p>
      ) : polish.phase === 'failed' ? (
        <p className="polish-note hint" role="status">
          {strings.voice.polishFailed}
        </p>
      ) : null}

      <ComposerBottomRow agent={agent} session={session} canSet={canSet} onSetOption={onSetOption}>
        {/*
          A43: the queue is one chip, the first of the row (A44). A message can
          be taken back into a field that can send and holds no other one.
        */}
        <UpNext
          queue={queue}
          canEdit={onTakeQueued !== undefined && !disabled && editing === null}
          onRemove={onRemoveQueued}
          onEdit={(item) => void editQueued(item)}
        />
      </ComposerBottomRow>
    </div>
  );

  /**
   * Two attach operations can run at once — a paste, then the file dialog
   * before the paste has finished encoding — and each reads the count it
   * started with. The cap is therefore enforced where the list is written, and
   * what the second call has to say is added to what the first said rather
   * than replacing it.
   */
  async function attach(files: File[]): Promise<void> {
    const drafts = useDrafts.getState();
    const result = await readAttachments(files, drafts.drafts[key]?.attachments.length ?? 0);
    const dropped = drafts.addAttachments(key, result.attachments);
    const messages =
      dropped > 0 && !result.errors.includes(strings.composer.attachTooMany(MAX_ATTACHMENTS))
        ? [...result.errors, strings.composer.attachTooMany(MAX_ATTACHMENTS)]
        : result.errors;
    if (messages.length === 0) return;
    setErrors((current) => [...new Set([...current, ...messages])]);
  }
}

/**
 * The label an agent's list gives an id, or the id itself. The agent's own ids
 * need not appear in its lists — `auto` is a real Claude permission mode the
 * device does not advertise — so an unknown one is shown as it arrived (A17).
 */
function labelOf(options: Choice[], value: string | null | undefined): string | null {
  if (!value) return null;
  return options.find((option) => option.id === value)?.label ?? value;
}

/**
 * `docs/DESIGN.md` § "The control row" (A43, A44): from the leading edge, Up
 * next, the model card, the permission mode. The web transcribes only on the
 * gateway, whose provider detects the language, so the row has no dictation
 * language to offer.
 */
function ComposerBottomRow({
  agent,
  session,
  canSet,
  onSetOption,
  children,
}: {
  agent: AgentInfo | null;
  session: Session;
  /**
   * A10/A11/A17/A40: whether this one setting is the device's to change. False
   * for every setting of a `terminal` session, and for the ones a shared
   * session's attachment does not carry. What the device cannot change it
   * shows: the card and the picker give way to what the transcript said, drawn
   * as chips that open nothing.
   */
  canSet: (key: SharedSettingKey) => boolean;
  onSetOption: (patch: SessionOptions) => void;
  /** A43: what leads the row — the Up next chip. */
  children?: ReactNode;
}) {
  const models = agent?.models ?? [];
  const modes = agent?.permission_modes ?? [];
  const efforts = agent?.efforts ?? [];
  const speeds = agent?.speeds ?? [];

  const modelText = labelOf(models, session.model);
  const effortText = labelOf(efforts, session.effort);
  const speedText = labelOf(speeds, session.speed);
  // "Opus 4.6 High": what runs, and how hard, in one line of the composer row.
  const cardText = [modelText, effortText].filter((part) => part !== null).join(' ');
  const hasCard = models.length > 0 || efforts.length > 0 || speeds.length > 0;
  // A40: the card is the one control for three settings, so it opens as soon
  // as one of them is the device's. The rows inside it answer for themselves.
  const cardLive = canSet('model') || canSet('effort') || canSet('speed');

  return (
    <div className="composer-bottom">
      {children}
      {cardLive ? (
        hasCard ? (
          <Popover
            side="top"
            align="start"
            chevron={false}
            ariaLabel={strings.composer.modelCard}
            triggerClassName="model-card-chip"
            label={
              <SizedBox
                className="model-card-chip-box"
                alternatives={labelPairs(models, efforts)}
                alternative={(pair) => <ChipLabel pair={pair} glyph={speeds.length > 0} />}
              >
                <ChipLabel
                  pair={{ model: modelText, effort: effortText }}
                  glyph={speedText !== null}
                  fallback={agentLabel(session.agent)}
                />
              </SizedBox>
            }
          >
            {() => (
              <ModelCard
                session={session}
                models={models}
                efforts={efforts}
                speeds={speeds}
                canSet={canSet}
                onSetOption={onSetOption}
              />
            )}
          </Popover>
        ) : null
      ) : (
        <TerminalSetting
          name={strings.composer.modelCard}
          text={cardText}
          speed={speedText}
          glyph={speedText !== null}
        />
      )}
      {canSet('permission_mode') ? (
        modes.length > 0 ? (
          <Menu
            side="top"
            ariaLabel={strings.composer.permissionMode}
            value={session.permission_mode}
            options={modes.map((m) => ({ id: m.id, label: m.label }))}
            onSelect={(permission_mode) => onSetOption({ permission_mode })}
            label={labelOf(modes, session.permission_mode) ?? strings.composer.permissionMode}
          />
        ) : null
      ) : (
        <TerminalSetting
          name={strings.composer.permissionMode}
          text={labelOf(modes, session.permission_mode) ?? ''}
          speed={null}
          glyph={false}
        />
      )}
    </div>
  );
}

/**
 * A21 — the card the model chip opens: the speed tier and the model on one row,
 * the effort slider under it. It stays open until it is dismissed, so several
 * changes can be made in one visit, and the model list takes the card over
 * rather than stacking a second popover on top of it.
 */
function ModelCard({
  session,
  models,
  efforts,
  speeds,
  canSet,
  onSetOption,
}: {
  session: Session;
  models: Choice[];
  efforts: Choice[];
  speeds: Choice[];
  /** A40: a row the device cannot change is not drawn as a control. */
  canSet: (key: SharedSettingKey) => boolean;
  onSetOption: (patch: SessionOptions) => void;
}) {
  const [picking, setPicking] = useState(false);
  // The stop the thumb is on, which the word beside the model name reads: the
  // card says what was chosen the moment it is chosen, not when the device
  // echoes it back. -1 while the session's effort is none of the agent's.
  const stop = efforts.findIndex((effort) => effort.id === session.effort);
  const [index, setIndex] = useState(stop);
  const [drawnStop, setDrawnStop] = useState(stop);
  const modelText = labelOf(models, session.model) ?? agentLabel(session.agent);

  // A change from anywhere else — a `/model` in the terminal, another tab —
  // wins over the position the thumb was left in.
  if (drawnStop !== stop) {
    setDrawnStop(stop);
    setIndex(stop);
  }

  if (picking) {
    return (
      <ul className="menu" role="listbox" aria-label={strings.composer.model}>
        {models.map((model) => (
          <li key={model.id}>
            <button
              type="button"
              role="option"
              aria-selected={session.model === model.id}
              className={cx('menu-item', session.model === model.id && 'selected')}
              onClick={() => {
                setPicking(false);
                if (session.model !== model.id) onSetOption({ model: model.id });
              }}
            >
              <span className="menu-label">{model.label}</span>
            </button>
          </li>
        ))}
      </ul>
    );
  }

  const effortText =
    index >= 0 ? (efforts[index]?.label ?? null) : labelOf(efforts, session.effort);
  // The row is as wide as the widest model-and-effort pair the agent offers, so
  // neither the chevron nor the card's edge moves while a level is chosen.
  const nameRow = (
    <SizedBox
      className="model-card-name-box"
      alternatives={labelPairs(models, efforts)}
      alternative={(pair) => <NameLabel pair={pair} />}
    >
      <NameLabel pair={{ model: modelText, effort: effortText }} />
    </SizedBox>
  );

  return (
    <div className="model-card">
      <div className="model-card-top">
        {speeds.length > 0 && canSet('speed') ? (
          <SpeedToggle session={session} speeds={speeds} onSetOption={onSetOption} />
        ) : null}
        {models.length > 0 && canSet('model') ? (
          <button
            type="button"
            className="model-card-name"
            aria-label={strings.composer.option(strings.composer.model, modelText)}
            onClick={() => setPicking(true)}
          >
            {nameRow}
            <ChevronRight size={14} aria-hidden className="model-card-chevron" />
          </button>
        ) : (
          <span className="model-card-name static">{nameRow}</span>
        )}
      </div>
      {efforts.length > 0 && canSet('effort') ? (
        <EffortSlider
          session={session}
          efforts={efforts}
          index={index}
          onIndex={setIndex}
          onSetOption={onSetOption}
        />
      ) : null}
    </div>
  );
}

/** The model chip's contents: the lightning, then "<model> <effort>". */
function ChipLabel({
  pair,
  glyph,
  fallback = '',
}: {
  pair: LabelPair;
  glyph: boolean;
  fallback?: string;
}) {
  const text = [pair.model, pair.effort].filter((part) => part !== null).join(' ');
  return (
    <>
      {glyph ? <Zap size={12} aria-hidden className="speed-glyph" /> : null}
      <span>{text || fallback}</span>
    </>
  );
}

/**
 * The model name and, beside it, the effort word the thumb is on. Every copy is
 * drawn the same way, because the hidden ones are what give the row its width.
 */
function NameLabel({ pair }: { pair: LabelPair }) {
  return (
    <>
      <span>{pair.model}</span>
      {pair.effort === null ? null : (
        <span className="model-card-effort" data-effort-word>
          {pair.effort}
        </span>
      )}
    </>
  );
}

/**
 * One stop per effort level, in the order the agent lists them. The pill is
 * filled to the thumb and carries one dot per stop, so the number of levels can
 * be read before anything moves; nothing else is drawn — no numbers, no labels
 * under the track — because the word beside the model name is what the thumb is
 * saying. The value is only sent when the thumb is released, which is the DOM's
 * own `change` rather than React's (which fires on every step).
 *
 * The native range input stays: it is the interaction and the keyboard. It is
 * drawn transparent on top of the pill, so the arrow keys still move one stop.
 */
function EffortSlider({
  session,
  efforts,
  index,
  onIndex,
  onSetOption,
}: {
  session: Session;
  efforts: Choice[];
  /** The live stop, owned by the card so the word above can read it. */
  index: number;
  onIndex: (index: number) => void;
  onSetOption: (patch: SessionOptions) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const value = Math.max(0, index);
  const last = Math.max(1, efforts.length - 1);

  useEffect(() => {
    const el = input.current;
    if (!el) return;
    const commit = () => {
      const picked = efforts[Number(el.value)];
      if (picked && picked.id !== session.effort) onSetOption({ effort: picked.id });
    };
    el.addEventListener('change', commit);
    return () => el.removeEventListener('change', commit);
  }, [efforts, session.effort, onSetOption]);

  return (
    <div
      className="effort-track"
      style={{ '--fill': `${(value / last) * 100}%` } as CSSProperties}
    >
      {/* Inset by half a thumb, which is where the thumb's centre travels. The
          lowest stop fills nothing: its cap would only show around the thumb. */}
      <span className="effort-fill-layer" aria-hidden>
        {value > 0 ? <span className="effort-fill" /> : null}
      </span>
      <span className="effort-dots" aria-hidden>
        {efforts.map((effort, stop) => (
          <span
            key={effort.id}
            className={cx('effort-dot', value > 0 && stop <= value && 'filled')}
            style={{ left: `${(stop / last) * 100}%` }}
          />
        ))}
      </span>
      <input
        ref={input}
        type="range"
        className="effort-slider"
        min={0}
        max={efforts.length - 1}
        step={1}
        value={value}
        aria-label={strings.composer.effort}
        aria-valuetext={efforts[value]?.label ?? ''}
        onChange={(e) => onIndex(Number(e.target.value))}
      />
    </div>
  );
}

/** A21: standard → each tier the agent lists → standard, one tap at a time. */
function SpeedToggle({
  session,
  speeds,
  onSetOption,
}: {
  session: Session;
  speeds: Choice[];
  onSetOption: (patch: SessionOptions) => void;
}) {
  const tier = speeds.find((speed) => speed.id === session.speed) ?? null;
  return (
    <button
      type="button"
      className={cx('speed-toggle', tier && 'on')}
      aria-pressed={tier !== null}
      aria-label={strings.composer.speed(tier?.label ?? strings.composer.speedStandard)}
      onClick={() => {
        const next = speeds[speeds.findIndex((speed) => speed.id === session.speed) + 1] ?? null;
        onSetOption({ speed: next?.id ?? null });
      }}
    >
      <Zap size={15} aria-hidden />
    </button>
  );
}

/**
 * A17: one value a terminal chose, where its picker would be. It is the shape
 * of the trigger beside it, opens nothing, and carries the whole sentence for
 * assistive technology, because on its own "auto" says nothing about who set
 * it. A21 folds the model, the effort and the tier into the first of them, the
 * same one chip the card would open from. A value the device has not seen draws
 * no chip at all.
 */
function TerminalSetting({
  name,
  text,
  speed,
  glyph,
}: {
  name: string;
  text: string;
  speed: string | null;
  glyph: boolean;
}) {
  if (text.length === 0) return null;
  const shown = speed === null ? text : `${text} · ${speed}`;
  const sentence = strings.composer.setInTerminal(name, shown);
  return (
    <span className="composer-chip readonly" role="note" title={sentence} aria-label={sentence}>
      {glyph ? <Zap size={12} aria-hidden className="speed-glyph" /> : null}
      {text}
    </span>
  );
}
