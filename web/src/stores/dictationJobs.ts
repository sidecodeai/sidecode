/**
 * Dictations that outlive the composer they were spoken into.
 *
 * `docs/DESIGN.md` § "Dictation keeps going when you leave" — once Done is
 * tapped, the wait for the last transcript and the polish run belong to this
 * store, keyed by session key, not to the composer, which remounts with every
 * session. A dictation still listening when the composer goes away counts as
 * Done. The result lands in the open composer's draft or in `useDrafts`, merged
 * with whatever the person typed meanwhile, and is never sent: nothing here
 * has a send path.
 *
 * In memory for the tab's life, and emptied on sign-out with every other store.
 */
import { create } from 'zustand';
import { api } from '../lib/api';
import type { PolishContextItem, PolishRequest } from '../protocol/types';
import { mergeDraft } from '../features/voice/draft';
import { applyPolished, canPolish, polishRequest, type PolishSpan } from '../features/voice/polish';
import { useDrafts } from './drafts';

/** `finishing` waits for the last transcript, `interrupted` never got one. */
export type JobPhase = 'finishing' | 'polishing' | 'ready' | 'interrupted';

/** What the model is asked, frozen when the dictation ends. */
export interface PolishPlan {
  model: string;
  strength: PolishRequest['strength'];
  context: PolishContextItem[];
}

/** An answer the draft took, which Undo can put back. */
export interface PolishNote {
  span: PolishSpan;
  text: string;
}

export interface DictationJob {
  phase: JobPhase;
  note: PolishNote | null;
  /** The model could not polish; the transcript is in the draft as it was. */
  failed: boolean;
  /** The result arrived while its session was not open, and has not been seen. */
  unseen: boolean;
}

/** Where a handed-over dictation reports: the transcript so far, and its end. */
export interface TranscriptSink {
  (text: string, isFinal: boolean, interrupted?: boolean): void;
  /** How the run behind the sink is closed if the store is emptied before it ends. */
  bindCancel: (cancel: () => void) => void;
}

export interface Handoff {
  /** The draft the dictation started from. */
  base: string;
  /** What the draft held after the last live write: base and the partial words. */
  applied: string;
  /** Null when the dictation is not polished. */
  plan: PolishPlan | null;
}

interface JobsState {
  jobs: Record<string, DictationJob>;
  /** A composer for this session is on screen; its result is seen as it lands. */
  open: (key: string) => void;
  /** The composer left. A result it had already shown goes with it. */
  leave: (key: string) => void;
  /** Send the dictated span to the model; the answer lands through this store. */
  polish: (key: string, span: PolishSpan, plan: PolishPlan) => void;
  /** The field moved on: end a polish run or forget a finished one. */
  drop: (key: string) => void;
  /** The composer is going away mid-dictation: take over from Done on. */
  handoff: (key: string, handoff: Handoff) => TranscriptSink;
  reset: () => void;
}

/** The polish request in flight per session, so a stale answer can be told. */
const runs = new Map<string, AbortController>();
/** Sessions whose composer is on screen. Not state: nothing draws from it. */
const attached = new Set<string>();
/** Bumped by `reset`: a sink handed out before it belongs to an account that left. */
let epoch = 0;
/** Closers of the handed-over runs still going, for `reset`. */
const handedOver = new Set<() => void>();

const textOf = (key: string): string => useDrafts.getState().drafts[key]?.text ?? '';

export const useDictationJobs = create<JobsState>((set, get) => {
  const put = (key: string, job: DictationJob) =>
    set((state) => ({ jobs: { ...state.jobs, [key]: job } }));

  const remove = (key: string) =>
    set((state) => {
      if (!state.jobs[key]) return {};
      const jobs = { ...state.jobs };
      delete jobs[key];
      return { jobs };
    });

  const settle = (
    key: string,
    phase: 'ready' | 'interrupted',
    rest: Partial<Pick<DictationJob, 'note' | 'failed'>> = {},
  ) => put(key, { phase, note: null, failed: false, unseen: !attached.has(key), ...rest });

  const endRun = (key: string, controller: AbortController): boolean => {
    if (runs.get(key) !== controller) return false;
    runs.delete(key);
    return true;
  };

  const cancelRun = (key: string) => {
    const controller = runs.get(key);
    runs.delete(key);
    controller?.abort();
  };

  const polish = (key: string, span: PolishSpan, plan: PolishPlan) => {
    if (!canPolish(span.dictated)) {
      settle(key, 'ready');
      return;
    }
    cancelRun(key);
    const controller = new AbortController();
    runs.set(key, controller);
    put(key, { phase: 'polishing', note: null, failed: false, unseen: false });
    const body = polishRequest(span, { model: plan.model, strength: plan.strength }, plan.context);
    api
      .polish(body, controller.signal)
      .then((result) => {
        if (!endRun(key, controller)) return;
        // The draft is read now, not when the request left: a person who typed
        // or sent meanwhile keeps their words, whichever composer they are in.
        const next = applyPolished(textOf(key), span, result.text);
        if (next === null) {
          settle(key, 'ready');
          return;
        }
        useDrafts.getState().setText(key, next);
        settle(key, 'ready', { note: { span, text: result.text.trim() } });
      })
      .catch(() => {
        if (!endRun(key, controller)) return;
        settle(key, 'ready', { failed: true });
      });
  };

  return {
    jobs: {},

    open: (key) => {
      attached.add(key);
      const job = get().jobs[key];
      if (job?.unseen) put(key, { ...job, unseen: false });
    },

    leave: (key) => {
      attached.delete(key);
      // A remount in the same tick (a development double mount) keeps the job.
      window.setTimeout(() => {
        const job = get().jobs[key];
        if (attached.has(key) || !job) return;
        if (job.phase === 'ready' || job.phase === 'interrupted') {
          if (!job.unseen) remove(key);
        }
      }, 0);
    },

    polish,

    drop: (key) => {
      const job = get().jobs[key];
      if (!job || job.phase === 'finishing') return;
      cancelRun(key);
      remove(key);
    },

    handoff: (key, { base, applied, plan }) => {
      // The words spoken so far are not final, so they are taken out of the
      // draft only while it still holds exactly them; a draft the person has
      // typed into keeps everything it has.
      if (textOf(key) === applied) useDrafts.getState().setText(key, base);
      put(key, { phase: 'finishing', note: null, failed: false, unseen: false });
      const born = epoch;
      let cancel: (() => void) | null = null;
      const sink = (text: string, isFinal: boolean, interrupted?: boolean) => {
        if (born !== epoch || !isFinal) return;
        if (cancel) handedOver.delete(cancel);
        const current = textOf(key);
        const merged = mergeDraft(current, text);
        // The plain transcript is the draft before the model is asked anything.
        if (merged !== current) useDrafts.getState().setText(key, merged);
        if (interrupted) settle(key, 'interrupted');
        else if (plan) polish(key, { base: current, dictated: text }, plan);
        else settle(key, 'ready');
      };
      return Object.assign(sink, {
        bindCancel: (close: () => void) => {
          cancel = close;
          handedOver.add(close);
        },
      });
    },

    reset: () => {
      epoch += 1;
      for (const close of handedOver) close();
      handedOver.clear();
      for (const controller of runs.values()) controller.abort();
      runs.clear();
      set({ jobs: {} });
    },
  };
});

export const jobOf =
  (key: string) =>
  (state: JobsState): DictationJob | undefined =>
    state.jobs[key];
