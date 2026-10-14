import { useEffect, useState } from 'react';
import { cx } from '../../../lib/cx';
import { strings } from '../../../strings';
import { draftOf, useAnswers } from '../../../stores/answers';
import { cardAnswers, cardComplete } from '../answering';
import type { QuestionAnswers, QuestionEvent } from '../../../protocol/types';

interface Props {
  event: QuestionEvent;
  onAnswer: (requestId: string, answers: QuestionAnswers) => Promise<void>;
}

/**
 * A question block. It is answerable wherever the composer is — a shared
 * session included, where the device feeds the answer to the CLI's own dialog
 * (A20) — and its working state lives in the answers store, so the composer
 * submits exactly what is on screen here.
 */
export function QuestionCard({ event, onAnswer }: Props) {
  const draft = useAnswers(draftOf(event.request_id));
  const toggle = useAnswers((s) => s.toggle);
  const setText = useAnswers((s) => s.setText);
  const clear = useAnswers((s) => s.clear);
  const [busy, setBusy] = useState(false);
  const pending = event.status === 'pending';

  /**
   * A question that stops being pending takes its working state with it,
   * however it ended: answered here, answered in the terminal, or expired. A
   * successful submit is only one of those, and a `secret` field said the value
   * is not stored — so nothing may be left holding one.
   */
  useEffect(() => {
    if (!pending) clear(event.request_id);
  }, [pending, event.request_id, clear]);

  // A failed answer keeps everything that was filled in: the page's banner says
  // what went wrong and the card is ready to be submitted again.
  async function submit(): Promise<void> {
    setBusy(true);
    try {
      await onAnswer(event.request_id, cardAnswers(event, draft));
      clear(event.request_id);
    } catch {
      /* reported by the page */
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={cx('question', !pending && 'resolved')}>
      {event.questions.map((question) => (
        <div className="question-item" key={question.id}>
          <p className="question-prompt">{question.prompt}</p>
          {question.options.length > 0 ? (
            <div className="question-options">
              {question.options.map((option) => {
                const active = (draft.selection[question.id] ?? []).includes(option.id);
                const answered = answerIncludes(event.answers?.[question.id], option.id);
                return (
                  <button
                    key={option.id}
                    type="button"
                    className={cx('question-option', (pending ? active : answered) && 'selected')}
                    disabled={!pending}
                    aria-pressed={pending ? active : answered}
                    onClick={() => toggle(event.request_id, question, option.id)}
                  >
                    <span>{option.label}</span>
                    {option.description ? (
                      <span className="hint">{option.description}</span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : null}
          {question.allow_text ? (
            <input
              className="field"
              type={question.secret ? 'password' : 'text'}
              disabled={!pending}
              placeholder={
                question.secret ? strings.chat.secretPlaceholder : strings.chat.freeTextPlaceholder
              }
              value={
                pending
                  ? (draft.text[question.id] ?? '')
                  : freeText(event.answers?.[question.id], question.secret)
              }
              aria-label={question.prompt}
              onChange={(e) => setText(event.request_id, question.id, e.target.value)}
            />
          ) : null}
        </div>
      ))}
      {pending ? (
        <div className="approval-actions">
          <button
            type="button"
            className="btn small primary"
            disabled={!cardComplete(event, draft) || busy}
            onClick={() => void submit()}
          >
            {strings.chat.submitAnswer}
          </button>
        </div>
      ) : (
        <p className="approval-result hint">{resultText(event)}</p>
      )}
    </section>
  );
}

/**
 * A20: a resolved question says who answered it, the way an approval answered
 * in the terminal does. A device that does not report `by` says only that the
 * question was answered.
 */
function resultText(event: QuestionEvent): string {
  if (event.status === 'expired') return strings.chat.questionExpired;
  if (event.by === 'terminal') return strings.chat.questionAnsweredInTerminal;
  return strings.chat.questionResolved;
}

function answerIncludes(answer: string[] | string | undefined, optionId: string): boolean {
  if (Array.isArray(answer)) return answer.includes(optionId);
  return false;
}

/**
 * What a resolved card shows in its free-text field: the answer that was given,
 * whether it was typed here or in the composer (A20). A secret answer is never
 * echoed back — the card said it is not stored, and neither is it redrawn.
 */
function freeText(answer: string[] | string | undefined, secret: boolean | undefined): string {
  if (secret || typeof answer !== 'string') return '';
  return answer;
}
