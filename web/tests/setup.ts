import '@testing-library/jest-dom/vitest';
import { beforeEach } from 'vitest';
import { useDrafts } from '../src/stores/drafts';

// A draft lives for the tab's life (`docs/DESIGN.md` § "The composer"), and the
// store that holds it is module state. Each test is its own tab.
beforeEach(() => {
  useDrafts.getState().reset();
});
