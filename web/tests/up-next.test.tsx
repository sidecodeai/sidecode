/**
 * A43 — `docs/DESIGN.md` § "Up next": the queue is one control, and a queued
 * message can be taken back. The chip says how many messages wait, the list it
 * opens removes or edits them, and an edit takes the message out of the line
 * before the field takes its words, then puts them back in the place they left.
 */
import { describe, expect, it, vi, type Mock } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from '../src/features/chat/Composer';
import { SEND_DELAY_MS } from '../src/features/chat/useImeGuard';
import type { Send } from '../src/features/chat/useQueuedEdit';
import type { AttachmentDraft } from '../src/features/chat/attachments';
import { RequestError } from '../src/lib/ws';
import { useDrafts } from '../src/stores/drafts';
import { sessionKey } from '../src/stores/sessions';
import { strings } from '../src/strings';
import { claudeAgent, codexAgent, commandsFor } from '../mock/fixtures';
import type {
  AgentInfo,
  Command,
  QuestionEvent,
  QueuedMessage,
  Session,
  SessionState,
} from '../src/protocol/types';

const baseSession: Session = {
  session_id: 'ses-queue',
  device_id: 'dev-mac',
  agent: 'claude',
  title: 'Migrate web to Vite 6',
  cwd: '/Users/me/dev/remote-control/web',
  git: null,
  state: 'running',
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
  queued: 3,
};

const KEY = sessionKey(baseSession.device_id, baseSession.session_id);

const QUEUE: QueuedMessage[] = [
  { id: 'q-1', text: 'Then run the full test suite.', ts: 1_000 },
  { id: 'q-2', text: 'These two screenshots show the drawer.', ts: 2_000, attachments: 2 },
  { id: 'q-3', text: 'After that, bump Vite.', ts: 3_000 },
];

const FILE: AttachmentDraft = { name: 'trace.log', mime: 'text/plain', size: 4, data_base64: 'AAAA' };

const QUESTION = {
  seq: 4,
  ts: 1,
  kind: 'question',
  block_id: 'q-block',
  request_id: 'req-1',
  status: 'pending',
  questions: [{ id: 'q1', prompt: 'Which branch?', options: [], multi: false, allow_text: true }],
} as unknown as QuestionEvent;

interface Setup {
  state?: SessionState;
  control?: Session['control'];
  deviceOnline?: boolean;
  agent?: AgentInfo;
  queue?: QueuedMessage[];
  question?: QuestionEvent | null;
  commands?: Command[];
  onTakeQueued?: (queuedId: string) => Promise<void>;
  onSend?: Mock<Send>;
}

function setup(overrides: Setup = {}) {
  const agent = overrides.agent ?? claudeAgent;
  const onSend = overrides.onSend ?? vi.fn<Send>().mockResolvedValue(undefined);
  const onTakeQueued = vi.fn(overrides.onTakeQueued ?? (() => Promise.resolve()));
  const onRemoveQueued = vi.fn();
  const props = (queue: QueuedMessage[], state: SessionState) => ({
    session: {
      ...baseSession,
      agent: agent.agent,
      state,
      ...(overrides.control ? { control: overrides.control } : {}),
    },
    agent,
    deviceOnline: overrides.deviceOnline ?? true,
    queue,
    question: overrides.question ?? null,
    sttEnabled: false,
    commands: overrides.commands ?? [],
    onSend,
    onAnswer: vi.fn().mockResolvedValue(undefined),
    onSetOption: vi.fn(),
    onRemoveQueued,
    onTakeQueued,
    onTakeover: vi.fn(),
  });
  const view = render(
    <Composer {...props(overrides.queue ?? QUEUE, overrides.state ?? 'running')} />,
  );
  const rerender = (queue: QueuedMessage[], state: SessionState = overrides.state ?? 'running') =>
    view.rerender(<Composer {...props(queue, state)} />);
  return { onSend, onTakeQueued, onRemoveQueued, rerender, user: userEvent.setup() };
}

