/**
 * Settings, `docs/DESIGN.md` § "The Settings screen": one page of sections,
 * each at an address of its own, drawn as a nav beside the section on show on
 * a wide screen and as a list of sections on a narrow one — by the routes and
 * the stylesheet, never by reading the window's width. Every row is two lines
 * with no rule between rows, a state speaks in its own row, and the versions
 * close the footer caption.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { connectionTone } from '../src/features/settings/connectionTone';
import { api, type ConfigResponse } from '../src/lib/api';
import { gatewayHost, initials } from '../src/lib/identity';
import { useAuth } from '../src/stores/auth';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import {
  address,
  captions,
  currentNavItem,
  listedSections,
  navItems,
  renderSettings,
  rowTitles,
  versionParts,
} from './settingsScreen';
import { labelled } from './choice';

beforeEach(() => {
  useSettings.setState({ language: 'en' });
  useAuth.setState({ status: 'signed-in', username: 'admin', role: 'admin' });
});

afterEach(() => {
  useAuth.setState({ config: null, version: null, protocol: null });
  useConnection.setState({ status: 'closed', gatewayVersion: null, protocol: null });
});

/** What `GET /api/config` answers, with only the part a test cares about set. */
const config = (over: Partial<ConfigResponse> = {}): ConfigResponse => ({
  public_origin: 'https://rc.example.com',
  stt: { enabled: false, languages: ['auto'] },
  push: { web_enabled: true, apns_enabled: true },
  version: '1.5.1',
  ...over,
});

const s = strings.settings;
const SECTIONS = ['general', 'voice', 'account'] as const;

/** The groups of the section on show, each by its caption (or none) and its rows. */
const groups = () =>
  [...document.querySelectorAll('.settings-content .settings-group')].map((group) => ({
    caption: group.querySelector('.group-title')?.textContent ?? null,
    rows: rowTitles(group),
  }));

/**
 * General, Voice and Account. On a wide screen the nav names them beside the
 * one on show, General at `/settings`; on a narrow one `/settings` is their
 * list.
 */
