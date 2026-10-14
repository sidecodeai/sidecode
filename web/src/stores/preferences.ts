/**
 * A35 — the account's preferences, which belong to the gateway rather than to
 * this browser: `hello` seeds them, `preferences.updated` keeps every app of the
 * account in step, and a change is written with `PATCH /api/preferences`.
 *
 * `undefined` is a gateway that predates them, which is not the same as "off":
 * the switch is disabled with a note rather than drawn as a choice the reader
 * could make (`docs/DESIGN.md` § "Paused by the usage limit").
 *
 * A41 put the Settings screen's own values in the same object. The ones this
 * app keeps are read from the settings store, which the whole app reads
 * already, so every object that arrives here is handed to it as well.
 */
import { create } from 'zustand';
import { api } from '../lib/api';
import type { Preferences } from '../protocol/types';
import { useSettings } from './settings';

interface PreferencesState {
  preferences: Preferences | undefined;
  /** What `hello` carried, which is nothing on a gateway older than A35. */
  fromHello: (preferences: Preferences | undefined) => void;
  /** A change made here or in another app of the same account. */
  apply: (preferences: Preferences) => void;
  /** Written at once and put back when the gateway refuses. */
  setResumeAfterLimit: (value: boolean) => Promise<void>;
  reset: () => void;
}

export const usePreferences = create<PreferencesState>((set, get) => ({
  preferences: undefined,

  fromHello: (preferences) => {
    set({ preferences });
    useSettings.getState().fromHello(preferences);
  },

  apply: (preferences) => {
    set({ preferences });
    useSettings.getState().fromAccount(preferences);
  },

  setResumeAfterLimit: async (value) => {
    const previous = get().preferences;
    // Nothing to write to: this gateway does not hold preferences at all.
    if (previous === undefined) return;
    set({ preferences: { ...previous, resume_after_limit: value } });
    try {
      const result = await api.setPreferences({ resume_after_limit: value });
      set({ preferences: result.preferences });
    } catch (err) {
      set({ preferences: previous });
      throw err;
    }
  },

  reset: () => {
    set({ preferences: undefined });
    // A41: and there is nothing to write them up to any more, until the next
    // account's `hello` says there is.
    useSettings.getState().fromHello(undefined);
  },
}));
