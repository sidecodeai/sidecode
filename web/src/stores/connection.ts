/**
 * Owns the app socket lifecycle and fans incoming push frames out to the
 * device / session / chat stores.
 */
import { create } from 'zustand';
import { getSocket, setSocket } from '../lib/gateway';
import { AppSocket, socketUrl, type SocketStatus } from '../lib/ws';
import type { PairingStep, PushFrame } from '../protocol/frames';
import type { Device, PolishInfo } from '../protocol/types';
import { useChat } from './chat';
import { useDevices } from './devices';
import { usePreferences } from './preferences';
import { useSessions } from './sessions';
import { pushTerminalFrame } from './terminal';

export interface PairingProgress {
  code: string;
  step: PairingStep;
  device?: Device;
}

interface ConnectionState {
  status: SocketStatus;
  helloAt: number | null;
  gatewayVersion: string | null;
  protocol: number | null;
  username: string | null;
  /**
   * Whether this gateway transcribes. Its language list is nothing to choose
   * from since A44 — the provider detects the language — so it is not kept.
   */
  stt: { enabled: boolean };
  /** A29: whether this gateway can polish a dictation. Off until `hello` says so. */
  polish: PolishInfo;
  /** server_time minus the local clock at the last hello, in milliseconds. */
  clockSkewMs: number;
  pairing: PairingProgress | null;
  onUnauthorized: (() => void) | null;
  connect: (onUnauthorized: () => void) => void;
  disconnect: () => void;
  clearPairing: () => void;
}

/**
 * What the store knows before a `hello`. Sign-out puts it back here, so the
 * composer never offers a capability the next `hello` has not confirmed and
 * nothing of the previous account is left to read.
 */
type Disconnected = Omit<
  ConnectionState,
  'connect' | 'disconnect' | 'clearPairing' | 'onUnauthorized'
>;

function disconnected(): Disconnected {
  return {
    status: 'closed',
    helloAt: null,
    gatewayVersion: null,
    protocol: null,
    username: null,
    stt: { enabled: false },
    polish: { enabled: false },
    clockSkewMs: 0,
    pairing: null,
  };
}

export const useConnection = create<ConnectionState>((set, get) => ({
  ...disconnected(),
  status: 'idle',
  onUnauthorized: null,

  connect: (onUnauthorized) => {
    if (get().status !== 'idle' && get().status !== 'closed') return;
    const socket = new AppSocket({
      url: socketUrl('/ws/app'),
      onStatus: (status) => set({ status }),
      onFrame: (frame) => handleFrame(frame, set),
      onUnauthorized,
    });
    setSocket(socket);
    set({ onUnauthorized });
    socket.start();
  },

  disconnect: () => {
    getSocket()?.stop();
    setSocket(null);
    set({ ...disconnected(), onUnauthorized: null });
  },

  clearPairing: () => set({ pairing: null }),
}));

type Setter = (partial: Partial<ConnectionState>) => void;

function handleFrame(frame: PushFrame, set: Setter): void {
  switch (frame.type) {
    case 'hello': {
      useDevices.getState().replaceAll(frame.devices);
      useSessions.getState().replaceAll(frame.sessions);
      // A35: the account's switches, or nothing at all on an older gateway.
      usePreferences.getState().fromHello(frame.preferences);
      set({
        helloAt: Date.now(),
        gatewayVersion: frame.gateway_version,
        protocol: frame.protocol,
        username: frame.user.username,
        stt: { enabled: frame.stt.enabled },
        polish: frame.polish ?? { enabled: false },
        clockSkewMs:
          typeof frame.server_time === 'number' ? frame.server_time - Date.now() : 0,
      });
      return;
    }
    case 'device.updated':
      useDevices.getState().upsert(frame.device);
      return;
    case 'device.removed':
      useDevices.getState().remove(frame.device_id);
      return;
    case 'session.updated':
      useSessions.getState().upsert(frame.session);
      return;
    case 'session.removed':
      useSessions.getState().remove(frame.device_id, frame.session_id);
      return;
    case 'session.event':
      // Amendment A5: `device_id` is present when the gateway stamped it.
      useChat.getState().ingestEvent(frame.session_id, frame.event, frame.device_id);
      return;
    case 'preferences.updated':
      usePreferences.getState().apply(frame.preferences);
      return;
    // A38: bytes and the shell's end, for the one terminal page that asked.
    // The gateway sends them to this connection alone, so there is nobody to
    // fan them out to; the feed hands them straight to whoever is open.
    case 'terminal.output':
    case 'terminal.exited':
      pushTerminalFrame(frame);
      return;
    case 'pairing.progress':
      set({
        pairing: {
          code: frame.code,
          step: frame.step,
          ...(frame.device ? { device: frame.device } : {}),
        },
      });
      return;
    default:
      return;
  }
}

export const isOnline = (status: SocketStatus): boolean => status === 'open';
