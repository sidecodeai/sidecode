import { Suspense, lazy, memo } from 'react';

const Markdown = lazy(() => import('./Markdown'));

/** Renders Markdown; falls back to plain text until the chunk arrives. */
export const MarkdownText = memo(function MarkdownText({ text }: { text: string }) {
  return (
    <Suspense fallback={<div className="md md-plain">{text}</div>}>
      <Markdown text={text} />
    </Suspense>
  );
});
