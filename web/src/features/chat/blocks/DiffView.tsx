import { strings } from '../../../strings';
import type { DiffInfo } from '../../../protocol/types';

/** Unified patch with coloured add/remove lines. */
export function DiffView({ diff }: { diff: DiffInfo }) {
  if (!diff.patch) return null;
  const lines = diff.patch.split('\n');
  return (
    <div className="diff">
      <pre className="scroll-thin">
        {lines.map((line, index) => (
          <span key={index} className={`diff-line ${diffClass(line)}`}>
            {line}
            {'\n'}
          </span>
        ))}
      </pre>
      {diff.patch_truncated ? <p className="hint diff-note">{strings.chat.patchTruncated}</p> : null}
    </div>
  );
}

export function DiffStat({ diff }: { diff: DiffInfo }) {
  return (
    <span className="diff-stat mono">
      <span className="add">+{diff.additions}</span> <span className="del">−{diff.deletions}</span>
    </span>
  );
}

function diffClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) return 'meta';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return '';
}
