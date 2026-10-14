/**
 * The Settings screen, `docs/DESIGN.md` § "The Settings screen": a header
 * saying who and where, four groups of two-line rows with no rule between them,
 * a state that speaks in its own row, and the versions as one caption line.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { SettingsPage } from '../src/features/settings/SettingsPage';
import { connectionTone } from '../src/features/settings/connectionTone';
import { api, type ConfigResponse } from '../src/lib/api';
import { gatewayHost, initials } from '../src/lib/identity';
import { useAuth } from '../src/stores/auth';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';

beforeEach(() => {
  useSettings.setState({ language: 'en' });
  useAuth.setState({ status: 'signed-in', username: 'admin', role: 'admin' });
});

afterEach(() => {
  useAuth.setState({ config: null, version: null, protocol: null });
  useConnection.setState({ status: 'closed', gatewayVersion: null, protocol: null });
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <SettingsPage />
    </MemoryRouter>,
  );

const captions = () => [...document.querySelectorAll('.group-title')].map((el) => el.textContent);

/** What `GET /api/config` answers, with only the part a test cares about set. */
const config = (over: Partial<ConfigResponse> = {}): ConfigResponse => ({
  public_origin: 'https://rc.example.com',
  stt: { enabled: false, languages: ['auto'] },
  push: { web_enabled: true, apns_enabled: true },
  version: '1.5.1',
  ...over,
});

/**
 * The header replaces the rows Signed in as, Connection, Gateway and Gateway
 * version: the username, then `role · host` with the connection's dot before
 * the host. The dot's word is never printed — it is read aloud and shown on
 * hover.
 */
describe('settings: the header', () => {
  it('says the username, the role word and the host without its scheme', () => {
    useAuth.setState({
      username: 'alice',
      role: 'member',
      config: config(),
    });
    renderPage();

    expect(screen.getByRole('heading', { name: 'alice' })).toBeInTheDocument();
    expect(screen.getByText('Member')).toBeInTheDocument();
    const host = document.querySelector('.settings-identity-host');
    expect(host?.textContent).toBe('rc.example.com');
    expect(host).toHaveAttribute('title', 'rc.example.com');
    expect(document.querySelector('.settings-identity .avatar')?.textContent).toBe('AL');
  });

  it('carries the connection as a dot whose word is its label alone', () => {
    useConnection.setState({ status: 'open' });
    const open = renderPage();

    const dot = screen.getByRole('img', { name: strings.settings.connected });
    expect(dot).toHaveClass('dot', 'working');
    expect(dot).toHaveAttribute('title', strings.settings.connected);
    expect(screen.queryByText(strings.settings.connected)).toBeNull();

    open.unmount();
    useConnection.setState({ status: 'closed' });
    renderPage();

    expect(screen.getByRole('img', { name: strings.settings.offline })).toHaveClass('dot', 'off');
  });

  it('takes its tone from the socket and nothing else', () => {
    expect(connectionTone('open')).toBe('working');
    expect(connectionTone('connecting')).toBe('waiting');
    expect(connectionTone('reconnecting')).toBe('waiting');
    expect(connectionTone('idle')).toBe('off');
    expect(connectionTone('closed')).toBe('off');
  });

  it('reads a username and an origin the same way the top bar does', () => {
    expect(initials('admin')).toBe('AD');
    expect(initials('j.gao')).toBe('JG');
    expect(initials('李雷')).toBe('李');
    expect(gatewayHost('https://rc.example.com')).toBe('rc.example.com');
    expect(gatewayHost('http://localhost:8787/')).toBe('localhost:8787');
    expect(gatewayHost('rc.example.com:8443')).toBe('rc.example.com:8443');
  });
});

/**
 * Four groups, in the order the ruling gives them, and nothing else: the About
 * group and the footnotes are gone, and every row carries its own sentence.
 */
