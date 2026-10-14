import { create } from 'zustand';
import { api } from '../lib/api';
import { rpc } from '../lib/gateway';
import { RequestError } from '../lib/ws';
import { strings } from '../strings';
import type { Device } from '../protocol/types';

interface DevicesState {
  devices: Device[];
  loaded: boolean;
  error: string | null;
  /**
   * A22: why a `device.update` was refused outright, per device. The gateway
   * only records `update_state: "failed"` for an update it accepted, so a
   * refusal of a retry (a running session, a client installed from source)
   * lives here until the gateway sends that device again.
   */
  updateErrors: Record<string, string>;
  load: () => Promise<void>;
  replaceAll: (devices: Device[]) => void;
  upsert: (device: Device) => void;
  remove: (deviceId: string) => void;
  rename: (deviceId: string, name: string) => Promise<void>;
  revoke: (deviceId: string) => Promise<void>;
  requestUpdate: (deviceId: string, build: string) => Promise<void>;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

const without = (errors: Record<string, string>, deviceId: string): Record<string, string> => {
  if (!(deviceId in errors)) return errors;
  const next = { ...errors };
  delete next[deviceId];
  return next;
};

const byName = (a: Device, b: Device): number => a.name.localeCompare(b.name);

export const useDevices = create<DevicesState>((set, get) => ({
  devices: [],
  loaded: false,
  error: null,
  updateErrors: {},

  load: async () => {
    try {
      const { devices } = await api.devices();
      set({ devices: [...devices].sort(byName), loaded: true, error: null });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'failed', loaded: true });
      throw err;
    }
  },

  replaceAll: (devices) => set({ devices: [...devices].sort(byName), loaded: true }),

  upsert: (device) =>
    set((s) => {
      const next = s.devices.filter((d) => d.device_id !== device.device_id);
      next.push(device);
      // The gateway's own `update_state` now speaks for this device.
      return { devices: next.sort(byName), updateErrors: without(s.updateErrors, device.device_id) };
    }),

  remove: (deviceId) =>
    set((s) => ({ devices: s.devices.filter((d) => d.device_id !== deviceId) })),

  rename: async (deviceId, name) => {
    const { device } = await api.renameDevice(deviceId, name);
    get().upsert(device);
  },

  revoke: async (deviceId) => {
    await api.removeDevice(deviceId);
    get().remove(deviceId);
  },

  requestUpdate: async (deviceId, build) => {
    set((s) => ({ updateErrors: without(s.updateErrors, deviceId) }));
    try {
      await rpc('device.update', { device_id: deviceId, build });
    } catch (err) {
      // The device's own words — a running session, an unsupported install.
      const message = err instanceof RequestError ? err.message : strings.errors.generic;
      set((s) => ({ updateErrors: { ...s.updateErrors, [deviceId]: message } }));
    }
  },

  reset: () => set({ devices: [], loaded: false, error: null, updateErrors: {} }),
}));

/**
 * A36: the third line of a device row, or null when there is nothing to say —
 * which is the ordinary case, because the gateway brings every device to the
 * wheel it serves on its own. A device only speaks while its update runs and
 * once it has failed; the version it runs is nobody's to watch.
 */
export const updateNotice = (
  device: Device,
  localError: string | undefined,
): { tone: 'updating' | 'failed'; text: string } | null => {
  if (device.update_state === 'updating') return { tone: 'updating', text: strings.devices.updating };
  const failure = localError ?? (device.update_state === 'failed' ? device.update_message : null);
  if (failure) return { tone: 'failed', text: strings.devices.updateFailed(failure) };
  return null;
};

export const selectDevice = (deviceId: string | undefined) => (s: DevicesState): Device | undefined =>
  deviceId ? s.devices.find((d) => d.device_id === deviceId) : undefined;