describe('settings: the sections', () => {
  it('names General, Voice and Account in the nav and in the narrow list, in that order', () => {
    renderSettings('/settings');

    expect(navItems()).toEqual([s.general, s.voice, s.account]);
    expect(listedSections()).toEqual([s.general, s.voice, s.account]);
    const list = document.querySelector('.settings-list') as HTMLElement;
    for (const [id, name, note] of [
      ['general', s.general, s.generalNote],
      ['voice', s.voice, s.voiceNote],
      ['account', s.account, s.accountNote],
    ]) {
      const row = within(list).getByRole('link', { name });
      expect(row).toHaveAttribute('href', `/settings/${id}`);
      expect(row).toHaveAccessibleDescription(note);
      expect(row.querySelector('.settings-row-chevron')).not.toBeNull();
      expect(row.querySelector('.settings-row-icon svg')).not.toBeNull();
    }
  });

  it('opens the list with who is signed in, which leads to Account', () => {
    useAuth.setState({ username: 'alice', role: 'member', config: config() });
    renderSettings('/settings');

    const list = document.querySelector('.settings-list') as HTMLElement;
    const identity = list.firstElementChild?.querySelector('a') as HTMLElement;
    expect(identity).toHaveAttribute('href', '/settings/account');
    expect(identity.querySelector('.settings-identity-name')).toHaveTextContent('alice');
    expect(identity).toHaveTextContent('Member');
    expect(identity).toHaveTextContent('rc.example.com');
  });

  it('shows General at /settings, marked current in the nav, under the page title Settings', () => {
    renderSettings('/settings');

    expect(screen.getByRole('heading', { level: 1, name: s.title })).toBeInTheDocument();
    expect(currentNavItem()).toBe(s.general);
    expect(captions()).toEqual([s.reading, s.whileAway]);
  });

  it.each(SECTIONS)('gives %s its address, its mark in the nav, its title and a way back', (id) => {
    const name = { general: s.general, voice: s.voice, account: s.account }[id];
    renderSettings(`/settings/${id}`);

    expect(currentNavItem()).toBe(name);
    expect(screen.getByRole('link', { name })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: s.title })).toHaveAttribute('href', '/settings');
    // A section stands alone: no list of sections beside it.
    expect(listedSections()).toEqual([]);
  });

  it('puts the nav, the section and the footer caption in the page in that order', () => {
    renderSettings('/settings/voice');

    const layout = document.querySelector('.settings-layout') as HTMLElement;
    expect([...layout.children].map((child) => child.className)).toEqual([
      'settings-nav',
      'settings-content',
      'settings-foot',
    ]);
    expect(layout.style.getPropertyValue('--settings-nav-items')).toBe('3');
  });

  it('moves between sections from the nav', async () => {
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(within(screen.getByRole('navigation', { name: s.title })).getByRole('link', { name: s.voice }));

    expect(address()).toBe('/settings/voice');
    expect(currentNavItem()).toBe(s.voice);
    expect(screen.getByRole('switch', { name: s.polish })).toBeInTheDocument();
  });

  it('takes an address that names no section to /settings', async () => {
    renderSettings('/settings/nowhere');
    await waitFor(() => expect(address()).toBe('/settings'));
  });

  it('decides the wide and the narrow shape in the stylesheet, at the top bar’s breakpoint, and never in script', () => {
    const css = readFileSync(resolve('src/features/settings/settings.css'), 'utf8');
    const narrow = css.slice(css.indexOf('@media (max-width: 760px)'));

    expect(narrow).not.toBe(css);
    expect(css).toMatch(/\.settings-list \{\s*display: none;/);
    expect(narrow).toMatch(/\.settings-nav,\s*\.settings\.at-section \.settings-foot \{\s*display: none;/);
    // Nothing is drawn out of the order it is read in.
    expect(css).not.toMatch(/(^|[\s;{])order\s*:/m);
    expect(narrow).toMatch(/\.settings-list \{\s*display: flex;/);
    expect(narrow).toMatch(/\.settings-index-general \{\s*display: none;/);
    const dir = resolve('src/features/settings');
    for (const file of readdirSync(dir).filter((name) => /\.tsx?$/.test(name))) {
      expect(readFileSync(join(dir, file), 'utf8')).not.toMatch(/matchMedia|innerWidth/);
    }
  });
});

/** Every row in every section carries its own sentence, and no rule parts two rows. */
describe('settings: the rows', () => {
  it.each(SECTIONS)('gives every row in %s a title and a sentence, and hangs no note under a group', (id) => {
    useAuth.setState({ username: 'dana', role: 'admin' });
    renderSettings(`/settings/${id}`);

    const rows = [...document.querySelectorAll('.settings-row')];
    expect(rows.length).toBeGreaterThan(1);
    for (const row of rows) {
      expect(row.querySelector('.settings-row-title')?.textContent).toBeTruthy();
      expect(row.querySelector('.settings-row-sentence')?.textContent).toBeTruthy();
    }
    expect(document.querySelector('.settings-note')).toBeNull();
  });

  it('parts two rows by spacing alone, with no rule and no hairline, on a row that is a floor', () => {
    const css = readFileSync(resolve('src/features/settings/settings.css'), 'utf8');

    expect(css).not.toMatch(/border-top/);
    expect(css).not.toMatch(/--hairline/);
    expect(css).not.toMatch(/settings-note/);
    expect(css).toContain('min-height: var(--row-h-setting)');
  });
});

/**
 * The header at the head of Account replaces the rows Signed in as,
 * Connection, Gateway and Gateway version: the username, then `role · host`
 * with the connection's dot before the host. The dot's word is never printed —
 * it is read aloud and shown on hover.
 */
describe('settings: the header', () => {
  const header = () => document.querySelector('header.settings-identity') as HTMLElement;

  it('says the username, the role word and the host without its scheme', () => {
    useAuth.setState({ username: 'alice', role: 'member', config: config() });
    renderSettings('/settings/account');

    expect(header().querySelector('.settings-identity-name')?.textContent).toBe('alice');
    expect(within(header()).getByText('Member')).toBeInTheDocument();
    const host = header().querySelector('.settings-identity-host');
    expect(host?.textContent).toBe('rc.example.com');
    expect(host).toHaveAttribute('title', 'rc.example.com');
    expect(header().querySelector('.avatar')?.textContent).toBe('AL');
    // It opens the section, on the canvas rather than on a surface.
    expect(header().closest('.surface')).toBeNull();
  });

  it('carries the connection as a dot whose word is its label alone', () => {
    useConnection.setState({ status: 'open' });
    const open = renderSettings('/settings/account');

    const dot = within(header()).getByRole('img', { name: s.connected });
    expect(dot).toHaveClass('dot', 'working');
    expect(dot).toHaveAttribute('title', s.connected);
    expect(screen.queryByText(s.connected)).toBeNull();

    open.unmount();
    useConnection.setState({ status: 'closed' });
    renderSettings('/settings/account');

    expect(within(header()).getByRole('img', { name: s.offline })).toHaveClass('dot', 'off');
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
 * A24: Users belongs to the admin role, a password to whoever has one — the
 * gateway refuses the change for the built-in `admin` alone — and Sign out asks
 * before it takes anything away.
 */
describe('settings: the account', () => {
  it('holds a member’s password under Sign-in, and signing out apart', () => {
    useAuth.setState({ username: 'alice', role: 'member' });
    renderSettings('/settings/account');

    expect(groups()).toEqual([
      { caption: s.signInGroup, rows: [s.changePassword] },
      { caption: null, rows: [s.signOut] },
    ]);
    expect(screen.getByText(s.changePasswordNote)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: strings.users.title })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: s.signOut }).querySelector('.settings-row-title')).toHaveClass('danger');
  });

  it('offers the built-in admin the accounts screen and no Sign-in, since it has no password here to change', () => {
    renderSettings('/settings/account');

    expect(groups()).toEqual([{ caption: null, rows: [strings.users.title, s.signOut] }]);
    expect(captions()).toEqual([]);
    expect(screen.queryByRole('button', { name: s.changePassword })).not.toBeInTheDocument();
  });

  it('offers a second admin account every row, the password alone under Sign-in', () => {
    useAuth.setState({ username: 'dana', role: 'admin' });
    renderSettings('/settings/account');

    expect(groups()).toEqual([
      { caption: s.signInGroup, rows: [s.changePassword] },
      { caption: null, rows: [strings.users.title, s.signOut] },
    ]);
  });

  it('has one Change password in the whole of Settings', () => {
    useAuth.setState({ username: 'dana', role: 'admin' });
    let found = 0;
    for (const path of ['/settings', ...SECTIONS.map((id) => `/settings/${id}`)]) {
      const view = renderSettings(path);
      found += screen.queryAllByRole('button', { name: s.changePassword }).length;
      found += screen.queryAllByText(s.changePassword).filter((el) => !el.closest('button')).length;
      view.unmount();
    }
    expect(found).toBe(1);
  });

  it('leads Users to the accounts screen', async () => {
    const user = userEvent.setup();
    renderSettings('/settings/account');

    await user.click(screen.getByRole('button', { name: strings.users.title }));
    expect(address()).toBe('/users');
  });

  it('asks before signing out, and signs out only on the confirmation', async () => {
    const user = userEvent.setup();
    const logout = vi.fn().mockResolvedValue(undefined);
    useAuth.setState({ logout });
    renderSettings('/settings/account');

    await user.click(screen.getByRole('button', { name: s.signOut }));
    expect(screen.getByRole('dialog')).toHaveTextContent(s.signOutConfirm);
    expect(screen.getByRole('dialog')).toHaveTextContent(s.signOutNote);
    expect(logout).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: strings.common.cancel }));
    expect(logout).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: s.signOut }));
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: s.signOut }));
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    expect(address()).toBe('/login');
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
    renderSettings('/settings/account');

    await user.click(screen.getByRole('button', { name: s.changePassword }));
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
 * General's second group, "While you're away": the notification that says a
 * session needs you, and A35's resume. Both are switches like the phone's, and
 * each says in its own row why it cannot be flipped.
 */
