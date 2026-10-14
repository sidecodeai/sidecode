import { useEffect, useState } from 'react';
import { rpc } from '../../lib/gateway';
import type { GitResult } from '../../protocol/frames';

export type DirStatus = 'idle' | 'checking' | 'exists' | 'missing';

export interface DirectoryProbe {
  status: DirStatus;
  git: GitResult | null;
}

interface ProbeResult extends DirectoryProbe {
  key: string;
}

const DEBOUNCE_MS = 400;

/** Debounced existence + git probe for the working directory field. */
export function useDirectoryProbe(deviceId: string | null, path: string): DirectoryProbe {
  const trimmed = path.trim();
  const key = `${deviceId ?? ''} ${trimmed}`;
  const [result, setResult] = useState<ProbeResult>({ key: '', status: 'idle', git: null });

  useEffect(() => {
    if (!deviceId || trimmed.length === 0) return;
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      try {
        await rpc('device.dirs', { device_id: deviceId, path: trimmed });
        let git: GitResult | null = null;
        try {
          git = await rpc('device.git', { device_id: deviceId, path: trimmed });
        } catch {
          git = null;
        }
        if (!cancelled) setResult({ key, status: 'exists', git });
      } catch {
        if (!cancelled) setResult({ key, status: 'missing', git: null });
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [key, deviceId, trimmed]);

  if (!deviceId || trimmed.length === 0) return { status: 'idle', git: null };
  // A key mismatch means the answer on screen is for an older path.
  if (result.key !== key) return { status: 'checking', git: null };
  return { status: result.status, git: result.git };
}
