import { useEffect, useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { ApiError, api } from '../../lib/api';
import { Mark } from '../../layout/Mark';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { rememberUsername, rememberedUsername } from './rememberedUsername';
import './login.css';

/** A24: the card asks for an account, or makes one. */
type Mode = 'sign-in' | 'register';

export function LoginPage() {
  const [mode, setMode] = useState<Mode>('sign-in');
  const [username, setUsername] = useState(rememberedUsername);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A24: whether this gateway takes registrations. The login screen is the only
  // place that asks, because it is the only place that offers it.
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const login = useAuth((s) => s.login);
  const register = useAuth((s) => s.register);
  const status = useAuth((s) => s.status);
  const navigate = useNavigate();
  const location = useLocation();
  // Where the app was headed when it found nobody signed in. A `/pair` link
  // carries its claim token in the hash, so the whole path is kept (A23).
  const destination = rememberedDestination(location.state);

  useEffect(() => {
    if (status === 'signed-in') navigate(destination, { replace: true });
  }, [status, destination, navigate]);

  useEffect(() => {
    let live = true;
    api
      .health()
      .then((health) => {
        if (live) setRegistrationOpen(health.auth.registration_open === true);
      })
      .catch(() => {
        // An unreachable gateway takes no registrations either.
      });
    return () => {
      live = false;
    };
  }, []);

  const registering = mode === 'register';
  const ready = username.trim().length > 0 && password.length > 0;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !ready) return;
    setBusy(true);
    setError(null);
    const name = username.trim();
    try {
      const user = registering ? await register(name, password) : await login(name, password);
      rememberUsername(user.username);
      navigate(destination, { replace: true });
    } catch (err) {
      if (registering && err instanceof ApiError && err.status === 403) setRegistrationOpen(false);
      setError(registering ? registerErrorText(err) : signInErrorText(err));
      setPassword('');
    } finally {
      setBusy(false);
    }
  }

  function switchTo(next: Mode) {
    setMode(next);
    setError(null);
    setPassword('');
  }

  return (
    <div className="login">
      <form className="login-card card" onSubmit={onSubmit}>
        <div className="login-brand">
          <Mark size={26} />
          <h1>{strings.login.title}</h1>
        </div>
        <p className="hint">
          {registering ? strings.login.registerSubtitle : strings.login.subtitle}
        </p>

        <label className="label" htmlFor="rc-username">
          {strings.account.username}
        </label>
        <input
          id="rc-username"
          className="field"
          type="text"
          autoComplete="username"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder={strings.login.usernamePlaceholder}
          value={username}
          autoFocus={registering || username.length === 0}
          onChange={(e) => setUsername(e.target.value)}
        />

        <label className="label" htmlFor="rc-password">
          {strings.account.password}
        </label>
        <input
          id="rc-password"
          className="field"
          type="password"
          autoComplete={registering ? 'new-password' : 'current-password'}
          placeholder={strings.login.passwordPlaceholder}
          value={password}
          autoFocus={!registering && username.length > 0}
          onChange={(e) => setPassword(e.target.value)}
        />

        {error ? (
          <p className="login-error" role="alert">
            {error}
          </p>
        ) : null}

        <button type="submit" className="btn primary block" disabled={busy || !ready}>
          {busy
            ? registering
              ? strings.login.creating
              : strings.login.signingIn
            : registering
              ? strings.login.createAccount
              : strings.login.submit}
        </button>

        {registering ? (
          <button type="button" className="login-switch" onClick={() => switchTo('sign-in')}>
            {strings.login.signInInstead}
          </button>
        ) : registrationOpen ? (
          <button type="button" className="login-switch" onClick={() => switchTo('register')}>
            {strings.login.createAccountLink}
          </button>
        ) : null}
      </form>
    </div>
  );
}

/**
 * Only a path inside this app, never an absolute URL a link could supply. With
 * nothing to return to the destination is the root, so signing in lands by the
 * rule in `docs/DESIGN.md` rather than always on Sessions.
 */
function rememberedDestination(state: unknown): string {
  const from = (state as { from?: unknown } | null)?.from;
  if (typeof from !== 'string') return '/';
  if (!from.startsWith('/') || from.startsWith('//') || from === '/login') return '/';
  return from;
}

function signInErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return strings.login.rateLimited;
    if (err.status === 403) return strings.account.disabled;
    if (err.status === 401) return strings.login.failed;
    return err.message || strings.errors.generic;
  }
  return strings.login.unreachable;
}

function registerErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return strings.login.rateLimited;
    if (err.status === 409) return strings.account.taken;
    if (err.status === 403) return strings.login.registrationClosed;
    if (err.status === 400) return strings.account.rules;
    return err.message || strings.errors.generic;
  }
  return strings.login.unreachable;
}