describe("settings: while you're away", () => {
  afterEach(() => {
    usePreferences.setState({ preferences: undefined });
    vi.restoreAllMocks();
  });

  it('draws notifications as a switch, disabled with the reason this browser has none', async () => {
    renderSettings('/settings/general');

    const toggle = screen.getByRole('switch', { name: s.notify });
    await waitFor(() => expect(toggle).toBeDisabled());
    expect(screen.getByText(s.pushUnsupported)).toBeInTheDocument();
    expect(screen.queryByText(s.notifyNote)).toBeNull();
  });

  it('says so in the row when the gateway serves no web push', () => {
    useAuth.setState({ config: config({ push: { web_enabled: false, apns_enabled: true } }) });
    renderSettings('/settings/general');

    expect(screen.getByText(s.pushServerDisabled)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: s.notify })).toBeDisabled();
    expect(screen.queryByText(s.pushUnsupported)).toBeNull();
  });

  it('draws the resume switch off with its sentence under it', () => {
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    renderSettings('/settings/general');

    const toggle = screen.getByRole('switch', { name: s.resumeAfterLimit });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(toggle).toBeEnabled();
    expect(screen.getByText(s.resumeAfterLimitNote)).toBeInTheDocument();
  });

  it('draws it on when the account has turned it on', () => {
    usePreferences.setState({ preferences: { resume_after_limit: true } });
    renderSettings('/settings/general');

    expect(screen.getByRole('switch', { name: s.resumeAfterLimit })).toHaveAttribute('aria-checked', 'true');
  });

  it('writes the change to the account, not to this browser', async () => {
    const user = userEvent.setup();
    const patch = vi.spyOn(api, 'setPreferences').mockResolvedValue({ preferences: { resume_after_limit: true } });
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    renderSettings('/settings/general');

    await user.click(screen.getByRole('switch', { name: s.resumeAfterLimit }));

    expect(patch).toHaveBeenCalledWith({ resume_after_limit: true });
    await waitFor(() => expect(usePreferences.getState().preferences?.resume_after_limit).toBe(true));
  });

  it('puts the switch back and says so in the row when the gateway refuses', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'setPreferences').mockRejectedValue(new Error('nope'));
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    renderSettings('/settings/general');

    await user.click(screen.getByRole('switch', { name: s.resumeAfterLimit }));

    await waitFor(() => expect(screen.getByText(strings.errors.setFailed)).toBeInTheDocument());
    expect(screen.queryByText(s.resumeAfterLimitNote)).toBeNull();
    expect(screen.getByRole('switch', { name: s.resumeAfterLimit })).toHaveAttribute('aria-checked', 'false');
  });

  it('disables it with its reason on a gateway that does not offer it', () => {
    usePreferences.setState({ preferences: undefined });
    renderSettings('/settings/general');

    expect(screen.getByRole('switch', { name: s.resumeAfterLimit })).toBeDisabled();
    expect(screen.getByText(s.resumeUnavailable)).toBeInTheDocument();
    expect(screen.queryByText(s.resumeAfterLimitNote)).toBeNull();
  });
});

