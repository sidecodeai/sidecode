import { lazy, memo } from 'react';
import { ChunkBoundary } from '../../components/ChunkBoundary';

const Markdown = lazy(() => import('./Markdown'));

/**
 * Renders Markdown; falls back to plain text until the chunk arrives, and keeps
 * the plain text if it never does, so a tab older than the server still reads.
 */
export const MarkdownText = memo(function MarkdownText({ text }: { text: string }) {
  const plain = <div className="md md-plain">{text}</div>;
  return (
    <ChunkBoundary loading={plain} failed={plain}>
      <Markdown text={text} />
    </ChunkBoundary>
  );
});
