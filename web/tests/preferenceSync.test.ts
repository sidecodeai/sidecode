/**
 * A41 — the five Settings values this app keeps are the account's. `hello` and
 * every `preferences.updated` frame are the truth, a change made here goes up
 * with `PATCH /api/preferences`, and a field the account has not set is written
 * up once from this browser's value. The dictation language is the iPhone's
 * since A44, and nothing here reads or writes it. `docs/DESIGN.md` § "Paused by
 * the usage limit" → "Settings are the account's, not the device's".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/lib/api';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import type { SyncedSettings } from '../src/stores/preferenceFields';
import { readSettingsFor, useSettings } from '../src/stores/settings';
import type { HelloFrame, PushFrame } from '../src/protocol/frames';
import type { Preferences } from '../src/protocol/types';

// The store owns the socket, so the frames are fed through the one it opens.
const { captured } = vi.hoisted(() => ({
  captured: { onFrame: null as ((frame: PushFrame) => void) | null },
}));

vi.mock('../src/lib/ws', () => ({
  socketUrl: () => 'ws://test/ws/app',
  AppSocket: class {
    constructor(options: { onFrame: (frame: PushFrame) => void }) {
      captured.onFrame = options.onFrame;
    }
    start(): void {
      /* nothing to dial in a test */
    }
    stop(): void {
      /* nothing to close */
    }
  },
}));

const handleFrame = (frame: PushFrame): void => {
  if (!captured.onFrame) throw new Error('the connection store opened no socket');
  captured.onFrame(frame);
};

const hello = (preferences?: Preferences): HelloFrame =>
  ({
    type: 'hello',
    protocol: 1,
    gateway_version: '1.6.1',
    user: { username: 'admin', role: 'admin' },
    devices: [],
    sessions: [],
    stt: { enabled: true, languages: ['auto'] },
    server_time: Date.now(),
    ...(preferences ? { preferences } : {}),
  }) as HelloFrame;

/** Every field set, as `fixtures/http/preferences.response.json` carries them. */
const all: Preferences = {
  resume_after_limit: true,
  language: 'zh-Hans',
  stt_language: 'zh',
  polish_enabled: true,
  polish_model: 'gpt-5.4-mini',
  polish_strength: 'strong',
  timeline_detail: 'detailed',
};

const synced = () => {
  const s = useSettings.getState();
  return {
    language: s.language,
    polishEnabled: s.polishEnabled,
    polishModel: s.polishModel,
    polishStrength: s.polishStrength,
    timelineDetail: s.timelineDetail,
  };
};

const defaults: SyncedSettings = {
  language: 'en',
  polishEnabled: false,
  polishModel: '',
  polishStrength: 'moderate',
  timelineDetail: 'simple',
};

