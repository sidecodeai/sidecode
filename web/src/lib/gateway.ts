/**
 * Module-level handle on the single app socket. Stores import this instead of
 * the connection store so request helpers never create an import cycle.
 */
import type { RequestParams, RequestResult, RequestType } from '../protocol/frames';
import type { AppSocket } from './ws';
import { RequestError } from './ws';

let socket: AppSocket | null = null;

export function setSocket(next: AppSocket | null): void {
  socket = next;
}

export function getSocket(): AppSocket | null {
  return socket;
}

export function rpc<T extends RequestType>(
  type: T,
  params: RequestParams<T>,
  options?: { id?: string; timeoutMs?: number },
): Promise<RequestResult<T>> {
  if (!socket) {
    return Promise.reject(new RequestError({ code: 'internal', message: '' }));
  }
  return socket.request(type, params, options);
}
