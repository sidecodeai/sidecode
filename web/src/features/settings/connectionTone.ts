/**
 * The dot in the Settings header, from the app socket alone (`docs/DESIGN.md`
 * § "The Settings screen"): green while connected, pulsing amber while the link
 * is coming up, grey while it is down. The red the ruling gives a refused
 * gateway is the phone's: a browser whose session the gateway refuses is signed
 * out and shown the sign-in form, so it never draws this header at all.
 */
import type { DotTone } from '../../components/dotTone';
import type { SocketStatus } from '../../lib/ws';
import { strings } from '../../strings';

export function connectionTone(status: SocketStatus): DotTone {
  switch (status) {
    case 'open':
      return 'working';
    case 'connecting':
    case 'reconnecting':
      return 'waiting';
    case 'idle':
    case 'closed':
      return 'off';
  }
}

/** The word for that tone. It is read aloud and shown on hover, never printed. */
export function connectionWord(status: SocketStatus): string {
  switch (connectionTone(status)) {
    case 'working':
      return strings.settings.connected;
    case 'waiting':
      return strings.settings.connecting;
    default:
      return strings.settings.offline;
  }
}
