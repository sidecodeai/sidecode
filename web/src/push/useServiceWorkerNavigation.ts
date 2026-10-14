import { useEffect } from 'react';
import { useNavigate } from 'react-router';

/**
 * Routes a notification click that focused an already-open tab. The service
 * worker posts `{type: 'rc.navigate', path}` instead of opening a new window.
 */
export function useServiceWorkerNavigation(): void {
  const navigate = useNavigate();

  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: string; path?: string } | null;
      if (data?.type !== 'rc.navigate') return;
      // Same-origin paths only; never follow an absolute or protocol-relative URL.
      if (typeof data.path !== 'string' || !data.path.startsWith('/') || data.path.startsWith('//')) {
        return;
      }
      navigate(data.path);
    };
    navigator.serviceWorker.addEventListener('message', onMessage);
    return () => navigator.serviceWorker.removeEventListener('message', onMessage);
  }, [navigate]);
}