const field = () => screen.getByLabelText(strings.composer.placeholder) as HTMLTextAreaElement;
const chip = (count = QUEUE.length) =>
  screen.getByRole('button', { name: strings.composer.upNextCount(count) });
const rows = () => [...document.querySelectorAll<HTMLElement>('.up-next-row')];
const strip = () => screen.queryByText(strings.composer.editingQueued);
const editing = () => useDrafts.getState().drafts[KEY]?.editing ?? null;

function deferred() {
  let resolve: () => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Opens the list and taps the row that holds `text`. */
async function tapRow(user: ReturnType<typeof userEvent.setup>, text: string, count?: number) {
  await user.click(chip(count));
  await user.click(screen.getByRole('button', { name: text }));
}

/** Taps the first message and waits for the field to hold it. */
async function editFirst(user: ReturnType<typeof userEvent.setup>) {
  await tapRow(user, QUEUE[0]!.text);
  await waitFor(() => expect(field()).toHaveValue(QUEUE[0]!.text));
}

describe('the Up next chip', () => {
  it('is one chip, the first of the control row, and only while something waits', () => {
    const { rerender } = setup();

    // A44: first from the leading edge, before the model card.
    const row = document.querySelector('.composer-bottom');
    expect(row?.firstElementChild).toContainElement(chip());
    // A count, never the messages themselves stacked over the field.
    expect(screen.queryByText(QUEUE[0]!.text)).not.toBeInTheDocument();
    expect(document.querySelector('.queue-list')).toBeNull();

    rerender([]);
    expect(screen.queryByRole('button', { name: /Up next/ })).not.toBeInTheDocument();
  });

  it('opens the list in delivery order, with the files a message carries', async () => {
    const { user } = setup();
    await user.click(chip());

    expect(screen.getByText(strings.composer.upNext)).toBeInTheDocument();
    expect(rows().map((row) => row.querySelector('.up-next-words')?.textContent)).toEqual(
      QUEUE.map((item) => item.text),
    );
    // The full words are one hover away when the line is cut short.
    expect(rows()[0]?.querySelector('.up-next-message')).toHaveAttribute('title', QUEUE[0]!.text);
    // A paperclip and a count; the files are on the device, so only × acts.
    const withFiles = within(rows()[1]!);
    expect(withFiles.getByText(strings.chat.attachments(2))).toBeInTheDocument();
    expect(withFiles.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      strings.chat.queuedRemove,
    ]);
  });

  it('takes a message out of the line with its × and leaves the list open', async () => {
    const { user, onRemoveQueued, onTakeQueued } = setup();
    await user.click(chip());
    await user.click(within(rows()[2]!).getByRole('button', { name: strings.chat.queuedRemove }));

    expect(onRemoveQueued).toHaveBeenCalledWith('q-3');
    expect(onTakeQueued).not.toHaveBeenCalled();
    expect(document.querySelector('.up-next')).not.toBeNull();
  });

  it('goes, open list and all, when the last message leaves the line', async () => {
    const { user, rerender } = setup({ queue: [QUEUE[0]!] });
    await user.click(chip(1));
    expect(document.querySelector('.up-next')).not.toBeNull();

    rerender([]);
    expect(document.querySelector('.up-next')).toBeNull();
  });

  it.each([
    ['the terminal holds the session', { control: 'terminal' as const }],
    ['the device is offline', { deviceOnline: false }],
  ])('offers Remove alone while %s', async (_, overrides) => {
    const { user, onRemoveQueued } = setup(overrides);
    await user.click(chip());

    for (const row of rows()) {
      expect(within(row).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
        strings.chat.queuedRemove,
      ]);
    }
    await user.click(within(rows()[0]!).getByRole('button', { name: strings.chat.queuedRemove }));
    expect(onRemoveQueued).toHaveBeenCalledWith('q-1');
  });
});

