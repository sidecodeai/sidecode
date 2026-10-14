/**
 * A24: signing in is a username and a password, and a gateway that takes
 * registrations offers to make an account. `docs/DESIGN.md` § "Accounts".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { LoginPage } from '../src/features/login/LoginPage';
import { useAuth } from '../src/stores/auth';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';

interface Call {
  path: string;
  method: string;
  body: Record<string, unknown>;
}

let calls: Call[] = [];
let registrationOpen = false;
/** What `POST /api/login` and `POST /api/register` answer next. */
let authStatus = 200;

function stubFetch() {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://gateway.test');
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
      calls.push({ path: url.pathname, method, body });

      if (url.pathname === '/api/health') {
        return ok({
          ok: true,
          version: '0.1.0',
          protocol: 1,
          auth: { mode: 'password', registration_open: registrationOpen },
        });
      }
      if (url.pathname === '/api/login' || url.pathname === '/api/register') {
        if (authStatus !== 200) {
          return new Response(JSON.stringify({ ok: false, error: { code: 'refused', message: '' } }), {
            status: authStatus,
            headers: { 'content-type': 'application/json' },
          });
        }
        const username = String(body.username ?? '');
        return ok({
          ok: true,
          token: 't',
          exp: 0,
          user: { username, role: 'member' },
        });
      }
      return ok({});
    }),
  );
}

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });

const renderPage = () =>
  render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );

const call = (path: string) => calls.find((c) => c.path === path);

/**
 * This jsdom build exposes no `localStorage`, so the remembered username has
 * nothing to read. One in-memory store stands in for it across a test.
 */
function stubLocalStorage() {
  const map = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    removeItem: (key: string) => void map.delete(key),
  });
}

beforeEach(() => {
  registrationOpen = false;
  authStatus = 200;
  useSettings.setState({ language: 'en' });
  useAuth.setState({ status: 'signed-out', username: null, role: null });
  stubLocalStorage();
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('signing in', () => {
  it('sends the username with the password', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText(strings.account.username), 'alice');
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.login.submit }));

    await waitFor(() => expect(call('/api/login')).toBeDefined());
    expect(call('/api/login')?.body).toEqual({ username: 'alice', password: 'devdevdev' });
    expect(useAuth.getState().role).toBe('member');
  });

  it('remembers the username for the next sign-in', async () => {
    const user = userEvent.setup();
    const { unmount } = renderPage();

    await user.type(screen.getByLabelText(strings.account.username), 'alice');
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.login.submit }));
    await waitFor(() => expect(call('/api/login')).toBeDefined());
    unmount();

    useAuth.setState({ status: 'signed-out', username: null, role: null });
    renderPage();
    expect(screen.getByLabelText(strings.account.username)).toHaveValue('alice');
  });

  it('says an account is disabled, and says nothing else about a wrong password', async () => {
    const user = userEvent.setup();
    authStatus = 403;
    renderPage();

    await user.type(screen.getByLabelText(strings.account.username), 'alice');
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.login.submit }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(strings.account.disabled));

    authStatus = 401;
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.login.submit }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(strings.login.failed));
  });
});

describe('creating an account', () => {
  it('offers nothing while the gateway takes no registrations', async () => {
    renderPage();
    await waitFor(() => expect(call('/api/health')).toBeDefined());
    expect(
      screen.queryByRole('button', { name: strings.login.createAccountLink }),
    ).not.toBeInTheDocument();
  });

  it('offers the link when the gateway says registration is open', async () => {
    registrationOpen = true;
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: strings.login.createAccountLink })).toBeInTheDocument(),
    );
  });

  it('swaps the card, registers, and comes back on "Sign in instead"', async () => {
    const user = userEvent.setup();
    registrationOpen = true;
    renderPage();

    await user.click(await screen.findByRole('button', { name: strings.login.createAccountLink }));
    expect(screen.getByRole('button', { name: strings.login.signInInstead })).toBeInTheDocument();

    await user.type(screen.getByLabelText(strings.account.username), 'bob');
    await user.type(screen.getByLabelText(strings.account.password), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.login.createAccount }));

    await waitFor(() => expect(call('/api/register')).toBeDefined());
    expect(call('/api/register')?.body).toEqual({ username: 'bob', password: 'devdevdev' });
  });

  it('words every refusal by its status, and drops the link when registration closed', async () => {
    const user = userEvent.setup();
    registrationOpen = true;
    renderPage();
    await user.click(await screen.findByRole('button', { name: strings.login.createAccountLink }));

    const submit = async (password: string) => {
      await user.clear(screen.getByLabelText(strings.account.username));
      await user.type(screen.getByLabelText(strings.account.username), 'bob');
      await user.type(screen.getByLabelText(strings.account.password), password);
      await user.click(screen.getByRole('button', { name: strings.login.createAccount }));
    };

    authStatus = 409;
    await submit('devdevdev');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(strings.account.taken));

    authStatus = 400;
    await submit('devdevdev');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(strings.account.rules));

    authStatus = 403;
    await submit('devdevdev');
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(strings.login.registrationClosed),
    );

    await user.click(screen.getByRole('button', { name: strings.login.signInInstead }));
    expect(
      screen.queryByRole('button', { name: strings.login.createAccountLink }),
    ).not.toBeInTheDocument();
  });
});
