import { useEffect, useState } from 'react';

/**
 * Whether this page is in front of the person: its document is visible and its
 * window has the focus. A tab in the background, a minimised window and a
 * window behind another application are open, but nobody is looking at them.
 */
export function useInFront(): boolean {
  const [inFront, setInFront] = useState(readInFront);
  useEffect(() => {
    const update = () => setInFront(readInFront());
    document.addEventListener('visibilitychange', update);
    window.addEventListener('focus', update);
    window.addEventListener('blur', update);
    // Either may have changed between the first render and this effect.
    update();
    return () => {
      document.removeEventListener('visibilitychange', update);
      window.removeEventListener('focus', update);
      window.removeEventListener('blur', update);
    };
  }, []);
  return inFront;
}

function readInFront(): boolean {
  return document.visibilityState === 'visible' && document.hasFocus();
}
