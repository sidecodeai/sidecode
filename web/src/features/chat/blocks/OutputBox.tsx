import { useState } from 'react';
import { foldLines } from '../../../lib/format';
import { strings } from '../../../strings';

const FOLD_LINES = 20;

interface Props {
  text: string;
  truncated?: boolean;
  onOpenFull?: () => void;
  live?: boolean;
}

/** Monospace output pane, folded beyond ~20 lines. */
export function OutputBox({ text, truncated, onOpenFull, live }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [loadingFull, setLoadingFull] = useState(false);
  const { head, folded, total } = foldLines(text, FOLD_LINES);
  const body = expanded || live ? text : head;

  return (
    <div className={`output-box${live ? ' live' : ''}`}>
      <pre className="scroll-thin">{body}</pre>
      {(folded && !live) || truncated ? (
        <div className="output-actions">
          {folded && !live ? (
            <button type="button" className="link-btn" onClick={() => setExpanded((v) => !v)}>
              {expanded ? strings.common.showLess : strings.chat.showMoreLines(total)}
            </button>
          ) : null}
          {truncated ? (
            <button
              type="button"
              className="link-btn"
              disabled={loadingFull || !onOpenFull}
              onClick={async () => {
                if (!onOpenFull) return;
                setLoadingFull(true);
                try {
                  await onOpenFull();
                } finally {
                  setLoadingFull(false);
                }
              }}
            >
              {loadingFull ? strings.common.loading : strings.chat.openFullOutput}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
