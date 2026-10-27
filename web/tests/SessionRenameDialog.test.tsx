/**
 * A55 — the Rename dialog: the device rename's dialog with one field, a limit
 * counted in code points, a Save that waits for the device and draws what the
 * device answers with, and one line under the field for what stops Save or for
 * a refusal. The store is real and the socket is a stand-in.
 */
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SessionRenameDialog } from '../src/features/sessions/SessionRenameDialog';
import { RequestError } from '../src/lib/ws';
import { strings } from '../src/strings';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { sessions } from '../mock/fixtures';
import type { SessionResult } from '../src/protocol/frames';
import type { Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const base: Session = sessions.find((s) => s.session_id === 'ses-push')!;

beforeEach(() => {
  rpcMock.mockReset();
  useSettings.setState({ language: 'en' });
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** The dialog open on `over`'s session, with the store holding that row. */
function open(over: Partial<Session> = {}) {
  const target: Session = { ...base, ...over };
  useSessions.setState({ sessions: { [keyOf(target)]: target }, loaded: true });
  const onClose = vi.fn();
  render(<SessionRenameDialog session={target} onClose={onClose} />);
  return { target, onClose };
}

const field = (): HTMLInputElement =>
  screen.getByRole('textbox', { name: strings.sessions.renameLabel });
const save = (): HTMLElement => screen.getByRole('button', { name: strings.common.save });
const cancel = (): HTMLElement => screen.getByRole('button', { name: strings.common.cancel });
const stored = (target: Session): Session => useSessions.getState().sessions[keyOf(target)]!;

/** Replace the field's text in one step, as a paste does. */
const setText = (text: string) => fireEvent.change(field(), { target: { value: text } });

/** The request the dialog sent, once the Enter's short wait for the input method is over. */
const sentRename = async () => {
  await waitFor(() => expect(rpcMock).toHaveBeenCalledTimes(1));
  return rpcMock.mock.calls[0]!;
};

describe('opening', () => {
  it('is titled and labelled in the device rename’s words, with Save and Cancel', () => {
    open();

    const dialog = screen.getByRole('dialog', { name: 'Rename session' });
    expect(within(dialog).getByText('Session name')).toBeInTheDocument();
    expect(save()).toHaveTextContent('Save');
    expect(cancel()).toHaveTextContent('Cancel');
  });

  it('is in Chinese when the interface is', () => {
    useSettings.setState({ language: 'zh-Hans' });
    open();

    expect(screen.getByRole('dialog', { name: '重命名会话' })).toBeInTheDocument();
    expect(screen.getByText('会话名称')).toBeInTheDocument();
    expect(save()).toHaveTextContent('保存');
    expect(cancel()).toHaveTextContent('取消');
  });

  it('starts on the current name, focused and selected, so typing replaces it', () => {
    open();

    expect(field()).toHaveValue('iOS push tokens');
    expect(field()).toHaveFocus();
    expect(field().selectionStart).toBe(0);
    expect(field().selectionEnd).toBe('iOS push tokens'.length);
  });

  it('starts an untitled session empty, and says what it needs', () => {
    open({ title: '' });

    expect(field()).toHaveValue('');
    expect(save()).toBeDisabled();
    expect(screen.getByText('Enter a name.')).toBeInTheDocument();
  });
});

describe('Save', () => {
  it('is disabled while the name is unchanged, and says nothing about it', () => {
    open();

    expect(save()).toBeDisabled();
    expect(screen.queryByText('Enter a name.')).not.toBeInTheDocument();

    // Only white space around the same name is still the same name.
    setText('  iOS push tokens  ');
    expect(save()).toBeDisabled();
  });

  it('is disabled while the trimmed text is empty', () => {
    open();

    setText('   ');

    expect(save()).toBeDisabled();
    expect(screen.getByText('Enter a name.')).toBeInTheDocument();
  });

  it('is disabled for a name that is only zero-width or control characters, which hold none', () => {
    open();

    for (const text of ['\u200b', '\ufeff', '\u0085', '\u200b \ufeff\u0085']) {
      setText(text);
      expect(save(), JSON.stringify(text)).toBeDisabled();
      expect(screen.getByText('Enter a name.')).toBeInTheDocument();
    }
  });

  it('sends the name without the zero-width characters round it, and keeps an emoji sequence whole', async () => {
    const { target } = open();
    rpcMock.mockResolvedValueOnce({ session: { ...target, title: '👩\u200d💻 pairing' } });

    setText('\u200b 👩\u200d💻 pairing \ufeff');
    await userEvent.click(save());

    expect(rpcMock.mock.calls[0]![1]).toEqual({
      session_id: target.session_id,
      title: '👩\u200d💻 pairing',
    });
  });

  it('is enabled for a new name', () => {
    open();

    setText('Push tokens, again');

    expect(save()).toBeEnabled();
    expect(screen.queryByText('Enter a name.')).not.toBeInTheDocument();
  });

  it('allows 60 code points however many UTF-16 units they take, and sends them trimmed', async () => {
    const { target } = open();
    const name = '😀'.repeat(60);
    expect(name.length).toBe(120);
    rpcMock.mockResolvedValueOnce({ session: { ...target, title: name } });

    setText(`  ${name}  `);
    expect(save()).toBeEnabled();
    await userEvent.click(save());

    expect(rpcMock).toHaveBeenCalledWith('session.set', {
      session_id: target.session_id,
      title: name,
    });
  });

  it('keeps the field to 60 code points: the 61st is cut, and the line says why', async () => {
    const { target } = open();
    const name = '😀'.repeat(60);
    rpcMock.mockResolvedValueOnce({ session: { ...target, title: name } });

    setText('😀'.repeat(61));

    expect(field()).toHaveValue(name);
    expect(screen.getByText('Names can be up to 60 characters.')).toHaveClass('form-error');
    // What was kept is a name, and Save sends it.
    expect(save()).toBeEnabled();
    await userEvent.click(save());
    expect(rpcMock.mock.calls[0]![1]).toEqual({ session_id: target.session_id, title: name });
  });

  it('refuses a character typed at the limit, one at a time', async () => {
    open();
    setText('a'.repeat(60));
    expect(screen.queryByText('Names can be up to 60 characters.')).not.toBeInTheDocument();

    await userEvent.type(field(), 'bc');

    expect(field()).toHaveValue('a'.repeat(60));
    expect(screen.getByText('Names can be up to 60 characters.')).toBeInTheDocument();

    // A character that makes room takes the line away.
    await userEvent.type(field(), '{Backspace}');
    expect(field()).toHaveValue('a'.repeat(59));
    expect(screen.queryByText('Names can be up to 60 characters.')).not.toBeInTheDocument();
  });

  it('shows a longer name the agent holds whole, refuses Save, and lets it be edited down', () => {
    open({ title: 'b'.repeat(90) });

    expect(field()).toHaveValue('b'.repeat(90));
    expect(save()).toBeDisabled();
    expect(screen.getByText('Names can be up to 60 characters.')).toBeInTheDocument();

    // Shortening is let through; growing is cut.
    setText('b'.repeat(70));
    expect(field()).toHaveValue('b'.repeat(70));
    expect(save()).toBeDisabled();
    setText('b'.repeat(71));
    expect(field()).toHaveValue('b'.repeat(60));

    setText('b'.repeat(60));
    expect(save()).toBeEnabled();
  });

  it('cuts what an input method commits, and not what it is still composing', () => {
    open({ title: 'a'.repeat(58) });

    fireEvent.compositionStart(field());
    fireEvent.input(field(), { target: { value: `${'a'.repeat(58)}nihao` }, isComposing: true });
    // The buffer is not text yet: it is not cut, and nothing can be saved from it.
    expect(field()).toHaveValue(`${'a'.repeat(58)}nihao`);
    expect(save()).toBeDisabled();

    field().value = `${'a'.repeat(58)}你好吗`;
    fireEvent.compositionEnd(field());

    expect(field()).toHaveValue(`${'a'.repeat(58)}你好`);
    expect(screen.getByText('Names can be up to 60 characters.')).toBeInTheDocument();
    expect(save()).toBeEnabled();
  });

  it('keeps a composition within the limit whole', () => {
    open({ title: 'a'.repeat(50) });

    fireEvent.compositionStart(field());
    fireEvent.input(field(), { target: { value: `${'a'.repeat(50)}nihao` }, isComposing: true });
    field().value = `${'a'.repeat(50)}你好`;
    fireEvent.compositionEnd(field());

    expect(field()).toHaveValue(`${'a'.repeat(50)}你好`);
    expect(screen.queryByText('Names can be up to 60 characters.')).not.toBeInTheDocument();
  });

  it('says the limit in Chinese too', () => {
    useSettings.setState({ language: 'zh-Hans' });
    open();

    setText('字'.repeat(61));

    expect(field()).toHaveValue('字'.repeat(60));
    expect(screen.getByText('名称最多 60 个字符。')).toBeInTheDocument();
    expect(save()).toBeEnabled();
  });

  it('sends the name alone, as the frame the contract names', async () => {
    const { target } = open();
    rpcMock.mockResolvedValueOnce({ session: { ...target, title: 'Push tokens, again' } });

    setText('Push tokens, again');
    await userEvent.click(save());

    expect(rpcMock).toHaveBeenCalledTimes(1);
    expect(rpcMock.mock.calls[0]![0]).toBe('session.set');
    expect(rpcMock.mock.calls[0]![1]).toEqual({
      session_id: target.session_id,
      title: 'Push tokens, again',
    });
  });
});

describe('the keyboard', () => {
  it('saves on Enter', async () => {
    const { target, onClose } = open();
    rpcMock.mockResolvedValueOnce({ session: { ...target, title: 'Typed name' } });

    await userEvent.clear(field());
    await userEvent.type(field(), 'Typed name{Enter}');

    const [type, params] = await sentRename();
    expect(type).toBe('session.set');
    expect(params).toEqual({ session_id: target.session_id, title: 'Typed name' });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('does nothing on Enter while Save is disabled', async () => {
    const { onClose } = open();

    await userEvent.type(field(), '{Enter}');
    await new Promise((resolve) => setTimeout(resolve, 150));

    expect(rpcMock).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on Escape and sends nothing', async () => {
    const { onClose } = open();

    setText('Something else');
    await userEvent.keyboard('{Escape}');

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(rpcMock).not.toHaveBeenCalled();
  });

  it('closes on Cancel and sends nothing', async () => {
    const { onClose } = open();

    setText('Something else');
    await userEvent.click(cancel());

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(rpcMock).not.toHaveBeenCalled();
  });

  it('leaves an Enter pressed while a composition is open to the input method', async () => {
    const { target } = open();
    rpcMock.mockResolvedValue({ session: { ...target, title: '新名字' } });

    setText('新名字');
    fireEvent.compositionStart(field());
    fireEvent.keyDown(field(), { key: 'Enter', keyCode: 229, isComposing: true });
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(rpcMock).not.toHaveBeenCalled();

    // The composition ends, and the next Enter is the person’s.
    fireEvent.compositionEnd(field());
    await new Promise((resolve) => setTimeout(resolve, 20));
    fireEvent.keyDown(field(), { key: 'Enter', keyCode: 13 });
    await sentRename();
  });

  it('leaves the Enter that WebKit sends just before the composition ends to the input method', async () => {
    const { target } = open();
    rpcMock.mockResolvedValue({ session: { ...target, title: '新名字' } });

    // The keydown of the confirming Enter looks like a plain one; the
    // `compositionend` of the same key press arrives a moment later.
    setText('新名字');
    fireEvent.keyDown(field(), { key: 'Enter', keyCode: 13 });
    fireEvent.compositionEnd(field());
    await new Promise((resolve) => setTimeout(resolve, 150));

    expect(rpcMock).not.toHaveBeenCalled();
  });

  it('leaves the Escape that cancels a composition to the input method, and keeps the dialog', () => {
    const { onClose } = open();

    fireEvent.compositionStart(field());
    fireEvent.keyDown(field(), { key: 'Escape', isComposing: true });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.compositionEnd(field());
    fireEvent.keyDown(field(), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('waiting for the device', () => {
  /** A request the device has not answered yet, and the way to answer it. */
  const pending = () => {
    let answer!: (result: SessionResult) => void;
    let refuse!: (error: unknown) => void;
    rpcMock.mockReturnValueOnce(
      new Promise<SessionResult>((resolve, reject) => {
        answer = resolve;
        refuse = reject;
      }),
    );
    return { answer, refuse };
  };

  it('shows progress, locks the field and Save, and leaves Cancel alone', async () => {
    const { target } = open();
    const device = pending();

    setText('A slow rename');
    await userEvent.click(save());

    expect(save()).toBeDisabled();
    expect(save().querySelector('.spinner')).not.toBeNull();
    expect(field()).toHaveAttribute('readonly');
    expect(cancel()).toBeEnabled();
    // Nothing is drawn ahead of the device.
    expect(stored(target).title).toBe(target.title);

    device.answer({ session: { ...target, title: 'A slow rename' } });
  });

  it('closes on success and draws the title in the reply, not the one that was typed', async () => {
    const { target, onClose } = open();
    const device = pending();

    setText('Fix it');
    await userEvent.click(save());
    expect(onClose).not.toHaveBeenCalled();
    device.answer({ session: { ...target, title: 'Fix it (2)' } });

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(stored(target).title).toBe('Fix it (2)');
  });

  it('keeps focus in the field after a click on Save, while it waits and after a refusal', async () => {
    const { target } = open();
    const device = pending();

    setText('Focus stays');
    await userEvent.click(save());
    expect(field()).toHaveFocus();

    device.refuse(new RequestError({ code: 'conflict', message: 'busy' }));
    await screen.findByRole('alert');
    expect(field()).toHaveFocus();
    expect(stored(target).title).toBe(target.title);
  });

  it('sends one request however many times Save is pressed while it waits', async () => {
    const { target } = open();
    const device = pending();

    setText('Only once');
    await userEvent.click(save());
    await userEvent.click(save());

    expect(rpcMock).toHaveBeenCalledTimes(1);
    device.answer({ session: { ...target, title: 'Only once' } });
  });

  it('can be dismissed meanwhile, and the late reply closes nothing else', async () => {
    const onClose = vi.fn();
    const target: Session = { ...base };
    useSessions.setState({ sessions: { [keyOf(target)]: target }, loaded: true });
    // The parent unmounts the dialog on close, as every caller does.
    function Host() {
      const [shown, setShown] = useState(true);
      return shown ? (
        <SessionRenameDialog
          session={target}
          onClose={() => {
            onClose();
            setShown(false);
          }}
        />
      ) : null;
    }
    render(<Host />);
    const device = pending();

    setText('Dismissed');
    await userEvent.click(save());
    await userEvent.click(cancel());
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    device.answer({ session: { ...target, title: 'Dismissed' } });
    await waitFor(() => expect(stored(target).title).toBe('Dismissed'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('a refusal', () => {
  const refuseWith = async (code: string, message = 'the device’s words') => {
    const { target, onClose } = open();
    rpcMock.mockRejectedValueOnce(new RequestError({ code, message }));
    setText('Not allowed');
    await userEvent.click(save());
    return { target, onClose };
  };

  it('says a busy terminal for conflict, under the field, with the dialog open', async () => {
    const { target, onClose } = await refuseWith('conflict', 'the terminal is busy');

    const line = await screen.findByRole('alert');
    expect(line).toHaveTextContent('The session is busy. Try again later.');
    expect(line).not.toHaveTextContent('the terminal is busy');
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // What was typed stays, and so does the session’s title.
    expect(field()).toHaveValue('Not allowed');
    expect(stored(target).title).toBe(target.title);
    expect(save()).toBeEnabled();
    expect(field()).not.toHaveAttribute('readonly');
  });

  it('says it cannot be renamed from here for unsupported', async () => {
    await refuseWith('unsupported', 'open in a terminal the device cannot reach');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "This session can't be renamed from here. Rename it in its terminal.",
    );
  });

  it('says both in Chinese when the interface is', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    await refuseWith('conflict');
    expect(await screen.findByRole('alert')).toHaveTextContent('会话正忙，请稍后再试。');

    rpcMock.mockRejectedValueOnce(new RequestError({ code: 'unsupported', message: 'x' }));
    await userEvent.click(save());
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        '这个会话无法从这里重命名，请在它的终端里改名。',
      ),
    );
  });

  it('keeps the app’s presentation for the other codes', async () => {
    await refuseWith('device_offline');
    expect(await screen.findByRole('alert')).toHaveTextContent(strings.errors.deviceOffline);
  });

  it('says it timed out in the app’s words', async () => {
    await refuseWith('timeout');
    expect(await screen.findByRole('alert')).toHaveTextContent(strings.errors.timeout);
  });

  it('falls back to the generic line when the failure carries no words', async () => {
    await refuseWith('internal', '');
    expect(await screen.findByRole('alert')).toHaveTextContent(strings.errors.generic);
  });

  it('is the device’s own sentence for a name it will not keep', async () => {
    await refuseWith('bad_request', 'title must not be empty');
    expect(await screen.findByRole('alert')).toHaveTextContent('title must not be empty');
  });

  it('goes when the name is edited, and a second try can succeed', async () => {
    const { target, onClose } = await refuseWith('conflict');
    await screen.findByRole('alert');

    setText('Not allowed, again');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    rpcMock.mockResolvedValueOnce({ session: { ...target, title: 'Not allowed, again' } });
    await userEvent.click(save());
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(stored(target).title).toBe('Not allowed, again');
  });
});

describe('the lists it opens from', () => {
  it('keeps focus, and the caret, where they are when the page re-renders around it', () => {
    const target: Session = { ...base };
    useSessions.setState({ sessions: { [keyOf(target)]: target }, loaded: true });
    const { rerender } = render(<SessionRenameDialog session={target} onClose={() => undefined} />);
    setText('A longer name');
    field().setSelectionRange(3, 3);
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');

    // Every session update re-renders the page, and hands the dialog a new
    // callback; the Modal re-arming its focus handling would take the field’s
    // focus away mid-composition and select the whole name again.
    rerender(<SessionRenameDialog session={target} onClose={() => undefined} />);

    expect(focus).not.toHaveBeenCalled();
    expect(field()).toHaveFocus();
    expect(field().selectionStart).toBe(3);
    expect(field().selectionEnd).toBe(3);
  });
});