describe('settings: the groups', () => {
  it('names the four groups in order and no others', () => {
    renderPage();

    expect(captions()).toEqual([
      strings.settings.account,
      strings.settings.whileAway,
      strings.settings.voice,
      strings.settings.reading,
    ]);
    for (const gone of ['About', 'Notifications', 'Sessions', 'Timeline']) {
      expect(captions()).not.toContain(gone);
    }
  });

  it('gives every row a sentence, and hangs no note under a group', () => {
    renderPage();

    const rows = [...document.querySelectorAll('.settings-row')];
    expect(rows.length).toBeGreaterThan(5);
    for (const row of rows) {
      expect(row.querySelector('.settings-row-title')?.textContent).toBeTruthy();
      expect(row.querySelector('.settings-row-sentence')?.textContent).toBeTruthy();
    }
    expect(document.querySelector('.settings-note')).toBeNull();
  });

  it('parts two rows by spacing alone, with no rule and no hairline', () => {
    const css = readFileSync(resolve('src/features/settings/settings.css'), 'utf8');

    expect(css).not.toMatch(/border-top/);
    expect(css).not.toMatch(/--hairline/);
    expect(css).not.toMatch(/settings-note/);
    expect(css).toContain('--row-h-setting');
  });
});

/**
 * A24: Users belongs to the admin role, a password to whoever has one — the
 * gateway refuses the change for the built-in `admin` alone — and Sign out asks
 * before it takes anything away.
 */
describe('settings: the account', () => {
  it('offers a member its own password and no accounts screen', () => {
    useAuth.setState({ username: 'alice', role: 'member' });
    renderPage();

    expect(
      screen.getByRole('button', { name: strings.settings.changePassword }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: strings.users.title })).not.toBeInTheDocument();
    expect(screen.getByText(strings.settings.changePasswordNote)).toBeInTheDocument();
  });

  it('offers the built-in admin the accounts screen and not a password it cannot change', () => {
    renderPage();

    expect(screen.getByRole('button', { name: strings.users.title })).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: strings.settings.changePassword }),
    ).not.toBeInTheDocument();
  });

  it('offers a second admin account both rows', () => {
    useAuth.setState({ username: 'dana', role: 'admin' });
    renderPage();

    expect(screen.getByRole('button', { name: strings.users.title })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: strings.settings.changePassword })).toBeInTheDocument();
  });

  it('asks before signing out, and signs out only on the confirmation', async () => {
    const user = userEvent.setup();
    const logout = vi.fn().mockResolvedValue(undefined);
    useAuth.setState({ logout });
    renderPage();

    await user.click(screen.getByRole('button', { name: strings.settings.signOut }));
    expect(screen.getByRole('dialog')).toHaveTextContent(strings.settings.signOutConfirm);
    expect(screen.getByRole('dialog')).toHaveTextContent(strings.settings.signOutNote);
    expect(logout).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: strings.common.cancel }));
    expect(logout).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: strings.settings.signOut }));
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: strings.settings.signOut }));
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
  });

  it("posts a member's password change to /api/password", async () => {
    const user = userEvent.setup();
    useAuth.setState({ username: 'alice', role: 'member' });
    const calls: { path: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          path: new URL(String(input), 'http://gateway.test').pathname,
          body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
        });
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }),
    );
    renderPage();

    await user.click(screen.getByRole('button', { name: strings.settings.changePassword }));
    await user.type(screen.getByLabelText(strings.account.currentPassword), 'oldoldold');
    await user.type(screen.getByLabelText(strings.account.newPassword), 'devdevdev');
    await user.click(screen.getByRole('button', { name: strings.common.save }));

    await waitFor(() => expect(calls.find((c) => c.path === '/api/password')).toBeDefined());
    expect(calls.find((c) => c.path === '/api/password')?.body).toEqual({
      current_password: 'oldoldold',
      new_password: 'devdevdev',
    });
    vi.unstubAllGlobals();
  });
});

/**
 * "While you're away": the notification that says a session needs you, and
 * A35's resume. Both are switches like the phone's, and each says in its own
 * row why it cannot be flipped.
 */