let patch: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  readSettingsFor(null);
  useSettings.setState(defaults);
  // Puts the store back to "no gateway holds these", like signing out.
  usePreferences.getState().reset();
  useConnection.getState().connect(() => undefined);
  patch = vi.spyOn(api, 'setPreferences').mockResolvedValue({ preferences: all });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the account settings on hello', () => {
  it('reads what the account holds, over whatever this browser had', async () => {
    handleFrame(hello(all));

    expect(synced()).toEqual({
      language: 'zh-Hans',
      polishEnabled: true,
      polishModel: 'gpt-5.4-mini',
      polishStrength: 'strong',
      timelineDetail: 'detailed',
    });
    expect(patch).not.toHaveBeenCalled();
  });

  it('ignores a word this build does not know and keeps the value it has', () => {
    handleFrame(hello({ resume_after_limit: false, language: 'fr' } as unknown as Preferences));

    expect(useSettings.getState().language).toBe('en');
  });

  it("writes this browser's values up once for the fields nobody has set", async () => {
    useSettings.setState({ language: 'zh-Hans', polishEnabled: true, polishModel: 'gpt-4.1' });

    handleFrame(hello({ resume_after_limit: false, timeline_detail: 'detailed' }));

    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    // The one field the account already had is not written back.
    expect(patch).toHaveBeenCalledWith({
      language: 'zh-Hans',
      polish_enabled: true,
      polish_model: 'gpt-4.1',
      polish_strength: 'moderate',
    });
    expect(useSettings.getState().timelineDetail).toBe('detailed');

    // Once: what the gateway then publishes is read, not written back.
    handleFrame({ type: 'preferences.updated', preferences: all } as PushFrame);
    await Promise.resolve();
    expect(patch).toHaveBeenCalledTimes(1);
  });

  it('writes nothing up when the account has set them all', async () => {
    handleFrame(hello(all));

    await Promise.resolve();
    expect(patch).not.toHaveBeenCalled();
  });

  it('leaves the dictation language to the iPhone, set or not (A44)', async () => {
    handleFrame(hello({ resume_after_limit: false }));

    // Nobody has set it, and it is still not this app's to write up.
    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0]?.[0]).not.toHaveProperty('stt_language');

    // A phone's choice arrives and is nothing this app holds.
    handleFrame({ type: 'preferences.updated', preferences: all } as PushFrame);
    expect(useSettings.getState()).not.toHaveProperty('sttLanguage');
  });

  it('writes nothing to a gateway that holds no preferences at all', async () => {
    useSettings.setState({ language: 'zh-Hans' });

    handleFrame(hello());

    await Promise.resolve();
    expect(patch).not.toHaveBeenCalled();
    expect(useSettings.getState().language).toBe('zh-Hans');
  });

  it('leaves this browser alone when the gateway answers without the fields', async () => {
    patch.mockResolvedValue({ preferences: { resume_after_limit: false } });
    useSettings.setState({ language: 'zh-Hans', timelineDetail: 'detailed' });

    handleFrame(hello({ resume_after_limit: false }));

    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(synced()).toEqual({ ...defaults, language: 'zh-Hans', timelineDetail: 'detailed' });
  });
});

describe('the account settings while the app is open', () => {
  it('follows a change another app of the account made', () => {
    handleFrame(hello({ resume_after_limit: false, ...withoutResume(all) }));

    handleFrame({
      type: 'preferences.updated',
      preferences: { ...all, polish_enabled: false, timeline_detail: 'simple' },
    } as PushFrame);

    expect(useSettings.getState().polishEnabled).toBe(false);
    expect(useSettings.getState().timelineDetail).toBe('simple');
  });

  it('answers the gateway’s echo of its own change with nothing', async () => {
    handleFrame(hello(all));
    patch.mockClear();

    handleFrame({ type: 'preferences.updated', preferences: all } as PushFrame);

    await Promise.resolve();
    expect(patch).not.toHaveBeenCalled();
    expect(synced().polishStrength).toBe('strong');
  });
});

describe('a change made on this screen', () => {
  it('goes up as the one field, and what comes back is what is held', async () => {
    handleFrame(hello(all));
    patch.mockClear();
    patch.mockResolvedValue({ preferences: { ...all, polish_model: 'gpt-5.4' } });

    useSettings.getState().setPolishModel('gpt-5.4');

    expect(useSettings.getState().polishModel).toBe('gpt-5.4');
    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch).toHaveBeenCalledWith({ polish_model: 'gpt-5.4' });
    expect(useSettings.getState().polishModel).toBe('gpt-5.4');
  });

  it('keeps what the reader chose when the gateway refuses', async () => {
    handleFrame(hello(all));
    patch.mockClear();
    patch.mockRejectedValue(new Error('nope'));

    useSettings.getState().setTimelineDetail('simple');

    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(useSettings.getState().timelineDetail).toBe('simple');
  });

  it('is nobody’s but this browser’s on a gateway that holds none', () => {
    handleFrame(hello());

    useSettings.getState().setLanguage('zh-Hans');

    expect(patch).not.toHaveBeenCalled();
    expect(useSettings.getState().language).toBe('zh-Hans');
  });

  it('leaves the folds of a list out of it, which are this browser’s', async () => {
    handleFrame(hello(all));
    patch.mockClear();

    useSettings.getState().toggleDeviceCollapsed('dev-1');

    await Promise.resolve();
    expect(patch).not.toHaveBeenCalled();
    expect(useSettings.getState().collapsedDevices).toEqual(['dev-1']);
  });
});

/** The Settings fields of an object, without the switch that was there before them. */
function withoutResume(preferences: Preferences): Partial<Preferences> {
  const { resume_after_limit: _resume, ...rest } = preferences;
  return rest;
}
