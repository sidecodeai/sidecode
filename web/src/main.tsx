// Global tokens and primitives load first so feature stylesheets can override
// them; Vite preserves this import order in the emitted CSS.
import './styles/base.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { App } from './App';
import { ChunkErrorBoundary } from './components/ChunkBoundary';
import { StaleBuildNotice } from './components/StaleBuildNotice';
import { registerServiceWorker } from './push/sw-register';
import { watchForStaleBuild } from './stores/staleBuild';

const container = document.getElementById('root');
if (!container) throw new Error('missing #root');

watchForStaleBuild();

// The boundary keeps a chunk that cannot be fetched from taking the notice down
// with the page: it is a sibling of the app, not a child of it.
createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <ChunkErrorBoundary>
        <App />
      </ChunkErrorBoundary>
      <StaleBuildNotice />
    </BrowserRouter>
  </StrictMode>,
);

registerServiceWorker();
