/**
 * The rules of Close that are not drawing (A39, A61): when a row offers it,
 * what it asks first, and the line a refusal becomes.
 */
import { afterEach, describe, expect, it } from 'vitest';
import {
  canCloseTerminal,
  closeFailure,
  closeQuestion,
  offersClose,
} from '../src/features/sessions/close';
import { RequestError } from '../src/lib/ws';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { claudeAgent, devices, sessions } from '../mock/fixtures';
import type { AgentInfo, Device, Session } from '../src/protocol/types';

afterEach(() => {
  useSettings.setState({ language: 'en' });
});

const mac = devices.find((d) => d.device_id === 'dev-mac') as Device;
const sessionOf = (id: string): Session => sessions.find((s) => s.session_id === id) as Session;

const withCapability = (device: Device, listed: boolean): Device => ({
  ...device,
  agents: device.agents.map((agent: AgentInfo) => ({
    ...agent,
    capabilities: listed
      ? [...new Set([...agent.capabilities, 'close_terminal'])]
      : agent.capabilities.filter((capability) => capability !== 'close_terminal'),
  })),
});

describe('when Close is offered', () => {
  const terminal = sessionOf('ses-terminal');
  const shared = sessionOf('ses-shared');
  const remote = sessionOf('ses-push');

  it('is offered on a session the device drives, whatever the device lists (A39)', () => {
    expect(offersClose(remote, withCapability(mac, false))).toBe(true);
  });

  it('is offered on a row a terminal holds when its agent lists close_terminal (A61)', () => {
    expect(offersClose(terminal, withCapability(mac, true))).toBe(true);
    expect(offersClose(shared, withCapability(mac, true))).toBe(true);
  });

  it('is not offered there when the agent does not list it, or the device is offline', () => {
    expect(offersClose(terminal, withCapability(mac, false))).toBe(false);
    expect(offersClose(shared, { ...withCapability(mac, true), online: false })).toBe(false);
  });

  it('is not offered for an agent the device does not report at all', () => {
    expect(offersClose(terminal, { ...withCapability(mac, true), agents: [] })).toBe(false);
  });

  it('is never offered on a row in the Archive or one nothing holds', () => {
    expect(offersClose({ ...shared, archived: true }, withCapability(mac, true))).toBe(false);
    expect(offersClose({ ...remote, archived: true }, mac)).toBe(false);
    expect(offersClose({ ...shared, control: 'none' }, withCapability(mac, true))).toBe(false);
  });

  it('reads the capability alone', () => {
    expect(canCloseTerminal({ ...claudeAgent, capabilities: ['close_terminal'] }, true)).toBe(true);
    expect(canCloseTerminal(claudeAgent, false)).toBe(false);
    expect(canCloseTerminal(undefined, true)).toBe(false);
  });
});

describe('what Close asks first', () => {
  const shared = sessionOf('ses-shared');

  it('asks nothing for an idle session the device drives', () => {
    expect(closeQuestion(sessionOf('ses-push'), 'mac', false)).toEqual([]);
  });

  it('says the work is lost while a session the device drives works (A39)', () => {
    expect(closeQuestion(sessionOf('ses-push'), 'mac', true)).toEqual([strings.sessions.closeBody]);
  });

  it('always warns about the terminal, naming the agent and the device (A61)', () => {
    expect(closeQuestion(shared, 'mac-studio-office', false)).toEqual([
      'This also quits Claude Code in its terminal on mac-studio-office. Anything typed there and not sent is lost.',
    ]);
    expect(closeQuestion(shared, 'mac-studio-office', true)).toEqual([
      'This also quits Claude Code in its terminal on mac-studio-office. Anything typed there and not sent is lost.',
      'The agent is still working; what it has not finished is lost.',
    ]);
  });

  it('keeps the agent and the device in their places in Chinese', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(closeQuestion({ ...shared, agent: 'codex' }, 'ci-runner-01', true)).toEqual([
      'Codex 在 ci-runner-01 终端里的进程也会退出，终端里尚未发送的内容会丢失。',
      'Agent 仍在工作，未完成的部分会丢失。',
    ]);
  });
});

describe('the line a refusal becomes', () => {
  const refusal = (code: string, message = 'from the device') => new RequestError({ code, message });

  it('has words of its own for unsupported and conflict', () => {
    expect(closeFailure(refusal('unsupported'))).toBe(
      "This session can't be closed from here. Close it in its terminal.",
    );
    expect(closeFailure(refusal('conflict'))).toBe('The session is busy. Try again later.');
  });

  it('keeps the app’s words for every other code', () => {
    expect(closeFailure(refusal('device_offline', ''))).toBe(strings.errors.deviceOffline);
    expect(closeFailure(refusal('timeout', ''))).toBe(strings.errors.timeout);
    expect(closeFailure(refusal('agent_unavailable', 'claude is not installed'))).toBe(
      'claude is not installed',
    );
    expect(closeFailure(new Error(''))).toBe(strings.errors.generic);
  });

  it('says them in Chinese', () => {
    useSettings.setState({ language: 'zh-Hans' });
    expect(closeFailure(refusal('unsupported'))).toBe('这个会话无法从这里关闭，请在它的终端里关闭。');
    expect(closeFailure(refusal('conflict'))).toBe('会话正忙，请稍后再试。');
  });
});