describe('editing a queued message', () => {
  it('takes the message out of the line first, then sets the draft aside for its words', async () => {
    const take = deferred();
    const { user, onTakeQueued } = setup({ onTakeQueued: () => take.promise });
    await user.click(field());
    await user.keyboard('half a thought');
    useDrafts.getState().addAttachments(KEY, [FILE]);

    await tapRow(user, QUEUE[0]!.text);
    expect(onTakeQueued).toHaveBeenCalledWith('q-1');
    // The list closes at once; the field waits for the device to let go.
    expect(document.querySelector('.up-next')).toBeNull();
    expect(field()).toHaveValue('half a thought');
    expect(strip()).not.toBeInTheDocument();

    take.resolve();
    await waitFor(() => expect(field()).toHaveValue(QUEUE[0]!.text));
    expect(strip()).toBeInTheDocument();
    expect(screen.queryByText('trace.log')).not.toBeInTheDocument();
    expect(editing()).toEqual({
      ts: 1_000,
      original: QUEUE[0]!.text,
      aside: { text: 'half a thought', attachments: [FILE] },
      sending: false,
    });
    // The field is in hand, with the caret after the words.
    expect(field()).toHaveFocus();
    expect(field().selectionStart).toBe(QUEUE[0]!.text.length);
  });

  it('opens nothing and says so when the device had already sent the message', async () => {
    const { user } = setup({
      onTakeQueued: () =>
        Promise.reject(new RequestError({ code: 'not_found', message: 'that message is not queued' })),
    });
    await user.click(field());
    await user.keyboard('half a thought');

    await tapRow(user, QUEUE[0]!.text);
    expect(await screen.findByText(strings.composer.alreadySent)).toBeInTheDocument();
    expect(field()).toHaveValue('half a thought');
    expect(strip()).not.toBeInTheDocument();
    expect(editing()).toBeNull();
  });

  it('puts the edited words back under the entry’s ts, as Queue even behind a steering agent', async () => {
    expect(codexAgent.capabilities).toContain('steer');
    const { user, onSend } = setup({ agent: codexAgent });
    await editFirst(user);

    await user.keyboard(' And the linters.');
    await user.click(screen.getByRole('button', { name: strings.composer.queue }));

    expect(onSend).toHaveBeenCalledWith(
      'Then run the full test suite. And the linters.',
      [],
      'queue',
      1_000,
    );
    await waitFor(() => expect(strip()).not.toBeInTheDocument());
    expect(field()).toHaveValue('');
    expect(editing()).toBeNull();
  });

  it('gives the field back what it held once the words are back in the line', async () => {
    const { user } = setup();
    await user.click(field());
    await user.keyboard('half a thought');
    useDrafts.getState().addAttachments(KEY, [FILE]);
    await editFirst(user);

    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    await waitFor(() => expect(field()).toHaveValue('half a thought'));
    expect(screen.getByText('trace.log')).toBeInTheDocument();
    expect(strip()).not.toBeInTheDocument();
  });

  it('puts the original words back on Cancel', async () => {
    const { user, onSend } = setup();
    await editFirst(user);
    await user.keyboard(' Words I no longer want.');

    await user.click(screen.getByRole('button', { name: strings.common.cancel }));
    expect(onSend).toHaveBeenCalledWith(QUEUE[0]!.text, [], 'queue', 1_000);
    await waitFor(() => expect(strip()).not.toBeInTheDocument());
    expect(field()).toHaveValue('');
  });

  it('keeps editing, with the draft still aside, when the words are refused', async () => {
    const onSend = vi
      .fn<Send>()
      .mockRejectedValue(new RequestError({ code: 'device_offline', message: '' }));
    const { user } = setup({ onSend });
    await user.click(field());
    await user.keyboard('half a thought');
    await editFirst(user);
    await user.keyboard(' Edited.');

    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    expect(await screen.findByText(strings.errors.deviceOffline)).toBeInTheDocument();
    expect(field()).toHaveValue(`${QUEUE[0]!.text} Edited.`);
    expect(strip()).toBeInTheDocument();
    expect(editing()?.aside.text).toBe('half a thought');
    expect(editing()?.sending).toBe(false);
    expect(screen.getByRole('button', { name: strings.composer.queue })).toBeEnabled();
  });

  it('holds still while the words are on their way back, so nothing goes twice', async () => {
    const back = deferred();
    const onSend = vi.fn<Send>(() => back.promise);
    const { user } = setup({ onSend });
    await editFirst(user);

    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    expect(screen.getByRole('status', { name: strings.chat.sending })).toBeInTheDocument();
    expect(field()).toHaveAttribute('readonly');
    expect(screen.getByRole('button', { name: strings.common.cancel })).toBeDisabled();
    await user.keyboard('{Enter}');
    await new Promise((resolve) => setTimeout(resolve, SEND_DELAY_MS + 20));
    expect(onSend).toHaveBeenCalledTimes(1);

    back.resolve();
    await waitFor(() => expect(strip()).not.toBeInTheDocument());
    expect(field()).not.toHaveAttribute('readonly');
  });

  it('sends when the turn ended meanwhile, still under the entry’s ts, and Enter is the button', async () => {
    const { user, onSend, rerender } = setup();
    await editFirst(user);
    rerender(QUEUE.slice(1), 'idle');

    // Idle: the button is Send's arrow again, and a `queue` message goes at once.
    expect(screen.queryByRole('button', { name: strings.composer.queue })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeEnabled();
    await user.click(field());
    await user.keyboard('{Enter}');
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(QUEUE[0]!.text, [], 'queue', 1_000));
  });

  it('interrupts with the edited words from the ⋯ menu, which keeps no place in the line', async () => {
    const { user, onSend } = setup();
    await editFirst(user);

    await user.click(screen.getByRole('button', { name: strings.composer.sendOptions }));
    await user.click(screen.getByRole('button', { name: strings.composer.interruptAndSend }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0]).toEqual([QUEUE[0]!.text, [], 'interrupt']);
    await waitFor(() => expect(strip()).not.toBeInTheDocument());
  });

  it('sends the files attached while editing with the edited words', async () => {
    const { user, onSend } = setup();
    await editFirst(user);
    useDrafts.getState().addAttachments(KEY, [FILE]);

    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    expect(onSend).toHaveBeenCalledWith(QUEUE[0]!.text, [FILE], 'queue', 1_000);
  });

  it('is a message and nothing else: no command panel opens', async () => {
    const { user } = setup({ agent: codexAgent, commands: commandsFor('codex') });
    await editFirst(user);

    await user.clear(field());
    await user.keyboard('/');
    expect(screen.queryByRole('listbox', { name: strings.commands.menu })).not.toBeInTheDocument();
  });

  it('keeps the field while a question waits, and gives it back to the question after', async () => {
    const { user } = setup({ question: QUESTION });
    expect(screen.getByRole('button', { name: strings.composer.answer })).toBeInTheDocument();

    await editFirst(user);
    expect(screen.queryByRole('button', { name: strings.composer.answer })).not.toBeInTheDocument();
    expect(field()).not.toHaveAttribute('placeholder', strings.composer.placeholderAnswer);

    await user.click(screen.getByRole('button', { name: strings.composer.queue }));
    await waitFor(() => expect(strip()).not.toBeInTheDocument());
    expect(field()).toHaveAttribute('placeholder', strings.composer.placeholderAnswer);
  });

  it('offers no second edit while one is open', async () => {
    const { user, rerender } = setup();
    await editFirst(user);
    rerender(QUEUE.slice(1));

    await user.click(chip(2));
    expect(screen.queryByRole('button', { name: QUEUE[2]!.text })).not.toBeInTheDocument();
    expect(within(rows()[1]!).getByRole('button', { name: strings.chat.queuedRemove })).toBeEnabled();
  });
});