/**
 * A29 — Voice: who transcribes dictation, and whether a model tidies it up,
 * on one surface under no caption, since the section's title says Voice. A
 * gateway with neither keeps both rows and says so in them. A44: the web only
 * transcribes on the gateway, which detects the language, so there is no
 * language to choose.
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

  const voiceRows = () => rowTitles(document.querySelector('.settings-content') as HTMLElement);

  const transcribeRow = () => screen.getByText(s.transcribe).closest('.settings-row') as HTMLElement;

  const answerModels = (status = 200) =>
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify(status === 200 ? { models } : { error: { code: 'upstream', message: 'provider' } }),
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
    renderSettings('/settings/voice');

    expect(voiceRows()).toEqual([s.transcribe, s.polish]);
    expect(groups()).toEqual([{ caption: null, rows: [s.transcribe, s.polish] }]);
    const row = transcribeRow();
    expect(within(row).getByText(s.transcribeGateway)).toBeInTheDocument();
    expect(within(row).getByText(s.transcribeNote)).toBeInTheDocument();
    // No menu, no switch, and the row is not a target: there is no choice.
    expect(within(row).queryByRole('button')).toBeNull();
    expect(row).not.toHaveClass('target');
  });

  it('keeps the Transcribe row on a gateway with no speech-to-text, and says so in it', () => {
    renderSettings('/settings/voice');

    expect(voiceRows()).toEqual([s.transcribe, s.polish]);
    const row = transcribeRow();
    expect(within(row).getByText(s.voiceServerDisabled)).toBeInTheDocument();
    expect(within(row).queryByText(s.transcribeNote)).toBeNull();
    expect(within(row).getByText(s.transcribeGateway)).toBeInTheDocument();
  });

  it('offers the switch and, once it is on, the model and the strength', async () => {
    const user = userEvent.setup();
    gateway(true);
    renderSettings('/settings/voice');

    const polish = screen.getByRole('switch', { name: s.polish });
    expect(polish).toBeEnabled();
    expect(polish).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(s.polishNote)).toBeInTheDocument();
    // Nothing to choose while it is off.
    expect(screen.queryByRole('button', { name: labelled(s.polishModel) })).toBeNull();

    await user.click(polish);

    expect(useSettings.getState().polishEnabled).toBe(true);
    // The first model the gateway listed is chosen, so the switch is enough.
    await waitFor(() => expect(useSettings.getState().polishModel).toBe('gpt-4.1-mini'));
    expect(screen.getByRole('button', { name: labelled(s.polishModel) })).toHaveTextContent('gpt-4.1-mini');
    expect(screen.getByText(s.polishModelNote)).toBeInTheDocument();
    expect(voiceRows()).toEqual([s.transcribe, s.polish, s.polishModel, s.polishStrength]);

    await user.click(screen.getByRole('button', { name: labelled(s.polishModel) }));
    await user.click(screen.getByRole('option', { name: 'gpt-4.1' }));
    expect(useSettings.getState().polishModel).toBe('gpt-4.1');

    await user.click(screen.getByRole('button', { name: s.polishStrong }));
    expect(useSettings.getState().polishStrength).toBe('strong');
  });

  it('keeps the menu and says one line in the row when the model list cannot be read', async () => {
    const user = userEvent.setup();
    gateway(true);
    useSettings.setState({ polishEnabled: true, polishModel: 'gpt-4.1' });
    answerModels(502);
    renderSettings('/settings/voice');

    await waitFor(() => expect(screen.getByText(s.polishModelsFailed)).toBeInTheDocument());
    expect(screen.queryByText(s.polishModelNote)).toBeNull();
    // With no list to name it, the menu reads the model by the id it shows.
    const select = screen.getByRole('button', { name: strings.a11y.choice(s.polishModel, 'gpt-4.1') });
    expect(select).toHaveTextContent('gpt-4.1');
    await user.click(select);
    expect(screen.getByRole('listbox', { name: s.polishModel })).toBeInTheDocument();
  });

  it('disables the switch and says why in the row when the gateway has no polish model', () => {
    gateway(false);
    renderSettings('/settings/voice');

    expect(screen.getByRole('switch', { name: s.polish })).toBeDisabled();
    expect(screen.getByText(s.polishServerDisabled)).toBeInTheDocument();
    expect(screen.queryByText(s.polishNote)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
});

/**
 * General's first group, Reading: two choices each, so both are segmented
 * controls in the row rather than menus. `docs/DESIGN.md` § "The three
 * screens" for the language itself.
 */
