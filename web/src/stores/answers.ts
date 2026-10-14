/**
 * What has been filled in on a pending question card, keyed by `request_id`.
 *
 * Amendment A20 gives a question two places to answer it: the card's own
 * options and free-text fields, and the composer, whose draft becomes the free
 * text of the first question still without a selection. Both read this store,
 * so there is one source of truth for what will be submitted, wherever the
 * submit came from.
 */
import { create } from 'zustand';
import type { QuestionSpec } from '../protocol/types';

/** One card's working state: options picked, and text typed on the card. */
export interface AnswerDraft {
  selection: Record<string, string[]>;
  text: Record<string, string>;
}

export const emptyDraft: AnswerDraft = { selection: {}, text: {} };

interface AnswersState {
  drafts: Record<string, AnswerDraft>;
  /** Pick an option, or unpick it on a `multi` question. */
  toggle: (requestId: string, question: QuestionSpec, optionId: string) => void;
  setText: (requestId: string, questionId: string, value: string) => void;
  /** Forget a card once it has been answered; nothing else may be resubmitted. */
  clear: (requestId: string) => void;
  /**
   * Sign-out: a question's working state includes fields whose own placeholder
   * says the value is not stored, so none of it outlives the account
   * (`signOut`).
   */
  reset: () => void;
}

export const useAnswers = create<AnswersState>((set) => ({
  drafts: {},

  toggle: (requestId, question, optionId) =>
    set((state) => {
      const draft = state.drafts[requestId] ?? emptyDraft;
      const current = draft.selection[question.id] ?? [];
      const next = !question.multi
        ? [optionId]
        : current.includes(optionId)
          ? current.filter((id) => id !== optionId)
          : [...current, optionId];
      return {
        drafts: {
          ...state.drafts,
          [requestId]: { ...draft, selection: { ...draft.selection, [question.id]: next } },
        },
      };
    }),

  setText: (requestId, questionId, value) =>
    set((state) => {
      const draft = state.drafts[requestId] ?? emptyDraft;
      return {
        drafts: {
          ...state.drafts,
          [requestId]: { ...draft, text: { ...draft.text, [questionId]: value } },
        },
      };
    }),

  clear: (requestId) =>
    set((state) => {
      if (!state.drafts[requestId]) return {};
      const drafts = { ...state.drafts };
      delete drafts[requestId];
      return { drafts };
    }),

  reset: () => set({ drafts: {} }),
}));

export const draftOf = (requestId: string) => (state: AnswersState): AnswerDraft =>
  state.drafts[requestId] ?? emptyDraft;
