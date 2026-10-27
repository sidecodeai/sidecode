/**
 * A55 — renaming a session from the browser: when Rename is offered (the
 * agent's capability on its device, and the device online, never a version),
 * what stands between the text in the field and Save, the line a refusal
 * becomes, and the store's waiting `rename`, which draws nothing ahead of the
 * device's reply.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RequestError } from '../src/lib/ws';
import { en, stringTables } from '../src/strings';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import {
  NAME_LIMIT,
  canRename,
  checkName,
  codePointCount,
  deviceCanRename,
  keepToLimit,
  renameFailure,
  trimName,
} from '../src/features/sessions/rename';
import {
  claudeAgent,
  claudeNoShim,
  codexAgent,
  codexNoDaemon,
  devices,
  grokAgent,
  grokNoLeader,
  piAgent,
  sessions,
} from '../mock/fixtures';
import type { SessionResult } from '../src/protocol/frames';
import type { AgentInfo, Device, Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const session: Session = sessions.find((s) => s.session_id === 'ses-push')!;
const deviceNamed = (id: string): Device => devices.find((d) => d.device_id === id)!;

beforeEach(() => {
  rpcMock.mockReset();
  useSettings.setState({ language: 'en' });
  useSessions.setState({ sessions: { [keyOf(session)]: session }, loaded: true });
});

describe('when Rename is offered', () => {
  it('needs the capability on the agent and the device online', () => {
    expect(canRename(claudeAgent, true)).toBe(true);
    expect(canRename(claudeAgent, false)).toBe(false);
    expect(canRename({ ...claudeAgent, capabilities: ['history'] }, true)).toBe(false);
    expect(canRename(null, true)).toBe(false);
    expect(canRename(undefined, true)).toBe(false);
  });

  it('never reads a version: a device from before the amendment lists nothing', () => {
    const older: AgentInfo = {
      ...claudeAgent,
      version: '99.0.0',
      capabilities: claudeAgent.capabilities.filter((capability) => capability !== 'rename'),
    };
    expect(canRename(older, true)).toBe(false);
    expect(canRename({ ...older, version: '0.0.1', capabilities: ['rename'] }, true)).toBe(true);
  });

  it('is listed for Claude, Codex and pi, and for Grok only while its leader is ready', () => {
    for (const agent of [claudeAgent, claudeNoShim, codexAgent, codexNoDaemon, piAgent, grokAgent]) {
      expect(canRename(agent, true)).toBe(true);
    }
    expect(canRename(grokNoLeader, true)).toBe(false);
  });

  it('reads the session’s own agent on its own device', () => {
    const mac = deviceNamed('dev-mac');
    const ci = deviceNamed('dev-ci');
    const grok = sessions.find((s) => s.session_id === 'ses-grok-no-leader')!;

    expect(deviceCanRename(mac, session)).toBe(true);
    // Grok Build runs on both devices; only the one with the leader lists it.
    expect(deviceCanRename(ci, grok)).toBe(false);
    expect(deviceCanRename(mac, { ...grok, device_id: 'dev-mac' })).toBe(true);
    // An agent the device does not list at all.
    expect(deviceCanRename({ ...mac, agents: [] }, session)).toBe(false);
    // The device the row sits under is offline.
    expect(deviceCanRename({ ...mac, online: false }, session)).toBe(false);
  });
});

describe('the name in the field', () => {
  it('counts code points, as the device does', () => {
    expect(codePointCount('abc')).toBe(3);
    expect(codePointCount('😀'.repeat(60))).toBe(60);
    expect('😀'.repeat(60).length).toBe(120);
    expect(codePointCount('修复认证令牌')).toBe(6);
  });

  it('is refused empty, whatever white space it holds', () => {
    expect(checkName('', 'Old')).toBe('empty');
    expect(checkName('   \t ', 'Old')).toBe('empty');
  });

  it('is unchanged when its trimmed text is the current title', () => {
    expect(checkName('Old', 'Old')).toBe('unchanged');
    expect(checkName('  Old  ', 'Old')).toBe('unchanged');
    expect(checkName('Old', '  Old ')).toBe('unchanged');
    // An untitled session is empty, and typing anything is a change.
    expect(checkName('First name', '')).toBe('ok');
  });

  it('allows 60 code points and refuses 61, however many UTF-16 units they take', () => {
    expect(NAME_LIMIT).toBe(60);
    expect(checkName('a'.repeat(60), 'Old')).toBe('ok');
    expect(checkName('a'.repeat(61), 'Old')).toBe('too-long');
    expect(checkName('😀'.repeat(60), 'Old')).toBe('ok');
    expect(checkName('😀'.repeat(61), 'Old')).toBe('too-long');
  });

  it('counts the trimmed text, which is what Save sends', () => {
    expect(checkName(`  ${'a'.repeat(60)}  `, 'Old')).toBe('ok');
  });
});

describe('the name a text holds', () => {
  it('is the text trimmed the way the device trims it', () => {
    expect(trimName('  Push tokens  ')).toBe('Push tokens');
    expect(trimName('\t\n Push\u00a0tokens\u3000')).toBe('Push\u00a0tokens');
    // What comes between is the device's to normalise.
    expect(trimName('Push\u200btokens')).toBe('Push\u200btokens');
  });

  it('is nothing when a text is only space, control and zero-width characters', () => {
    const blank = ['', '   ', '\u200b', '\ufeff', '\u0085', '\u00ad', '\u2028', '\u200b\ufeff\u0085', '\ud800'];
    for (const text of blank) {
      expect(trimName(text), JSON.stringify(text)).toBe('');
      expect(checkName(text, 'Old'), JSON.stringify(text)).toBe('empty');
    }
  });

  it('trims zero-width characters off both ends, and not the joiners of an emoji sequence', () => {
    expect(trimName('\u200b\ufeffPush tokens\u200b')).toBe('Push tokens');
    expect(trimName('\u200b👩\u200d💻\u200b')).toBe('👩\u200d💻');
    expect(trimName('\u200d')).toBe('\u200d');
    expect(trimName('\u200cname')).toBe('\u200cname');
    expect(codePointCount(trimName('👩\u200d💻'))).toBe(3);
  });

  it('is what is compared with the current name', () => {
    expect(checkName('\u200bOld\u200b', 'Old')).toBe('unchanged');
    expect(checkName('Old', '\ufeffOld')).toBe('unchanged');
    expect(checkName('New', '\u200b')).toBe('ok');
  });
});

describe('keeping the field to the limit', () => {
  it('lets an edit through while the name is within 60 code points', () => {
    expect(keepToLimit('abc', 'ab')).toBe('abc');
    expect(keepToLimit('a'.repeat(60), 'a'.repeat(59))).toBe('a'.repeat(60));
    expect(keepToLimit('😀'.repeat(60), '')).toBe('😀'.repeat(60));
    expect(keepToLimit('', 'abc')).toBe('');
  });

  it('cuts an edit that would grow the name past it, at the end, by code points', () => {
    expect(keepToLimit('a'.repeat(61), 'a'.repeat(60))).toBe('a'.repeat(60));
    expect(keepToLimit('a'.repeat(200), '')).toBe('a'.repeat(60));
    // Not in the middle of an emoji: it is one code point and two UTF-16 units.
    expect(keepToLimit('😀'.repeat(61), '😀'.repeat(60))).toBe('😀'.repeat(60));
    expect(keepToLimit('字'.repeat(75), 'Old')).toBe('字'.repeat(60));
  });

  it('counts the name, which is the trimmed text, and keeps the space round it', () => {
    expect(keepToLimit(`  ${'a'.repeat(60)}  `, '')).toBe(`  ${'a'.repeat(60)}  `);
    expect(keepToLimit(`  ${'a'.repeat(61)}  `, '')).toBe(`  ${'a'.repeat(60)}`);
    // Space alone never grows the name.
    expect(keepToLimit(`${'a'.repeat(60)}${' '.repeat(30)}`, 'a'.repeat(60))).toBe(
      `${'a'.repeat(60)}${' '.repeat(30)}`,
    );
  });

  it('counts the name and not the zero-width characters round it', () => {
    expect(keepToLimit(`\u200b${'a'.repeat(60)}\u200b`, '')).toBe(`\u200b${'a'.repeat(60)}\u200b`);
    expect(keepToLimit(`\u200b${'a'.repeat(61)}\u200b`, '')).toBe(`\u200b${'a'.repeat(60)}`);
    const blank = '\u200b\ufeff\u0085'.repeat(40);
    expect(keepToLimit(blank, '')).toBe(blank);
  });

  it('lets a longer name the agent already holds be edited down, and refuses only growth', () => {
    const held = 'a'.repeat(90);
    expect(keepToLimit('a'.repeat(89), held)).toBe('a'.repeat(89));
    expect(keepToLimit('a'.repeat(70), held)).toBe('a'.repeat(70));
    expect(keepToLimit('a'.repeat(91), held)).toBe('a'.repeat(60));
    // The same length as before is not growth.
    expect(keepToLimit(`${'a'.repeat(89)}b`, held)).toBe(`${'a'.repeat(89)}b`);
  });
});

describe('the line a refusal becomes', () => {
  const refused = (code: string, message = 'the device’s words') =>
    renameFailure(new RequestError({ code, message }));

  it('says a busy terminal for conflict, in either language', () => {
    expect(refused('conflict')).toBe('The session is busy. Try again later.');
    useSettings.setState({ language: 'zh-Hans' });
    expect(refused('conflict')).toBe('会话正忙，请稍后再试。');
  });

  it('says it cannot be renamed from here for unsupported, in either language', () => {
    expect(refused('unsupported')).toBe(
      "This session can't be renamed from here. Rename it in its terminal.",
    );
    useSettings.setState({ language: 'zh-Hans' });
    expect(refused('unsupported')).toBe('这个会话无法从这里重命名，请在它的终端里改名。');
  });

  it('keeps the app’s own words for the other codes', () => {
    expect(refused('device_offline')).toBe(en.errors.deviceOffline);
    expect(refused('timeout')).toBe(en.errors.timeout);
    expect(refused('not_found')).toBe(en.errors.notFound);
    // `agent_unavailable` and `bad_request` are the device's own sentences.
    expect(refused('bad_request', 'title must not be empty')).toBe('title must not be empty');
    // A failure minted here has no message, so the generic line stands in.
    expect(refused('internal', '')).toBe(en.errors.generic);
    expect(renameFailure(new Error(''))).toBe(en.errors.generic);
  });

  it('is the same wording in the table of each language', () => {
    expect(en.sessions.renameTitle).toBe('Rename session');
    expect(en.sessions.renameLabel).toBe('Session name');
    expect(en.sessions.renameEmpty).toBe('Enter a name.');
    expect(en.sessions.renameTooLong(60)).toBe('Names can be up to 60 characters.');
    const zh = stringTables['zh-Hans'].sessions;
    expect(zh.renameTitle).toBe('重命名会话');
    expect(zh.renameLabel).toBe('会话名称');
    expect(zh.renameEmpty).toBe('请输入名称。');
    expect(zh.renameTooLong(60)).toBe('名称最多 60 个字符。');
    expect(en.common.rename).toBe('Rename');
    expect(stringTables['zh-Hans'].common.rename).toBe('重命名');
  });
});

describe('the store’s rename', () => {
  const stored = (): Session => useSessions.getState().sessions[keyOf(session)]!;

  it('sends the name alone and resolves with the session the device answers with', async () => {
    const reply: Session = { ...session, title: 'New name' };
    rpcMock.mockResolvedValueOnce({ session: reply } satisfies SessionResult);

    const result = await useSessions.getState().rename(session.session_id, 'New name');

    expect(rpcMock).toHaveBeenCalledTimes(1);
    expect(rpcMock).toHaveBeenCalledWith('session.set', {
      session_id: session.session_id,
      title: 'New name',
    });
    expect(result).toBe(reply);
    expect(stored()).toBe(reply);
  });

  it('draws nothing ahead of the reply, and then the reply’s title, not the one asked for', async () => {
    let answer!: (result: SessionResult) => void;
    rpcMock.mockReturnValueOnce(
      new Promise<SessionResult>((resolve) => {
        answer = resolve;
      }),
    );

    const pending = useSessions.getState().rename(session.session_id, 'Fix it');

    // Pending: the stored title is still the old one, as is every other field.
    expect(stored().title).toBe(session.title);
    expect(stored()).toBe(session);

    // Claude suffixes a name another live session holds.
    answer({ session: { ...session, title: 'Fix it (2)' } });
    await expect(pending).resolves.toMatchObject({ title: 'Fix it (2)' });
    expect(stored().title).toBe('Fix it (2)');
  });

  it('rejects with the device’s refusal and leaves the stored session alone', async () => {
    rpcMock.mockRejectedValueOnce(new RequestError({ code: 'conflict', message: 'busy' }));

    await expect(useSessions.getState().rename(session.session_id, 'New name')).rejects.toMatchObject({
      code: 'conflict',
    });

    expect(stored()).toBe(session);
  });

  it('leaves the row where it was: the reply’s updated_at is the device’s word', async () => {
    rpcMock.mockResolvedValueOnce({ session: { ...session, title: 'New name' } });

    await useSessions.getState().rename(session.session_id, 'New name');

    expect(stored().updated_at).toBe(session.updated_at);
  });
});