describe("settings: while you're away", () => {
  afterEach(() => {
    usePreferences.setState({ preferences: undefined });
    vi.restoreAllMocks();
  });

  it('draws notifications as a switch, disabled with the reason this browser has none', async () => {
    renderPage();

    const toggle = screen.getByRole('switch', { name: strings.settings.notify });
    await waitFor(() => expect(toggle).toBeDisabled());
    expect(screen.getByText(strings.settings.pushUnsupported)).toBeInTheDocument();
    expect(screen.queryByText(strings.settings.notifyNote)).toBeNull();
  });

  it('says so in the row when the gateway serves no web push', () => {
    useAuth.setState({ config: config({ push: { web_enabled: false, apns_enabled: true } }) });
    renderPage();

    expect(screen.getByText(strings.settings.pushServerDisabled)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: strings.settings.notify })).toBeDisabled();
    expect(screen.queryByText(strings.settings.pushUnsupported)).toBeNull();
  });

  it('draws the resume switch off with its sentence under it', () => {
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    renderPage();

    const toggle = screen.getByRole('switch', { name: strings.settings.resumeAfterLimit });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(toggle).toBeEnabled();
    expect(screen.getByText(strings.settings.resumeAfterLimitNote)).toBeInTheDocument();
  });

  it('draws it on when the account has turned it on', () => {
    usePreferences.setState({ preferences: { resume_after_limit: true } });
    renderPage();

    expect(screen.getByRole('switch', { name: strings.settings.resumeAfterLimit })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });

  it('writes the change to the account, not to this browser', async () => {
    const user = userEvent.setup();
    const patch = vi
      .spyOn(api, 'setPreferences')
      .mockResolvedValue({ preferences: { resume_after_limit: true } });
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    renderPage();

    await user.click(screen.getByRole('switch', { name: strings.settings.resumeAfterLimit }));

    expect(patch).toHaveBeenCalledWith({ resume_after_limit: true });
    await waitFor(() =>
      expect(usePreferences.getState().preferences?.resume_after_limit).toBe(true),
    );
  });

  it('puts the switch back and says so in the row when the gateway refuses', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'setPreferences').mockRejectedValue(new Error('nope'));
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    renderPage();

    await user.click(screen.getByRole('switch', { name: strings.settings.resumeAfterLimit }));

    await waitFor(() => expect(screen.getByText(strings.errors.setFailed)).toBeInTheDocument());
    expect(screen.queryByText(strings.settings.resumeAfterLimitNote)).toBeNull();
    expect(screen.getByRole('switch', { name: strings.settings.resumeAfterLimit })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('disables it with its reason on a gateway that does not offer it', () => {
    usePreferences.setState({ preferences: undefined });
    renderPage();

    expect(screen.getByRole('switch', { name: strings.settings.resumeAfterLimit })).toBeDisabled();
    expect(screen.getByText(strings.settings.resumeUnavailable)).toBeInTheDocument();
    expect(screen.queryByText(strings.settings.resumeAfterLimitNote)).toBeNull();
  });
});

/**
 * A29 — the Voice group: who transcribes dictation, and whether a model tidies
 * it up. A gateway with neither keeps both rows and says so in them. A44: the
 * web only transcribes on the gateway, which detects the language, so there is
 * no language to choose.
 */
