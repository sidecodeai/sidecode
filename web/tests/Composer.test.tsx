import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from '../src/features/chat/Composer';
import { SEND_DELAY_MS } from '../src/features/chat/useImeGuard';
import { foldSession } from '../src/stores/chat';
import { strings } from '../src/strings';
import { claudeAgent, codexAgent } from '../mock/fixtures';
import type { AgentInfo, Session, SessionEvent, SessionState } from '../src/protocol/types';

const baseSession: Session = {
  session_id: 'ses-1',
  device_id: 'dev-1',
  agent: 'claude',
  title: 'Fix flaky auth test',
  cwd: '/Users/me/dev/gateway',
  git: null,
  state: 'idle',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'claude-sonnet-4-5',
  permission_mode: 'default',
  effort: 'high',
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
};

function setup(
  overrides: {
    state?: SessionState;
    control?: Session['control'];
    agent?: AgentInfo;
    deviceOnline?: boolean;
  } = {},
) {
  const onSend = vi.fn().mockResolvedValue(undefined);
  const props = {
    session: {
      ...baseSession,
      ...(overrides.state ? { state: overrides.state } : {}),
      ...(overrides.control ? { control: overrides.control } : {}),
      ...(overrides.agent ? { agent: overrides.agent.agent } : {}),
    },
    agent: overrides.agent ?? claudeAgent,
    deviceOnline: overrides.deviceOnline ?? true,
    queue: [],
    sttEnabled: false,
    question: null,
    onSend,
    onAnswer: vi.fn().mockResolvedValue(undefined),
    onSetOption: vi.fn(),
    onRemoveQueued: vi.fn(),
    onTakeover: vi.fn(),
  };
  render(<Composer {...props} />);
  return { onSend };
}

const typeAndSend = async (text: string, buttonName: RegExp | string) => {
  const user = userEvent.setup();
  await user.click(screen.getByLabelText('Message the agent…'));
  await user.keyboard(text);
  await user.click(screen.getByRole('button', { name: buttonName }));
};

