import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const GATEWAY = process.env.RC_GATEWAY ?? 'http://127.0.0.1:8787';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: GATEWAY, changeOrigin: false },
      '/ws': { target: GATEWAY, ws: true, changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // The Markdown renderer and its highlighter are reached only through a
    // dynamic import, so Rollup keeps them in their own chunk. Leave the
    // splitting automatic: a manual chunk here would be preloaded eagerly.
  },
});
