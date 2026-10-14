/**
 * A24: the admin's accounts screen, and the member who never sees it.
 * `docs/DESIGN.md` § "Accounts" and PROTOCOL.md §3.9.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { UsersPage } from '../src/features/users/UsersPage';
import { useAuth } from '../src/stores/auth';
import { useSettings } from '../src/stores/settings';
import { useUsers } from '../src/stores/users';
import { strings } from '../src/strings';

interface Call {
  path: string;
  method: string;
  body: Record<string, unknown>;
}

let calls: Call[] = [];
let listed: Record<string, unknown>[] = [];
let registrationOpen = false;
/** Status for the next write; reads always succeed. */
let writeStatus = 200;

const ADMIN = {
  username: 'admin',
  role: 'admin',
  state: 'active',
  created_at: 1788426000000,
  last_login_at: 1788944400000,
  devices: 2,
};

const ALICE = {
  username: 'alice',
  role: 'member',
  state: 'active',
  created_at: 1788512400000,
  last_login_at: null,
  devices: 1,
};

function stubFetch() {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://gateway.test');
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
      calls.push({ path: url.pathname, method, body });

      if (url.pathname === '/api/users' && method === 'GET') {
        return ok({ users: listed, registration_open: registrationOpen });
      }
      if (writeStatus !== 200) {
        return new Response(
          JSON.stringify({ ok: false, error: { code: 'conflict', message: 'refused' } }),
          { status: writeStatus, headers: { 'content-type': 'application/json' } },
        );
      }
      if (url.pathname === '/api/registration') return ok({ open: body.open === true });
      return ok({ ok: true, user: ALICE });
    }),
  );
}

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/users']}>
      <Routes>
        <Route path="/users" element={<UsersPage />} />
        <Route path="/sessions" element={<h1>{strings.sessions.title}</h1>} />
      </Routes>
    </MemoryRouter>,
  );

const call = (path: string, method: string) =>
  calls.find((c) => c.path === path && c.method === method);

/** Opens the row menu of one account. */
async function openMenu(user: ReturnType<typeof userEvent.setup>, index: number) {
  const triggers = screen.getAllByRole('button', { name: strings.a11y.openMenu });
  await user.click(triggers[index]!);
}

beforeEach(() => {
  listed = [structuredClone(ADMIN), structuredClone(ALICE)];
  registrationOpen = false;
  writeStatus = 200;
  useSettings.setState({ language: 'en' });
  useAuth.setState({ status: 'signed-in', username: 'admin', role: 'admin' });
  useUsers.setState({ users: [], registrationOpen: false, loaded: false, error: null });
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the accounts screen', () => {
  it('lists every account with its role, state, devices and last sign-in', async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    expect(screen.getByText(strings.users.meta('Admin', 'Active'))).toBeInTheDocument();
    expect(screen.getByText(strings.users.meta('Member', 'Active'))).toBeInTheDocument();
    // A24: an account that has never signed in says so rather than showing a date.
    expect(
      screen.getByText(
        `${strings.users.deviceCount(1)} · ${strings.users.lastSignIn(strings.users.never)}`,
      ),
    ).toBeInTheDocument();
  });

  it('gives the admin row no actions', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    expect(screen.getAllByRole('button', { name: strings.a11y.openMenu })).toHaveLength(1);
  });

  it('opens and closes registration through PATCH /api/registration', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const toggle = screen.getByRole('switch', { name: strings.users.registration });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    await user.click(toggle);

    await waitFor(() => expect(call('/api/registration', 'PATCH')).toBeDefined());
    expect(call('/api/registration', 'PATCH')?.body).toEqual({ open: true });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('disables an account with a PATCH of its state', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    await openMenu(user, 0);
    await user.click(screen.getByRole('menuitem', { name: strings.users.disable }));

    await waitFor(() => expect(call('/api/users/alice', 'PATCH')).toBeDefined());
    expect(call('/api/users/alice', 'PATCH')?.body).toEqual({ state: 'disabled' });
  });

  it('resets a password with a PATCH carrying only the new one', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    await openMenu(user, 0);
    await user.click(screen.getByRole('menuitem', { name: strings.users.resetPassword }));
    await user.type(screen.getByLabelText(strings.account.newPassword), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.common.save }));

    await waitFor(() => expect(call('/api/users/alice', 'PATCH')).toBeDefined());
    expect(call('/api/users/alice', 'PATCH')?.body).toEqual({ password: 'devdevdev' });
  });

  it('names the devices that go with an account before deleting it', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    await openMenu(user, 0);
    await user.click(screen.getByRole('menuitem', { name: strings.users.deleteAction }));
    expect(screen.getByText(strings.users.deleteBodyDevices('alice', 1))).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: strings.users.deleteConfirm }));
    await waitFor(() => expect(call('/api/users/alice', 'DELETE')).toBeDefined());
  });

  it('adds an account with a username, a password and a role', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: strings.users.add }));
    await user.type(screen.getByLabelText(strings.account.username), 'bob');
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getByRole('button', { name: 'Admin' }));
    await user.click(screen.getAllByRole('button', { name: strings.users.add }).at(-1)!);

    await waitFor(() => expect(call('/api/users', 'POST')).toBeDefined());
    expect(call('/api/users', 'POST')?.body).toEqual({
      username: 'bob',
      password: 'devdevdev',
      role: 'admin',
    });
  });

  it('says a taken username in the modal that asked for it', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    writeStatus = 409;
    await user.click(screen.getByRole('button', { name: strings.users.add }));
    await user.type(screen.getByLabelText(strings.account.username), 'alice');
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getAllByRole('button', { name: strings.users.add }).at(-1)!);

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(strings.account.taken));
    // The modal stays open, holding what was typed, so it can be corrected.
    expect(screen.getByLabelText(strings.account.username)).toHaveValue('alice');
  });
});

describe('a member', () => {
  it('is sent to Sessions instead of the accounts screen', async () => {
    useAuth.setState({ status: 'signed-in', username: 'alice', role: 'member' });
    renderPage();

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.sessions.title })).toBeInTheDocument(),
    );
    expect(call('/api/users', 'GET')).toBeUndefined();
  });
});