describe('Composer send mode', () => {
  it('sends mode "auto" when the session is idle', async () => {
    const { onSend } = setup();
    await typeAndSend('run the tests', 'Send');
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith('run the tests', [], 'auto');
  });

  it('still sends mode "auto" while the turn is running, so the device can queue', async () => {
    const { onSend } = setup({ state: 'running' });
    // Without the steer capability the button reads "Queue"…
    await typeAndSend('and then lint', 'Queue');
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    // …but the wire mode stays "auto": forcing "queue" would bypass steering.
    expect(onSend).toHaveBeenCalledWith('and then lint', [], 'auto');
  });

  it('sends mode "auto" for a steer-capable agent and labels the button Send', async () => {
    expect(codexAgent.capabilities).toContain('steer');
    const { onSend } = setup({ state: 'running', agent: codexAgent });
    await typeAndSend('actually target the CI runner', 'Send');
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith('actually target the CI runner', [], 'auto');
  });

  it('offers "Interrupt & send" as an explicit alternative while running', async () => {
    const user = userEvent.setup();
    const { onSend } = setup({ state: 'needs_approval' });
    await user.click(screen.getByLabelText('Message the agent…'));
    await user.keyboard('stop and do this instead');

    await user.click(screen.getByRole('button', { name: 'Send options' }));
    await user.click(screen.getByRole('button', { name: 'Interrupt & send' }));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith('stop and do this instead', [], 'interrupt');
  });

  it('does not offer the interrupt alternative when the session is idle', () => {
    setup();
    expect(screen.queryByRole('button', { name: 'Send options' })).not.toBeInTheDocument();
  });

  it('sends on Enter and inserts a newline on Shift+Enter', async () => {
    const user = userEvent.setup();
    const { onSend } = setup();
    const input = screen.getByLabelText('Message the agent…');

    await user.click(input);
    await user.keyboard('first{Shift>}{Enter}{/Shift}second');
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue('first\nsecond');

    await user.keyboard('{Enter}');
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith('first\nsecond', [], 'auto');
  });

  it("leaves the Enter that confirms an input method's text to the input method", async () => {
    const user = userEvent.setup();
    const { onSend } = setup();
    const input = screen.getByLabelText('Message the agent…');
    await user.click(input);
    await user.keyboard('hello');

    // Chrome and Firefox: the keydown itself says it is part of the composition.
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.compositionEnd(input);
    expect(onSend).not.toHaveBeenCalled();

    // WebKit, one order: compositionend first, then a keydown that no longer says so.
    fireEvent.compositionStart(input);
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Enter' });
    await new Promise((resolve) => setTimeout(resolve, SEND_DELAY_MS * 2));
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue('hello');

    // WebKit, the other order: the keydown lands before the compositionend
    // of the same press, so the send it scheduled is cancelled.
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.compositionEnd(input);
    await new Promise((resolve) => setTimeout(resolve, SEND_DELAY_MS * 2));
    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue('hello');

    // The next press, in a later task with nothing composing, is Send.
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith('hello', [], 'auto');
  });

  it('refuses to send an empty message', async () => {
    const { onSend } = setup();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    await typeAndSend('   ', 'Send');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('clears the field in the same tick as the send (A12)', async () => {
    const user = userEvent.setup();
    // A send that never settles: the field must not wait for it.
    const onSend = vi.fn().mockReturnValue(new Promise<void>(() => {}));
    render(
      <Composer
        session={baseSession}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={onSend}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    const input = screen.getByLabelText('Message the agent…');
    await user.click(input);
    await user.keyboard('run the tests');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    expect(onSend).toHaveBeenCalledWith('run the tests', [], 'auto');
    expect(input).toHaveValue('');
    // Empty again, so the button is ready for the next message rather than busy.
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    await user.click(input);
    await user.keyboard('and then lint');
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('hands the draft back when the gateway refuses the message', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn().mockRejectedValue(new Error('That device is offline.'));
    render(
      <Composer
        session={baseSession}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={onSend}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    await user.click(screen.getByLabelText('Message the agent…'));
    await user.keyboard('keep me');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await screen.findByText('That device is offline.');
    expect(screen.getByLabelText('Message the agent…')).toHaveValue('keep me');
  });

  it('leaves a newer draft alone when an older send is refused', async () => {
    const user = userEvent.setup();
    let refuse = (_: unknown) => {};
    const onSend = vi.fn().mockReturnValue(new Promise((_resolve, reject) => (refuse = reject)));
    render(
      <Composer
        session={baseSession}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={onSend}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    const input = screen.getByLabelText('Message the agent…');
    await user.click(input);
    await user.keyboard('first message');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    await user.click(input);
    await user.keyboard('second message');

    refuse(new Error('That device is offline.'));
    await screen.findByText('That device is offline.');
    expect(input).toHaveValue('second message');
  });
});

describe('Composer disabled states', () => {
  it('disables input and offers takeover for a terminal-controlled session', async () => {
    const user = userEvent.setup();
    const onTakeover = vi.fn();
    render(
      <Composer
        session={{ ...baseSession, control: 'terminal', state: 'readonly' }}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn()}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={onTakeover}
      />,
    );
    expect(screen.getByLabelText('Message the agent…')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Take over' }));
    expect(onTakeover).toHaveBeenCalled();
  });

  it('stays disabled for a terminal session that reports running (A7)', () => {
    const onTakeover = vi.fn();
    render(
      <Composer
        session={{ ...baseSession, control: 'terminal', state: 'running' }}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn()}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={onTakeover}
      />,
    );
    const input = screen.getByLabelText('Message the agent…');
    expect(input).toBeDisabled();
    // The field says who holds it; the status line offers taking it over.
    expect(input).toHaveAttribute('placeholder', 'Controlled by the terminal');
    expect(screen.getByRole('button', { name: 'Take over' })).toBeInTheDocument();
  });

  it('disables the composer while the device is offline', () => {
    setup({ deviceOnline: false });
    const input = screen.getByLabelText('Message the agent…');
    expect(input).toBeDisabled();
    expect(input).toHaveAttribute('placeholder', 'Device is offline');
  });

  it('shows queued messages as one Up next chip whose list removes them', async () => {
    const user = userEvent.setup();
    const onRemoveQueued = vi.fn();
    render(
      <Composer
        session={{ ...baseSession, state: 'running', queued: 1 }}
        agent={claudeAgent}
        deviceOnline
        queue={[{ id: 'q1', text: 'also update the changelog', ts: 1 }]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn()}
        onSetOption={vi.fn()}
        onRemoveQueued={onRemoveQueued}
        onTakeover={vi.fn()}
      />,
    );
    // A43: a count, never a stack of lines over the field.
    expect(screen.queryByText('also update the changelog')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Up next · 1' }));
    expect(screen.getByText('also update the changelog')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Remove from queue' }));
    expect(onRemoveQueued).toHaveBeenCalledWith('q1');
  });

  it('hides the microphone when the gateway has speech-to-text disabled', () => {
    setup();
    expect(screen.queryByRole('button', { name: 'Start voice input' })).not.toBeInTheDocument();
  });
});

/**
 * `docs/DESIGN.md` § "The control row" (A43, A44): from the leading edge, Up
 * next, the model card, the permission mode. The web dictates only through the
 * gateway, which detects the language, so the row offers no language even
 * while the microphone is there.
 */
describe('Composer control row', () => {
  const renderRow = (control: Session['control']) =>
    render(
      <Composer
        session={{ ...baseSession, control, state: 'running', queued: 1 }}
        agent={claudeAgent}
        deviceOnline
        queue={[{ id: 'q1', text: 'also update the changelog', ts: 1 }]}
        sttEnabled
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn().mockResolvedValue(undefined)}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );

  const row = () => document.querySelector('.composer-bottom') as HTMLElement;

  it('reads Up next, the model card, then the permission mode, and nothing else', () => {
    renderRow('remote');

    expect(screen.getByRole('button', { name: strings.composer.micStart })).toBeInTheDocument();
    const names = within(row())
      .getAllByRole('button')
      .map((button) => button.getAttribute('aria-label') ?? button.textContent);
    expect(names).toEqual([
      strings.composer.upNextCount(1),
      strings.composer.modelCard,
      strings.composer.permissionMode,
    ]);
  });

  it('keeps Up next first on a session the terminal holds', () => {
    renderRow('terminal');

    expect(row().children).toHaveLength(3);
    expect(row().firstElementChild).toContainElement(
      screen.getByRole('button', { name: strings.composer.upNextCount(1) }),
    );
    expect(row().querySelectorAll('.composer-chip.readonly')).toHaveLength(2);
  });
});

/**
 * A17 — what the terminal chose is shown, not offered. `docs/DESIGN.md`
 * § "The composer": a session a terminal holds draws the model card and the
 * permission mode as chips that open nothing, and A21 puts the tier on the
 * first of them.
 */
describe('Composer settings a terminal holds', () => {
  const renderComposer = (session: Partial<Session>, agent: AgentInfo | null = claudeAgent) =>
    render(
      <Composer
        session={{ ...baseSession, ...session }}
        agent={agent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn().mockResolvedValue(undefined)}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );

  const chip = (name: string, value: string) =>
    screen.queryByLabelText(strings.composer.setInTerminal(name, value));

  it('shows only the setting A40 leaves the terminal on a shared Claude session', () => {
    renderComposer({ control: 'shared' });

    // The device types `/model` and `/effort` into the terminal, so the card
    // opens; it has nothing to type for the permission mode, which stays a
    // chip that opens nothing.
    expect(screen.getByRole('button', { name: strings.composer.modelCard })).toBeEnabled();
    expect(chip(strings.composer.permissionMode, 'Ask before edits')).toBeInTheDocument();
    expect(document.querySelectorAll('.composer-chip.readonly')).toHaveLength(1);
  });

  it('shows them on a terminal session too, whose composer is disabled', () => {
    renderComposer({ control: 'terminal' });

    expect(chip(strings.composer.modelCard, 'Sonnet 4.5 High')).toBeInTheDocument();
    expect(screen.getByLabelText(strings.composer.placeholder)).toBeDisabled();
  });

  it('draws nothing for a value the device has not seen', () => {
    renderComposer({ control: 'shared', model: null, effort: null });

    expect(document.querySelectorAll('.composer-chip.readonly')).toHaveLength(1);
    expect(chip(strings.composer.permissionMode, 'Ask before edits')).toBeInTheDocument();
  });

  it('shows an id the agent does not advertise by its id', () => {
    // `auto` is a real Claude permission mode the device does not list, and
    // the chip that shows it is the terminal's (A40).
    renderComposer({ control: 'terminal', permission_mode: 'auto', model: 'claude-opus-5[1m]' });

    expect(chip(strings.composer.permissionMode, 'auto')).toBeInTheDocument();
    expect(chip(strings.composer.modelCard, 'claude-opus-5[1m] High')).toBeInTheDocument();
  });

  it('shows them with no agent at all, by their ids', () => {
    renderComposer({ control: 'terminal' }, null);

    expect(chip(strings.composer.modelCard, 'claude-sonnet-4-5 high')).toBeInTheDocument();
  });

  /** A21: a Codex thread that was switched to the faster tier in its terminal. */
  it('names the tier on the read-only chip of a terminal-held Codex session', () => {
    renderComposer(
      {
        agent: 'codex',
        control: 'terminal',
        model: 'gpt-5.4-codex',
        effort: 'medium',
        speed: 'priority',
      },
      codexAgent,
    );

    const held = chip(strings.composer.modelCard, 'GPT-5.4 Codex Medium · Fast');
    expect(held).toBeInTheDocument();
    expect(held).toHaveTextContent('GPT-5.4 Codex Medium');
    // The lightning is the only mark of the tier in the row itself.
    expect(held?.querySelector('.speed-glyph')).not.toBeNull();
  });

  it('keeps the card on a session the device drives', () => {
    renderComposer({ control: 'remote' });

    expect(screen.getByRole('button', { name: strings.composer.modelCard })).toBeInTheDocument();
    expect(document.querySelectorAll('.composer-chip.readonly')).toHaveLength(0);
  });

  it('keeps the card on a shared agent that carries the settings', () => {
    renderComposer({ control: 'shared', agent: 'codex', model: 'gpt-5.4' }, codexAgent);

    expect(screen.getByRole('button', { name: strings.composer.modelCard })).toBeInTheDocument();
    expect(document.querySelectorAll('.composer-chip.readonly')).toHaveLength(0);
  });

  /**
   * A17 for a session the terminal holds, and A40 for the shared one: either
   * way the row reads what the terminal last confirmed, never what an app
   * wishes were true.
   */
  it('follows a meta event that changes the model', () => {
    const session = { ...baseSession, control: 'terminal' as const };
    const { rerender } = renderComposer(session);
    expect(chip(strings.composer.modelCard, 'Sonnet 4.5 High')).toBeInTheDocument();

    // What the socket does with a `/model` typed in the terminal.
    const next = foldSession(session, [
      { seq: 9, ts: 9, kind: 'meta', model: 'claude-haiku-4-5' } as SessionEvent,
    ]);
    rerender(
      <Composer
        session={next}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn().mockResolvedValue(undefined)}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );

    expect(chip(strings.composer.modelCard, 'Haiku 4.5 High')).toBeInTheDocument();
    expect(chip(strings.composer.modelCard, 'Sonnet 4.5 High')).toBeNull();
  });
});

/**
 * A21 — the model card. `docs/DESIGN.md` § "The composer": one chip for what
 * runs and how hard, a card with the tier and the model on one row and the
 * effort slider under it.
 */
describe('Composer model card', () => {
  const renderComposer = (session: Partial<Session>, agent: AgentInfo = claudeAgent) => {
    const onSetOption = vi.fn();
    render(
      <Composer
        session={{ ...baseSession, ...session }}
        agent={agent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSend={vi.fn().mockResolvedValue(undefined)}
        onSetOption={onSetOption}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    return { onSetOption };
  };

  const openCard = async () => {
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: strings.composer.modelCard }));
    return user;
  };

  /** The effort word actually drawn, rather than one of the hidden sizers. */
  const shownWord = () => document.querySelector('.sized-box-shown [data-effort-word]');

  it('reads the model and the effort in one chip', () => {
    renderComposer({});
    expect(screen.getByRole('button', { name: strings.composer.modelCard })).toHaveTextContent(
      'Sonnet 4.5 High',
    );
    expect(document.querySelector('.model-card-chip .sized-box-shown .speed-glyph')).toBeNull();
  });

  it('marks the chip with the lightning while a tier is on', () => {
    renderComposer({ agent: 'codex', model: 'gpt-5.4-codex', speed: 'priority' }, codexAgent);
    expect(
      document.querySelector('.model-card-chip .sized-box-shown .speed-glyph'),
    ).not.toBeNull();
  });

  it('opens a card with the model name and the effort slider', async () => {
    renderComposer({});
    await openCard();

    expect(
      screen.getByRole('button', {
        name: strings.composer.option(strings.composer.model, 'Sonnet 4.5'),
      }),
    ).toBeInTheDocument();
    const slider = screen.getByRole('slider', { name: strings.composer.effort });
    expect(slider).toHaveValue('2');
    expect(slider).toHaveAttribute('aria-valuetext', 'High');
  });

  it('follows the thumb with the word and sends only on release', async () => {
    const { onSetOption } = renderComposer({});
    await openCard();
    const slider = screen.getByRole('slider', { name: strings.composer.effort });

    fireEvent.input(slider, { target: { value: '0' } });
    expect(slider).toHaveAttribute('aria-valuetext', 'Low');
    // The word is the thumb's, not the device's: it changes before the value
    // is even sent.
    expect(shownWord()).toHaveTextContent('Low');
    expect(onSetOption).not.toHaveBeenCalled();

    fireEvent.change(slider, { target: { value: '0' } });
    expect(onSetOption).toHaveBeenCalledWith({ effort: 'low' });
  });

  it('draws one dot per effort level and fills them up to the thumb', async () => {
    renderComposer({});
    await openCard();

    const dots = document.querySelectorAll('.effort-dot');
    expect(dots).toHaveLength(claudeAgent.efforts.length);
    // "High" is the last of three, so every dot is on the filled side.
    expect(document.querySelectorAll('.effort-dot.filled')).toHaveLength(3);
    expect(document.querySelector('.effort-fill')).not.toBeNull();

    // Nothing is filled at the lowest stop, and no cap is left to peek out
    // around the thumb.
    fireEvent.input(screen.getByRole('slider', { name: strings.composer.effort }), {
      target: { value: '0' },
    });
    expect(document.querySelectorAll('.effort-dot.filled')).toHaveLength(0);
    expect(document.querySelector('.effort-fill')).toBeNull();
  });

  it('sizes the chip and the name row on every model-and-effort pair', async () => {
    renderComposer({});
    const chip = screen.getByRole('button', { name: strings.composer.modelCard });
    // Three models times three levels, laid out under the visible label so the
    // chip cannot change width when either of them changes.
    expect(chip.querySelectorAll('.sized-box-ghost')).toHaveLength(9);
    expect(chip.querySelectorAll('.sized-box-ghost[aria-hidden="true"]')).toHaveLength(9);

    await openCard();
    const name = screen.getByRole('button', {
      name: strings.composer.option(strings.composer.model, 'Sonnet 4.5'),
    });
    expect(name.querySelectorAll('.sized-box-ghost')).toHaveLength(9);
    expect(name.querySelector('.sized-box-ghost')).toHaveTextContent(/Sonnet 4\.5\s*Low/);
  });

  it('reserves the width of the lightning for an agent that lists tiers', () => {
    renderComposer({ agent: 'codex', model: 'gpt-5.4-codex', effort: 'medium' }, codexAgent);
    // Standard speed, so nothing is lit — but the box is already as wide as it
    // will be once a tier is on.
    expect(document.querySelector('.sized-box-shown .speed-glyph')).toBeNull();
    expect(document.querySelectorAll('.sized-box-ghost .speed-glyph')).toHaveLength(6);
  });

  it('draws no slider for an agent with no effort levels', async () => {
    renderComposer({}, { ...claudeAgent, efforts: [], default_effort: null });
    await openCard();
    expect(screen.queryByRole('slider')).not.toBeInTheDocument();
  });

  it('changes the model from the list the card opens', async () => {
    const { onSetOption } = renderComposer({});
    const user = await openCard();

    await user.click(
      screen.getByRole('button', {
        name: strings.composer.option(strings.composer.model, 'Sonnet 4.5'),
      }),
    );
    await user.click(screen.getByRole('option', { name: 'Haiku 4.5' }));

    expect(onSetOption).toHaveBeenCalledWith({ model: 'claude-haiku-4-5' });
  });

  it('offers no speed toggle for an agent that lists no tiers', async () => {
    expect(claudeAgent.speeds).toBeUndefined();
    renderComposer({});
    await openCard();
    expect(document.querySelector('.speed-toggle')).toBeNull();
  });

  it('cycles standard → the tier → standard for Codex', async () => {
    expect(codexAgent.speeds).toEqual([{ id: 'priority', label: 'Fast' }]);
    const { onSetOption } = renderComposer(
      { agent: 'codex', model: 'gpt-5.4-codex', effort: 'medium' },
      codexAgent,
    );
    const user = await openCard();

    const toggle = screen.getByRole('button', {
      name: strings.composer.speed(strings.composer.speedStandard),
    });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await user.click(toggle);
    expect(onSetOption).toHaveBeenCalledWith({ speed: 'priority' });
  });

  it('turns the tier back off from the lit toggle', async () => {
    const { onSetOption } = renderComposer(
      { agent: 'codex', model: 'gpt-5.4-codex', effort: 'medium', speed: 'priority' },
      codexAgent,
    );
    const user = await openCard();

    const toggle = screen.getByRole('button', { name: strings.composer.speed('Fast') });
    expect(toggle).toHaveAttribute('aria-pressed', 'true');
    await user.click(toggle);
    expect(onSetOption).toHaveBeenCalledWith({ speed: null });
  });
});