describe('settings: reading', () => {
  it('starts in English and at Simple, before anything is rendered', () => {
    expect(useSettings.getState().language).toBe('en');
    expect(useSettings.getState().timelineDetail).toBe('simple');
  });

  it('draws both choices as segments, with their sentences, ahead of While you’re away', () => {
    renderSettings('/settings/general');

    expect(groups()).toEqual([
      { caption: s.reading, rows: [s.language, s.timelineDetail] },
      { caption: s.whileAway, rows: [s.notify, s.resumeAfterLimit] },
    ]);
    const language = screen.getByRole('group', { name: s.language });
    const detail = screen.getByRole('group', { name: s.timelineDetail });
    expect([...language.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['English', '中文']);
    expect([...detail.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Simple', 'Detailed']);
    expect(screen.getByText(s.languageNote)).toBeInTheDocument();
    expect(screen.getByText(s.timelineDetailNote)).toBeInTheDocument();
  });

  it('writes the detail level the reader picks', async () => {
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(screen.getByRole('button', { name: 'Detailed' }));

    expect(useSettings.getState().timelineDetail).toBe('detailed');
    expect(screen.getByRole('button', { name: 'Detailed' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('writes the language the reader picks', async () => {
    const user = userEvent.setup();
    renderSettings('/settings/general');

    await user.click(screen.getByRole('button', { name: '中文' }));

    expect(useSettings.getState().language).toBe('zh-Hans');
  });

  // The app remounts its routes on a language change (`App.tsx`), so what the
  // screen owes is that it is drawn in the language it is given.
  it('says the page, its sections and General’s captions in 中文', () => {
    useSettings.setState({ language: 'zh-Hans' });
    renderSettings('/settings');

    expect(screen.getByRole('heading', { level: 1, name: '设置' })).toBeInTheDocument();
    expect(navItems()).toEqual(['通用', '语音', '账户']);
    expect(listedSections()).toEqual(['通用', '语音', '账户']);
    expect(captions()).toEqual(['阅读', '你不在时']);
  });

  it('says Account in 中文', () => {
    useSettings.setState({ language: 'zh-Hans' });
    useAuth.setState({ username: 'alice', role: 'member' });
    renderSettings('/settings/account');

    expect(captions()).toEqual(['登录方式']);
    expect(screen.getByText('缓存的会话和草稿将从此设备移除，你的机器不受影响。')).toBeInTheDocument();
  });
});

/**
 * The footer caption under the nav, and closing the narrow list: the
 * versions, from the running code and never typed.
 */
describe('settings: the footer caption', () => {
  it('says the gateway and the protocol the connection reported, each part whole', () => {
    useConnection.setState({ status: 'open', gatewayVersion: '1.5.1', protocol: 1 });
    renderSettings('/settings');

    expect(document.querySelector('.settings-foot .settings-versions')).not.toBeNull();
    expect(versionParts()).toEqual(['Gateway 1.5.1', 'Protocol v1']);
    expect(versionParts().join(' · ')).toBe(s.versions('1.5.1', 'v1'));
  });

  it('falls back to what /api/config answered before the socket said hello', () => {
    useAuth.setState({ version: '1.4.9', protocol: 1 });
    renderSettings('/settings/voice');

    expect(versionParts().join(' · ')).toBe(s.versions('1.4.9', 'v1'));
  });

  it('says the versions in 中文 in the same parts', () => {
    useSettings.setState({ language: 'zh-Hans' });
    useConnection.setState({ gatewayVersion: '1.5.1', protocol: 1 });
    renderSettings('/settings');

    expect(versionParts()).toEqual(['网关 1.5.1', '协议 v1']);
  });

  it('keeps every versions part whole, a centred row on a narrow screen', () => {
    const css = readFileSync(resolve('src/features/settings/settings.css'), 'utf8');
    const narrow = css.slice(css.indexOf('@media (max-width: 760px)'), css.indexOf('@media (max-width: 640px)'));

    // A versions part never breaks inside, and its dot is the next part's, outside a line's edge.
    expect(css).toMatch(/\.settings-versions-part \{[^}]*white-space: nowrap;/);
    expect(css).toMatch(/\.settings-versions-part \+ \.settings-versions-part::before \{[^}]*content: '·';/);
    expect(css).toMatch(/\.settings-versions \{[^}]*overflow: hidden;/);
    // A narrow screen: one centred row.
    expect(narrow).toMatch(/\.settings-versions \{\s*justify-content: center;/);
  });

  it('carries the versions alone, and no links', () => {
    renderSettings('/settings');

    const foot = document.querySelector('.settings-foot') as HTMLElement;
    expect(foot.children).toHaveLength(1);
    expect(within(foot).queryAllByRole('link')).toEqual([]);
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
    renderSettings('/settings/voice');
    expect(screen.getByRole('switch', { name: s.polish })).toHaveAttribute('aria-checked', 'false');

    act(() => usePreferences.getState().apply({ resume_after_limit: false, polish_enabled: true }));

    expect(screen.getByRole('switch', { name: s.polish })).toHaveAttribute('aria-checked', 'true');
  });

  it('moves the timeline detail segment the same way', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    renderSettings('/settings/general');

    act(() => usePreferences.getState().apply({ resume_after_limit: false, timeline_detail: 'detailed' }));

    expect(screen.getByRole('button', { name: 'Detailed' })).toHaveAttribute('aria-pressed', 'true');
  });
});
