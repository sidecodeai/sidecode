/**
 * Dictations that outlive the composer they were spoken into.
 *
 * `docs/DESIGN.md` § "Dictation keeps going when you leave" — once Done is
 * tapped, the wait for the last transcript and the polish run belong to this
 * store, keyed by session key, not to the composer, which remounts with every
 * session. A dictation still listening when the composer goes away counts as
 * Done. The result lands in the open composer's draft or in `useDrafts`, merged
 * with whatever the person typed meanwhile, and is never sent: nothing here
 * has a send path. A57: a batch dictation's words are all still to come when
 * it is handed over — at a switch, or when its field is reached for — so typing
 * never waits for them, and they are appended after the typed words.
 *
 * In memory for the tab's life, and emptied on sign-out with every other store.
 */
import { create } from 'zustand';
import { api } from '../lib/api';
import type { PolishContextItem, PolishRequest } from '../protocol/types';
import type { DictationMode } from '../features/voice/dictationMode';
import { mergeDraft } from '../features/voice/draft';
import { applyPolished, canPolish, polishRequest, type PolishSpan } from '../features/voice/polish';
import { landsAside, useDrafts } from './drafts';

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

/** Why a dictation ended without all its words: the sentence the composer shows. */
export interface DictationNotice {
  message: string;
}

export interface DictationJob {
  phase: JobPhase;
  /** A57: how the dictation behind the job showed its words. */
  mode: DictationMode;
  note: PolishNote | null;
  /** The model could not polish; the transcript is in the draft as it was. */
  failed: boolean;
  /**
   * A57: what a batch dictation that ended `interrupted` says, wherever its
   * session is opened.
   */
  notice: DictationNotice | null;
  /** The result arrived while its session was not open, and has not been seen. */
  unseen: boolean;
  /**
   * A57: a batch dictation's transcript is still on its way and the person has
   * typed since: Send's slot is theirs again, and the transcript is appended
   * after their words when it comes.
   */
  typedOver: boolean;
}

/** Where a handed-over dictation reports: the transcript so far, and its end. */
export interface TranscriptSink {
  (text: string, isFinal: boolean, interrupted?: boolean, notice?: DictationNotice): void;
  /** How the run behind the sink is closed if the store is emptied before it ends. */
  bindCancel: (cancel: () => void) => void;
}

/**
 * The open composer's field, as a landing sees it: it runs the landing now, or
 * once an input method's composition there has ended.
 */
export type LandingGate = (land: () => void) => void;

export interface Handoff {
  /** The draft the dictation started from. */
  base: string;
  /** What the draft held after the last live write: base and the partial words. */
  applied: string;
  /** Null when the dictation is not polished. */
  plan: PolishPlan | null;
  /** A57: a batch dictation's words are all still to come, and typing does not wait for them. */
  mode: DictationMode;
  /** A57: the queued message the field was editing when the mic was pressed, or null. */
  editTs: number | null;
}

interface JobsState {
  jobs: Record<string, DictationJob>;
  /**
   * A composer for this session is on screen; its result is seen as it lands,
   * through its field's gate.
   */
  open: (key: string, gate?: LandingGate) => void;
  /** The composer left. A result it had already shown goes with it. */
  leave: (key: string) => void;
  /** Send the dictated span to the model; the answer lands through this store. */
  polish: (key: string, span: PolishSpan, plan: PolishPlan, mode: DictationMode) => void;
  /**
   * The field moved on: end a polish run or forget a finished one. A transcript
   * on its way is never dropped, and a batch one gives Send's slot back; the
   * notice of a dictation that lost words stays until it is forgotten (A57).
   */
  drop: (key: string) => void;
  /**
   * The person is done with what the job says — its notice dismissed, the
   * draft sent, a new dictation begun: it goes, unless words are still coming.
   */
  forget: (key: string) => void;
  /**
   * The composer lets go of a dictation midway — it is going away, or (A57) a
   * batch dictation's field was reached for: take over from Done on.
   */
  handoff: (key: string, handoff: Handoff) => TranscriptSink;
  reset: () => void;
}

/** The polish request in flight per session, so a stale answer can be told. */
const runs = new Map<string, AbortController>();
/** Sessions whose composer is on screen. Not state: nothing draws from it. */
const attached = new Set<string>();
/** The gates of the fields on screen, by session. */
const gates = new Map<string, LandingGate>();
/** Bumped by `reset`: a sink handed out before it belongs to an account that left. */
let epoch = 0;

