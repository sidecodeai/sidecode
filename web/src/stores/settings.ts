/**
 * What Settings holds. Five of the values are the account's since A41 — the
 * interface language, polish with its model and its strength, and the
 * timeline detail — and for those this store is the cache of what the gateway
 * holds: `hello` and every `preferences.updated` frame land here, and a change
 * made here goes up at once. The rest is this browser's alone: which device
 * groups a list has folded, and which Archives are open. The account's
 * dictation language is not here at all: since A44 it is what an iPhone's own
 * recogniser listens for, and the web only ever transcribes on the gateway.
 *
 * Persisted in localStorage when available, under the signed-in account's key,
 * so the moments before `hello` read what this browser last saw rather than
 * the defaults.
 */
import { create } from 'zustand';
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware';
import type {
  InterfaceLanguage,
  PolishStrength,
  Preferences,
  TimelineDetail,
} from '../protocol/types';
import {
  changesFrom,
  readPreferences,
  unsetKeys,
  type SyncedKey,
  type SyncedSettings,
} from './preferenceFields';
import { sendPreferences, setPreferencesHeld } from './preferenceWrite';

interface SettingsState extends SyncedSettings {
  /** Device groups the user folded shut in a session list. Expanded by default. */
  collapsedDevices: string[];
  /** Devices whose Archive sub-group is open. Collapsed by default. */
  archiveExpanded: string[];
  setLanguage: (language: InterfaceLanguage) => void;
  setPolishEnabled: (enabled: boolean) => void;
  setPolishModel: (model: string) => void;
  setPolishStrength: (strength: PolishStrength) => void;
  setTimelineDetail: (detail: TimelineDetail) => void;
  toggleDeviceCollapsed: (deviceId: string) => void;
  toggleArchiveExpanded: (deviceId: string) => void;
  /**
   * A41: what `hello` carried, which is nothing at all on a gateway older than
   * A35. The fields it names win here, and the ones it leaves out are this
   * browser's to write up, once.
   */
  fromHello: (preferences: Preferences | undefined) => void;
  /** A41: a change made here or in another app of the same account. */
  fromAccount: (preferences: Preferences) => void;
}

const toggle = (list: string[], id: string): string[] =>
  list.includes(id) ? list.filter((entry) => entry !== id) : [...list, id];

/**
 * `localStorage` is missing or throws in private windows, in embedded webviews
 * and under strict cookie policies. Preferences are a convenience, so fall back
 * to memory rather than letting the app fail to boot.
 */
function browserStorage(): StateStorage | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const probe = '__rc_probe__';
    store.setItem(probe, '1');
    store.removeItem(probe);
    return store;
  } catch {
    return null;
  }
}

function memoryStorage(): StateStorage {
  const map = new Map<string, string>();
  return {
    getItem: (name) => map.get(name) ?? null,
    setItem: (name, value) => void map.set(name, value),
    removeItem: (name) => void map.delete(name),
  };
}

const storage = browserStorage() ?? memoryStorage();

/**
 * A24: what this app remembers belongs to the person signed in, not to the
 * browser. `localStorage` is already scoped to the gateway's origin, so the
 * username is all the key still needs; two people on one browser find their own
 * language, polish choices and timeline detail. Nobody is signed in on the
 * login screen, which is why the signed-out key exists at all.
 */
const SIGNED_OUT_KEY = 'rc.settings';

const keyFor = (username: string | null): string =>
  username === null ? SIGNED_OUT_KEY : `${SIGNED_OUT_KEY}.${username}`;

/** What an account that has chosen nothing yet reads. */
const defaults = (): Pick<
  SettingsState,
  | 'language'
  | 'polishEnabled'
  | 'polishModel'
  | 'polishStrength'
  | 'timelineDetail'
  | 'collapsedDevices'
  | 'archiveExpanded'
> => ({
  language: 'en',
  polishEnabled: false,
  polishModel: '',
  polishStrength: 'moderate',
  timelineDetail: 'simple',
  collapsedDevices: [],
  archiveExpanded: [],
});

export const useSettings = create<SettingsState>()(
  persist(
    (set) => ({
      ...defaults(),
      // A41: the value moves under the finger first and goes up after, so a
      // control never waits on the round trip; the reply and the frame that
      // follows it carry the same value, and change nothing here.
      setLanguage: (language) => {
        set({ language });
        writeUp(['language']);
      },
      setPolishEnabled: (polishEnabled) => {
        set({ polishEnabled });
        writeUp(['polishEnabled']);
      },
      setPolishModel: (polishModel) => {
        set({ polishModel });
        writeUp(['polishModel']);
      },
      setPolishStrength: (polishStrength) => {
        set({ polishStrength });
        writeUp(['polishStrength']);
      },
      setTimelineDetail: (timelineDetail) => {
        set({ timelineDetail });
        writeUp(['timelineDetail']);
      },
      toggleDeviceCollapsed: (deviceId) =>
        set((s) => ({ collapsedDevices: toggle(s.collapsedDevices, deviceId) })),
      toggleArchiveExpanded: (deviceId) =>
        set((s) => ({ archiveExpanded: toggle(s.archiveExpanded, deviceId) })),
      fromHello: (preferences) => {
        setPreferencesHeld(preferences !== undefined);
        if (preferences === undefined) return;
        settle(readPreferences(preferences));
        writeUp(unsetKeys(preferences));
      },
      fromAccount: (preferences) => settle(readPreferences(preferences)),
    }),
    {
      name: SIGNED_OUT_KEY,
      version: 1,
      storage: createJSONStorage(() => storage),
      // The defaults go under whatever the account stored, so switching to an
      // account that has chosen nothing does not inherit the last person's
      // choices. Without this, a rehydrate with nothing to read leaves the
      // previous account's state in place.
      merge: (persisted, current) => ({
        ...current,
        ...defaults(),
        ...(persisted as Partial<SettingsState> | undefined),
      }),
    },
  ),
);

/**
 * A41: what the account holds, applied in place. Only what differs is set, so
 * the gateway's echo of a change made here moves nothing on the screen and
 * writes nothing back.
 */
function settle(read: Partial<SyncedSettings>): void {
  const changes = changesFrom(read, useSettings.getState());
  if (Object.keys(changes).length > 0) useSettings.setState(changes);
}

/**
 * A41: send some of this app's values to the account — the one field the
 * reader just changed, or, right after `hello`, every field the account has
 * not set. What the gateway answers is what the store then holds.
 */
function writeUp(keys: SyncedKey[]): void {
  void sendPreferences(keys, useSettings.getState()).then((read) => {
    if (read) settle(read);
  });
}

/** The account the store is currently reading and writing. */
let account: string | null = null;

/**
 * Point the store at one account's preferences: writes go to that account's key
 * from here on, and the rehydrate reads back what it chose before, or the
 * defaults when it has chosen nothing.
 */
export function readSettingsFor(username: string | null): void {
  if (username === account) return;
  account = username;
  useSettings.persist.setOptions({ name: keyFor(username) });
  void useSettings.persist.rehydrate();
}
