import { useLayoutEffect, useRef } from 'react';
import { NavigationType, useLocation, useNavigationType } from 'react-router';

/** The page an address belongs to, its first segment: `/settings/voice` is Settings. */
const pageOf = (pathname: string): string => pathname.split('/')[1] ?? '';

/**
 * A different page opens at its top. The pages scroll the document, one
 * scroller they all share (`layout.css`), and the router leaves its offset
 * where it was, so Sessions opened from the foot of Settings would open
 * mid-list, its title, search and New session out of view. A link or a
 * redirect to another page therefore starts it from the top, before it is
 * painted. Within a page the offset stays: the sections of Settings are one
 * page, each at an address of its own, and a session or a device opened from
 * its list, and the way back to it, belong to that list's page. Back and
 * Forward stay the browser's, which restores the offset it kept.
 */
export function usePageTop(): void {
  const { pathname } = useLocation();
  const navigationType = useNavigationType();
  const page = pageOf(pathname);
  const shown = useRef(page);

  useLayoutEffect(() => {
    if (page === shown.current) return;
    shown.current = page;
    if (navigationType === NavigationType.Pop) return;
    if (window.scrollX !== 0 || window.scrollY !== 0) window.scrollTo(0, 0);
  }, [page, navigationType]);
}
