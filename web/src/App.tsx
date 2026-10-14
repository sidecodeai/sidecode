import { Suspense, lazy, useCallback, useEffect } from 'react';
import { Route, Routes, useLocation, useNavigate } from 'react-router';
import { AppLayout } from './layout/AppLayout';
import { Landing } from './layout/Landing';
import { LoginPage } from './features/login/LoginPage';
import { DevicePage } from './features/devices/DevicePage';
import { DevicesPage } from './features/devices/DevicesPage';
import { PairPage } from './features/devices/PairPage';
import { SessionsPage } from './features/sessions/SessionsPage';
import { ChatPage } from './features/chat/ChatPage';
import { SettingsPage } from './features/settings/SettingsPage';
import { UsersPage } from './features/users/UsersPage';
import { useAppBadge } from './push/useAppBadge';
import { useServiceWorkerNavigation } from './push/useServiceWorkerNavigation';
import { useAuth } from './stores/auth';
import { useConnection } from './stores/connection';
import { signOut } from './stores/signOut';
import { readSettingsFor, useSettings } from './stores/settings';

/**
 * A38: xterm.js is a third of a megabyte, and most opens of this app never
 * reach a terminal. The page is fetched when a device row is tapped, so the
 * boot bundle is the one it was before the emulator arrived.
 */
const TerminalPage = lazy(() =>
  import('./features/devices/TerminalPage').then((m) => ({ default: m.TerminalPage })),
);

export function App() {
  const status = useAuth((s) => s.status);
  const username = useAuth((s) => s.username);
  const check = useAuth((s) => s.check);
  const markSignedOut = useAuth((s) => s.markSignedOut);
  const connect = useConnection((s) => s.connect);
  const navigate = useNavigate();
  const location = useLocation();
  // Every screen reads `strings`, which is a view on the table this names. The
  // routes carry it as their key so the words change on every open screen at
  // once, while the socket and the session probe above stay untouched.
  const language = useSettings((s) => s.language);

  useServiceWorkerNavigation();
  useAppBadge();

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  useEffect(() => {
    void check();
  }, [check]);

  // A24: what this app remembers is the signed-in account's, not the browser's,
  // so two people who share one browser keep their own choices.
  useEffect(() => {
    readSettingsFor(username);
  }, [username]);

  // A revoked session and the Sign out button end the same way: the socket is
  // closed and every store the account wrote to is emptied.
  const onUnauthorized = useCallback(() => {
    markSignedOut();
    signOut();
  }, [markSignedOut]);

  useEffect(() => {
    if (status === 'signed-in') connect(onUnauthorized);
    if (status === 'signed-out') signOut();
  }, [status, connect, onUnauthorized]);

  useEffect(() => {
    if (status === 'signed-out' && location.pathname !== '/login') {
      // The hash carries the claim token of a `/pair` link (A23), so the
      // destination the login page returns to has to keep it.
      const from = `${location.pathname}${location.search}${location.hash}`;
      navigate('/login', { replace: true, state: { from } });
    }
  }, [status, location.pathname, location.search, location.hash, navigate]);

  if (status === 'unknown') return <div className="boot" aria-busy="true" />;

  // Signed out, every path is the login screen. Rendering the requested page
  // first and redirecting from an effect would mount it for one pass, and its
  // mount effects would fire authenticated requests that can only 401.
  if (status === 'signed-out') {
    return (
      <Routes key={language}>
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }

  return (
    <Routes key={language}>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
      {/* A38: a shell on one device, full screen — the emulator wants the page. */}
      <Route
        path="/devices/:deviceId/terminal"
        element={
          <Suspense fallback={<div className="boot" aria-busy="true" />}>
            <TerminalPage />
          </Suspense>
        }
      />
      <Route element={<AppLayout />}>
        <Route path="/devices" element={<DevicesPage />} />
        {/* A33: one device, its agents, how each is signed in and its quota. */}
        <Route path="/devices/:deviceId" element={<DevicePage />} />
        <Route path="/pair" element={<PairPage />} />
        <Route path="/sessions" element={<SessionsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/users" element={<UsersPage />} />
      </Route>
      <Route path="/" element={<Landing />} />
      <Route path="*" element={<Landing />} />
    </Routes>
  );
}
