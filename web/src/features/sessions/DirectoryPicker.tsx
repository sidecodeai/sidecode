import { useCallback, useEffect, useState } from 'react';
import { ChevronUp, Folder, FolderPlus, GitBranch } from 'lucide-react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { NewFolderRow } from './NewFolderRow';
import { errorText } from '../../lib/errors';
import { rpc } from '../../lib/gateway';
import { RequestError } from '../../lib/ws';
import { tildePath } from '../../lib/format';
import { strings } from '../../strings';
import type { DirsResult } from '../../protocol/frames';

interface Props {
  open: boolean;
  deviceId: string | null;
  startPath?: string;
  onClose: () => void;
  onPick: (path: string) => void;
}

/**
 * Mounting the picker only while it is open keeps the first listing out of an
 * effect that would otherwise have to reset state synchronously on every open.
 */
export function DirectoryPicker(props: Props) {
  if (!props.open) return null;
  return <Picker {...props} />;
}

/**
 * A37: `conflict` is the one outcome the picker says in its own words — the
 * device reports an existing entry of any kind, and a person only needs to
 * know the name is taken. Everything else, `bad_request` included, is the
 * device's own sentence.
 */
function mkdirErrorText(error: unknown): string {
  if (error instanceof RequestError && error.code === 'conflict') {
    return strings.newSession.newFolderExists;
  }
  return errorText(error);
}

function Picker({ deviceId, startPath, onClose, onPick }: Props) {
  const [listing, setListing] = useState<DirsResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [naming, setNaming] = useState(false);
  const [folderName, setFolderName] = useState('');
  const [folderBusy, setFolderBusy] = useState(false);
  const [folderError, setFolderError] = useState<string | null>(null);

  const load = useCallback(
    async (path?: string) => {
      if (!deviceId) return;
      try {
        const result = await rpc('device.dirs', {
          device_id: deviceId,
          ...(path ? { path } : {}),
        });
        setListing(result);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : strings.errors.generic);
      } finally {
        setLoading(false);
      }
    },
    [deviceId],
  );

  // The first listing resolves asynchronously, so the effect body itself never
  // touches state synchronously.
  useEffect(() => {
    if (!deviceId) return;
    let cancelled = false;
    void rpc('device.dirs', {
      device_id: deviceId,
      ...(startPath ? { path: startPath } : {}),
    })
      .then((result) => {
        if (cancelled) return;
        setListing(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : strings.errors.generic);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [deviceId, startPath]);

  const openDir = (path?: string) => {
    // The name being typed belongs to the directory on screen, so leaving it
    // takes the row with it.
    setNaming(false);
    setLoading(true);
    void load(path);
  };

  const startNaming = () => {
    setFolderName('');
    setFolderError(null);
    setNaming(true);
  };

  /**
   * A37: the reply is the new directory's own listing, so making a folder is
   * also walking into it — "Use this directory" then picks what was just made.
   */
  const createFolder = () => {
    const name = folderName.trim();
    if (!deviceId || !listing || name === '' || folderBusy) return;
    setFolderBusy(true);
    setFolderError(null);
    void rpc('device.mkdir', { device_id: deviceId, path: listing.path, name })
      .then((result) => {
        setListing(result);
        setError(null);
        setNaming(false);
        setFolderName('');
      })
      .catch((err: unknown) => {
        setFolderError(mkdirErrorText(err));
      })
      .finally(() => {
        setFolderBusy(false);
      });
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={strings.newSession.browseTitle}
      width={520}
      showClose
      footer={
        <>
          <Button onClick={onClose}>{strings.common.cancel}</Button>
          <Button
            variant="primary"
            disabled={!listing}
            onClick={() => {
              if (listing) onPick(listing.path);
              onClose();
            }}
          >
            {strings.newSession.browseUse}
          </Button>
        </>
      }
    >
      <div className="picker-head">
        <div className="picker-path mono">
          {listing ? tildePath(listing.path) : strings.common.loading}
        </div>
        <Button small disabled={!listing || naming} onClick={startNaming}>
          <FolderPlus size={14} aria-hidden />
          {strings.newSession.newFolder}
        </Button>
      </div>
      {naming ? (
        <NewFolderRow
          name={folderName}
          busy={folderBusy}
          error={folderError}
          onChange={setFolderName}
          onSubmit={createFolder}
          onCancel={() => setNaming(false)}
        />
      ) : null}
      {error ? <p className="login-error">{error}</p> : null}
      <ul className="picker-list scroll-thin">
        {listing?.parent ? (
          <li>
            <button type="button" onClick={() => openDir(listing.parent ?? undefined)}>
              <ChevronUp size={15} aria-hidden />
              <span>{strings.newSession.browseUp}</span>
            </button>
          </li>
        ) : null}
        {listing?.entries.map((entry) => (
          <li key={entry.path}>
            <button type="button" onClick={() => openDir(entry.path)}>
              <Folder size={15} aria-hidden />
              <span className="mono">{entry.name}</span>
              {entry.is_git ? <GitBranch size={13} aria-hidden className="picker-git" /> : null}
            </button>
          </li>
        ))}
        {listing && listing.entries.length === 0 && !loading ? (
          <li className="picker-empty hint">{strings.newSession.browseEmpty}</li>
        ) : null}
      </ul>
    </Modal>
  );
}