describe('settings: voice', () => {
  const models = [
    { id: 'gpt-4.1-mini', label: 'gpt-4.1-mini' },
    { id: 'gpt-4.1', label: 'gpt-4.1' },
  ];

  const gateway = (polishEnabled: boolean) =>
    useConnection.setState({
      stt: { enabled: true },
      polish: { enabled: polishEnabled },
    });

  /** The group's rows by their titles, in the order they are drawn. */
  const voiceRows = () => {
    const group = [...document.querySelectorAll('.settings-section')].find(
      (section) => section.querySelector('.group-title')?.textContent === strings.settings.voice,
    );
    return [...(group?.querySelectorAll('.settings-row-title') ?? [])].map((el) => el.textContent);
  };

  const transcribeRow = () =>
    screen.getByText(strings.settings.transcribe).closest('.settings-row') as HTMLElement;

  const answerModels = (status = 200) =>
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify(
              status === 200 ? { models } : { error: { code: 'upstream', message: 'provider' } },
            ),
            { status, headers: { 'content-type': 'application/json' } },
          ),
      ),
    );

  beforeEach(() => {
    useSettings.setState({ polishEnabled: false, polishModel: '', polishStrength: 'moderate' });
    answerModels();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useConnection.setState({
      stt: { enabled: false },
      polish: { enabled: false },
    });
  });

  it('opens on Transcribe, which names the gateway and offers nothing to choose', () => {
    gateway(false);
    renderPage();

    expect(voiceRows()).toEqual([strings.settings.transcribe, strings.settings.polish]);
    const row = transcribeRow();
    expect(within(row).getByText(strings.settings.transcribeGateway)).toBeInTheDocument();
    expect(within(row).getByText(strings.settings.transcribeNote)).toBeInTheDocument();
    // No menu, no switch, and the row is not a target: there is no choice.
    expect(within(row).queryByRole('button')).toBeNull();
    expect(row).not.toHaveClass('target');
  });

  it('keeps the Transcribe row on a gateway with no speech-to-text, and says so in it', () => {
    renderPage();

    expect(captions()).toContain(strings.settings.voice);
    expect(voiceRows()).toEqual([strings.settings.transcribe, strings.settings.polish]);
    const row = transcribeRow();
    expect(within(row).getByText(strings.settings.voiceServerDisabled)).toBeInTheDocument();
    expect(within(row).queryByText(strings.settings.transcribeNote)).toBeNull();
    expect(within(row).getByText(strings.settings.transcribeGateway)).toBeInTheDocument();
  });

  it('offers the switch and, once it is on, the model and the strength', async () => {
    const user = userEvent.setup();
    gateway(true);
    renderPage();

    const polish = screen.getByRole('switch', { name: strings.settings.polish });
    expect(polish).toBeEnabled();
    expect(polish).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(strings.settings.polishNote)).toBeInTheDocument();
    // Nothing to choose while it is off.
    expect(screen.queryByRole('button', { name: strings.settings.polishModel })).toBeNull();

    await user.click(polish);

    expect(useSettings.getState().polishEnabled).toBe(true);
    // The first model the gateway listed is chosen, so the switch is enough.
    await waitFor(() => expect(useSettings.getState().polishModel).toBe('gpt-4.1-mini'));
    expect(screen.getByRole('button', { name: strings.settings.polishModel })).toHaveTextContent(
      'gpt-4.1-mini',
    );
    expect(screen.getByText(strings.settings.polishModelNote)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: strings.settings.polishModel }));
    await user.click(screen.getByRole('option', { name: 'gpt-4.1' }));
    expect(useSettings.getState().polishModel).toBe('gpt-4.1');

    await user.click(screen.getByRole('button', { name: strings.settings.polishStrong }));
    expect(useSettings.getState().polishStrength).toBe('strong');
  });

  it('keeps the menu and says one line in the row when the model list cannot be read', async () => {
    const user = userEvent.setup();
    gateway(true);
    useSettings.setState({ polishEnabled: true, polishModel: 'gpt-4.1' });
    answerModels(502);
    renderPage();

    await waitFor(() =>
      expect(screen.getByText(strings.settings.polishModelsFailed)).toBeInTheDocument(),
    );
    expect(screen.queryByText(strings.settings.polishModelNote)).toBeNull();
    const select = screen.getByRole('button', { name: strings.settings.polishModel });
    expect(select).toHaveTextContent('gpt-4.1');
    await user.click(select);
    expect(screen.getByRole('listbox', { name: strings.settings.polishModel })).toBeInTheDocument();
  });

  it('disables the switch and says why in the row when the gateway has no polish model', () => {
    gateway(false);
    renderPage();

    expect(screen.getByRole('switch', { name: strings.settings.polish })).toBeDisabled();
    expect(screen.getByText(strings.settings.polishServerDisabled)).toBeInTheDocument();
    expect(screen.queryByText(strings.settings.polishNote)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
});

/**
 * Reading: two choices each, so both are segmented controls in the row rather
 * than menus. `docs/DESIGN.md` § "The three screens" for the language itself.
 */
describe('settings: reading', () => {
  it('starts in English and at Simple, before anything is rendered', () => {
    expect(useSettings.getState().language).toBe('en');
    expect(useSettings.getState().timelineDetail).toBe('simple');
  });

  it('draws both choices as segments, with their sentences', () => {
    renderPage();

    const language = screen.getByRole('group', { name: strings.settings.language });
    const detail = screen.getByRole('group', { name: strings.settings.timelineDetail });
    expect([...language.querySelectorAll('button')].map((b) => b.textContent)).toEqual([
      'English',
      '中文',
    ]);
    expect([...detail.querySelectorAll('button')].map((b) => b.textContent)).toEqual([
      'Simple',
      'Detailed',
    ]);
    expect(screen.getByText(strings.settings.languageNote)).toBeInTheDocument();
    expect(screen.getByText(strings.settings.timelineDetailNote)).toBeInTheDocument();
  });

  it('writes the detail level the reader picks', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: 'Detailed' }));

    expect(useSettings.getState().timelineDetail).toBe('detailed');
    expect(screen.getByRole('button', { name: 'Detailed' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('writes the language the reader picks', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: '中文' }));

    expect(useSettings.getState().language).toBe('zh-Hans');
  });

  // The app remounts its routes on a language change (`App.tsx`), so what the
  // screen owes is that it is drawn in the language it is given.
  it('says the screen and its group captions in 中文', () => {
    useSettings.setState({ language: 'zh-Hans' });
    renderPage();

    expect(screen.getByRole('heading', { name: '设置' })).toBeInTheDocument();
    expect(captions()).toEqual(['账户', '你不在时', '语音', '阅读']);
    expect(
      screen.getByText('缓存的会话和草稿将从此设备移除，你的机器不受影响。'),
    ).toBeInTheDocument();
  });
});

