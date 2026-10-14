/**
 * A37 — the directory picker makes a folder. `docs/DESIGN.md` § "The three
 * screens" (New session): it asks for one name, makes that directory inside
 * the one on screen, then stands in the new, empty folder so the same choose
 * action picks it. A clash is said beside the name and the name is kept.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DirectoryPicker } from '../src/features/sessions/DirectoryPicker';
import { rpc } from '../src/lib/gateway';
import { RequestError } from '../src/lib/ws';
import { strings } from '../src/strings';

vi.mock('../src/lib/gateway', () => ({ rpc: vi.fn() }));

const HOME = {
  path: '/Users/me/dev',
  parent: '/Users/me',
  entries: [{ name: 'remote-control', path: '/Users/me/dev/remote-control', is_git: true }],
  recent: [],
};

const MADE = {
  path: '/Users/me/dev/new-project',
  parent: '/Users/me/dev',
  entries: [],
  recent: [],
};

const onPick = vi.fn();
const onClose = vi.fn();

/** Every listing answered from `HOME`, with `device.mkdir` left to each test. */
function servingHome(mkdir: (params: Record<string, unknown>) => unknown): void {
  vi.mocked(rpc).mockImplementation((async (method: string, params: Record<string, unknown>) => {
    if (method === 'device.dirs') return HOME;
    if (method === 'device.mkdir') return mkdir(params);
    throw new Error(`unexpected ${method}`);
  }) as unknown as typeof rpc);
}

const renderPicker = () =>
  render(<DirectoryPicker open deviceId="dev-mac" onClose={onClose} onPick={onPick} />);

/** Opens the picker, waits for its first listing, and reveals the name row. */
async function openNewFolder(): Promise<void> {
  renderPicker();
  await screen.findByText('~/dev');
  await userEvent.click(screen.getByRole('button', { name: strings.newSession.newFolder }));
}

const nameField = () => screen.getByLabelText(strings.newSession.newFolderName);

beforeEach(() => {
  onPick.mockReset();
  onClose.mockReset();
  vi.mocked(rpc).mockReset();
});

describe('the directory picker', () => {
  it('makes the folder, stands in it, and picks it', async () => {
    servingHome(() => MADE);
    await openNewFolder();

    await userEvent.type(nameField(), 'new-project');
    await userEvent.click(screen.getByRole('button', { name: strings.newSession.newFolderCreate }));

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('device.mkdir', {
        device_id: 'dev-mac',
        path: '/Users/me/dev',
        name: 'new-project',
      }),
    );

    // The reply is the new directory's own listing: the picker stands in it.
    expect(await screen.findByText('~/dev/new-project')).toBeInTheDocument();
    expect(screen.getByText(strings.newSession.browseEmpty)).toBeInTheDocument();
    expect(screen.queryByLabelText(strings.newSession.newFolderName)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: strings.newSession.browseUse }));
    expect(onPick).toHaveBeenCalledWith('/Users/me/dev/new-project');
  });

  it('sends the name on Enter', async () => {
    servingHome(() => MADE);
    await openNewFolder();

    await userEvent.type(nameField(), 'new-project{Enter}');

    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith(
        'device.mkdir',
        expect.objectContaining({ name: 'new-project' }),
      ),
    );
  });

  it('says a clash beside the name and keeps the name', async () => {
    servingHome(() => {
      throw new RequestError({ code: 'conflict', message: 'a folder with that name exists' });
    });
    await openNewFolder();

    await userEvent.type(nameField(), 'remote-control{Enter}');

    expect(await screen.findByText(strings.newSession.newFolderExists)).toBeInTheDocument();
    expect(nameField()).toHaveValue('remote-control');
    // Still in the directory it was asked for.
    expect(screen.getByText('~/dev')).toBeInTheDocument();
  });

  it('shows what the device said about a name it refused', async () => {
    servingHome(() => {
      throw new RequestError({ code: 'bad_request', message: 'a name cannot contain a slash' });
    });
    await openNewFolder();

    await userEvent.type(nameField(), 'a/b{Enter}');

    expect(await screen.findByText('a name cannot contain a slash')).toBeInTheDocument();
    expect(nameField()).toHaveValue('a/b');
  });

  it('hides the row on Cancel and on Escape, without closing the picker', async () => {
    servingHome(() => MADE);
    await openNewFolder();

    await userEvent.click(screen.getAllByRole('button', { name: strings.common.cancel })[0]!);
    expect(screen.queryByLabelText(strings.newSession.newFolderName)).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: strings.newSession.newFolder }));
    await userEvent.type(nameField(), 'draft{Escape}');

    expect(screen.queryByLabelText(strings.newSession.newFolderName)).not.toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('leaves the row behind when the listing changes', async () => {
    servingHome(() => MADE);
    await openNewFolder();

    await userEvent.click(screen.getByRole('button', { name: /remote-control/ }));

    await waitFor(() =>
      expect(screen.queryByLabelText(strings.newSession.newFolderName)).not.toBeInTheDocument(),
    );
  });

  it('will not send an empty name, and holds still while one is in flight', async () => {
    let settle: ((listing: typeof MADE) => void) | undefined;
    servingHome(
      () =>
        new Promise((resolve) => {
          settle = resolve;
        }),
    );
    await openNewFolder();

    const create = screen.getByRole('button', { name: strings.newSession.newFolderCreate });
    expect(create).toBeDisabled();

    await userEvent.type(nameField(), 'new-project{Enter}');
    await waitFor(() => expect(create).toBeDisabled());
    expect(nameField()).toBeDisabled();

    settle?.(MADE);
    expect(await screen.findByText('~/dev/new-project')).toBeInTheDocument();
  });
});
