import { useEffect, useState } from 'react';

const QUERY = '(prefers-reduced-motion: reduce)';

/** Whether the reader asked the system or the browser for less motion, kept current. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => window.matchMedia?.(QUERY).matches ?? false);
  useEffect(() => {
    const list = window.matchMedia?.(QUERY);
    if (!list) return;
    const update = (event: MediaQueryListEvent) => setReduced(event.matches);
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, []);
  return reduced;
}
