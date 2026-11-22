import { splitFolder, tildePath } from '../../lib/format';

interface Props {
  device: string;
  cwd: string;
  /** The session's branch, or null outside a repository. */
  branch: string | null;
}

/**
 * The line under the conversation's title: the device, the working directory
 * and the branch. When the line is too narrow the directory gives way first,
 * cut from its head but never past the folder it ends in, and only then are the
 * device and the branch cut, at their tail (`chat.css`). The text itself stays
 * whole, which is what assistive technology reads.
 */
export function ChatSubLine({ device, cwd, branch }: Props) {
  const [head, folder] = splitFolder(tildePath(cwd));
  return (
    <p className="mono chat-sub">
      <span className="chat-sub-device">{device}</span>
      <span className="chat-sub-colon">:</span>
      {head ? (
        // Right-to-left outside and a left-to-right run inside, as the session
        // row's path: the characters stay in order and the ellipsis lands at
        // the head.
        <span className="chat-sub-head">
          <bdi dir="ltr">{head}</bdi>
        </span>
      ) : null}
      <span className="chat-sub-folder">{folder}</span>
      {branch === null ? null : (
        <>
          <span className="chat-sub-dot"> · </span>
          <span className="chat-sub-branch">{branch}</span>
        </>
      )}
    </p>
  );
}