/** The line that closes the screen, from the running code and never typed. */
describe('settings: the versions line', () => {
  it('says the gateway and the protocol the connection reported', () => {
    useConnection.setState({ status: 'open', gatewayVersion: '1.5.1', protocol: 1 });
    renderPage();

    const line = document.querySelector('.settings-versions');
    expect(line?.textContent).toContain('1.5.1');
    expect(line?.textContent).toContain('v1');
    expect(line?.textContent).toBe(strings.settings.versions('1.5.1', 'v1'));
  });

  it('falls back to what /api/config answered before the socket said hello', () => {
    useAuth.setState({ version: '1.4.9', protocol: 1 });
    renderPage();

    expect(document.querySelector('.settings-versions')?.textContent).toBe(
      strings.settings.versions('1.4.9', 'v1'),
    );
  });
});

/**
 * A41 — the six Settings values are the account's, and nothing on the screen
 * says "syncing": a change made on the phone moves the control in place while
 * the screen is open. `docs/DESIGN.md` § "Paused by the usage limit" →
 * "Settings are the account's, not the device's".
 */
describe('settings: a change another app of the account made', () => {
  afterEach(() => {
    usePreferences.getState().reset();
    useConnection.setState({ polish: { enabled: false } });
  });

  it('moves the polish switch where the reader is looking', () => {
    useConnection.setState({ polish: { enabled: true } });
    useSettings.setState({ polishEnabled: false });
    renderPage();
    expect(screen.getByRole('switch', { name: strings.settings.polish })).toHaveAttribute(
      'aria-checked',
      'false',
    );

    act(() =>
      usePreferences.getState().apply({ resume_after_limit: false, polish_enabled: true }),
    );

    expect(screen.getByRole('switch', { name: strings.settings.polish })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });

  it('moves the timeline detail segment the same way', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    renderPage();

    act(() =>
      usePreferences.getState().apply({ resume_after_limit: false, timeline_detail: 'detailed' }),
    );

    expect(screen.getByRole('button', { name: 'Detailed' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });
});
