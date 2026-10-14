/**
 * Amendment A20: how a pending question turns into a `session.answer`.
 *
 * A question is answered where you are. The card takes options and its own
 * free-text fields; the composer takes a sentence. Both end up in the same
 * `answers` map, and these rules say how — pure, so the composer, the card and
 * the tests all read one description of the behaviour.
 */
import type { AnswerDraft } from '../../stores/answers';
import type { QuestionAnswers, QuestionEvent, QuestionSpec } from '../../protocol/types';

/** What one question already holds: the options picked, else the text typed. */
function answerFor(question: QuestionSpec, draft: AnswerDraft): string[] | string | null {
  const picked = draft.selection[question.id] ?? [];
  if (picked.length > 0) return picked;
  const typed = (draft.text[question.id] ?? '').trim();
  return typed.length > 0 ? typed : null;
}

/** The answers a card submits on its own, with nothing left unanswered. */
export function cardAnswers(event: QuestionEvent, draft: AnswerDraft): QuestionAnswers {
  const answers: QuestionAnswers = {};
  for (const question of event.questions) {
    const answer = answerFor(question, draft);
    if (answer !== null) answers[question.id] = answer;
  }
  return answers;
}

/** True once every question on the card carries an answer of its own. */
export const cardComplete = (event: QuestionEvent, draft: AnswerDraft): boolean =>
  event.questions.every((question) => answerFor(question, draft) !== null);

/**
 * The submission for a composer draft, or null when the draft has nowhere to
 * go: every question already carries a selection, or the first one without a
 * selection refuses free text. The draft is then left where it is.
 *
 * The draft answers the first question with no option selected. Text typed on
 * the card does not take that slot, so the composer's sentence replaces it
 * rather than being ignored, and the other questions keep what they hold.
 */
export function composeAnswer(
  event: QuestionEvent,
  draft: AnswerDraft,
  text: string,
): QuestionAnswers | null {
  const value = text.trim();
  if (value.length === 0) return null;
  const target = event.questions.find(
    (question) => (draft.selection[question.id] ?? []).length === 0,
  );
  if (!target || !target.allow_text) return null;
  return { ...cardAnswers(event, draft), [target.id]: value };
}
