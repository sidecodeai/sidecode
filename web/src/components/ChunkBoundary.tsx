/**
 * Where a lazily loaded part of the app is drawn. A chunk that cannot be
 * fetched — the tab is older than the server (`stores/staleBuild.ts`) — rejects
 * the import, and without a boundary React unmounts the whole page and the
 * typed text with it. Here the failure stays local: the part is drawn as
 * `failed` (nothing, by default) and the page-wide notice asks for a reload.
 */
import { Component, Suspense, type ReactNode } from 'react';
import { isChunkLoadError, useStaleBuild } from '../stores/staleBuild';

interface BoundaryProps {
  /** What stands in for the part whose chunk did not arrive. */
  fallback?: ReactNode;
  children: ReactNode;
}

export class ChunkErrorBoundary extends Component<BoundaryProps, { error: Error | null }> {
  override state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  override componentDidCatch(error: Error): void {
    if (isChunkLoadError(error)) useStaleBuild.getState().mark();
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    // Only a chunk that failed to load is held here; any other error goes on up.
    if (!isChunkLoadError(error)) throw error;
    return this.props.fallback ?? null;
  }
}

interface ChunkBoundaryProps {
  /** Drawn while the chunk is on its way. */
  loading?: ReactNode;
  /** Drawn if it never arrives. */
  failed?: ReactNode;
  children: ReactNode;
}

export function ChunkBoundary({ loading = null, failed = null, children }: ChunkBoundaryProps) {
  return (
    <ChunkErrorBoundary fallback={failed}>
      <Suspense fallback={loading}>{children}</Suspense>
    </ChunkErrorBoundary>
  );
}