/**
 * Which account's turn it is: sign-out moves it on, so work begun before can
 * tell that it no longer belongs to anyone.
 */
export const jobsEpoch = (): number => epoch;
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
    mode: DictationMode,
    rest: Partial<Pick<DictationJob, 'note' | 'failed' | 'notice'>> = {},
  ) =>
    put(key, {
      phase,
      mode,
      note: null,
      failed: false,
      notice: null,
      unseen: !attached.has(key),
      typedOver: false,
      ...rest,
    });

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

  const polish = (key: string, span: PolishSpan, plan: PolishPlan, mode: DictationMode) => {
    if (!canPolish(span.dictated)) {
      settle(key, 'ready', mode);
      return;
    }
    cancelRun(key);
    const controller = new AbortController();
    runs.set(key, controller);
    put(key, {
      phase: 'polishing',
      mode,
      note: null,
      failed: false,
      notice: null,
      unseen: false,
      typedOver: false,
    });
    const body = polishRequest(span, { model: plan.model, strength: plan.strength }, plan.context);
    api
      .polish(body, controller.signal)
      .then((result) => {
        if (!endRun(key, controller)) return;
        // The draft is read now, not when the request left: a person who typed
        // or sent meanwhile keeps their words, whichever composer they are in.
        const next = applyPolished(textOf(key), span, result.text);
        if (next === null) {
          settle(key, 'ready', mode);
          return;
        }
        useDrafts.getState().setText(key, next);
        settle(key, 'ready', mode, { note: { span, text: result.text.trim() } });
      })
      .catch(() => {
        if (!endRun(key, controller)) return;
        settle(key, 'ready', mode, { failed: true });
      });
  };

  return {
    jobs: {},

    open: (key, gate) => {
      attached.add(key);
      if (gate) gates.set(key, gate);
      const job = get().jobs[key];
      if (job?.unseen) put(key, { ...job, unseen: false });
    },

    leave: (key) => {
      attached.delete(key);
      gates.delete(key);
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
      if (!job) return;
      if (job.phase === 'finishing') {
        if (job.mode === 'batch' && !job.typedOver) put(key, { ...job, typedOver: true });
        return;
      }
      if (job.notice) return;
      cancelRun(key);
      remove(key);
    },

    forget: (key) => {
      const job = get().jobs[key];
      if (!job || job.phase === 'finishing') return;
      cancelRun(key);
      remove(key);
    },

    handoff: (key, { base, applied, plan, mode, editTs }) => {
      // The words spoken so far are not final, so they are taken out of the
      // draft only while it still holds exactly them; a draft the person has
      // typed into keeps everything it has. A batch dictation wrote none.
      if (textOf(key) === applied) useDrafts.getState().setText(key, base);
      put(key, {
        phase: 'finishing',
        mode,
        note: null,
        failed: false,
        notice: null,
        unseen: false,
        typedOver: false,
      });
      const born = epoch;
      let cancel: (() => void) | null = null;
      const sink = (
        text: string,
        isFinal: boolean,
        interrupted?: boolean,
        notice?: DictationNotice,
      ) => {
        if (born !== epoch || !isFinal) return;
        if (cancel) handedOver.delete(cancel);
        const land = () => {
          if (born !== epoch) return;
          const draft = useDrafts.getState().drafts[key];
          // Words for the draft set aside while a queued message is edited
          // wait there, and nothing is asked of the model about them.
          const aside = landsAside(draft, editTs);
          const current = aside ? (draft?.editing?.aside.text ?? '') : textOf(key);
          const merged = mergeDraft(current, text);
          if (merged !== current) {
            if (aside) useDrafts.getState().setAside(key, merged);
            // The plain transcript is the draft before the model is asked anything.
            else useDrafts.getState().setText(key, merged);
          }
          if (interrupted) settle(key, 'interrupted', mode, { notice: notice ?? null });
          else if (plan && !aside) polish(key, { base: current, dictated: text }, plan, mode);
          else settle(key, 'ready', mode);
        };
        // Never into a composition the open field is in the middle of.
        const gate = gates.get(key);
        if (gate) gate(land);
        else land();
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
