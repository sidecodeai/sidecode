/**
 * A65 — the permission an AI feature asks for, in the protocol's order: the title, what the feature
 * is for, its points as a list, the paragraphs after them, then Not now, which has the focus, beside
 * Agree and continue. Agreeing runs what the caller keeps and closes; a refusal of it stays in the
 * open dialog; Not now, Escape or a press outside runs nothing but the caller's own decline.
 */
import type { ReactNode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ConsentDialog } from '../src/features/consent/ConsentDialog';
import { strings } from '../src/strings';

const POINTS = ['What you say goes to Example AI.', 'Nothing else goes with it.', 'It becomes a draft.'];
const PARAGRAPHS = ['The gateway keeps nothing.', 'You can withdraw this permission in Settings.'];
const c = strings.consent;

function ask(over: Partial<Parameters<typeof ConsentDialog>[0]> = {}) {
  const onAgree = vi.fn();
  const onClose = vi.fn();
  const onDecline = vi.fn();
  const wrap = (node: ReactNode) => render(<MemoryRouter>{node}</MemoryRouter>);
  wrap(
    <ConsentDialog
      open
      title="Turn on dictation?"
      lead="Dictation turns what you say into text."
      points={POINTS}
      paragraphs={PARAGRAPHS}
      onAgree={onAgree}
      onClose={onClose}
      onDecline={onDecline}
      {...over}
    />,
  );
  return { onAgree, onClose, onDecline, dialog: screen.getByRole('dialog', { name: 'Turn on dictation?' }) };
}

describe('the permission dialog', () => {
  it('draws its lines in order: what it is for, the points, then the paragraphs', () => {
    const { dialog } = ask();

    expect(within(dialog).getByText('Dictation turns what you say into text.')).toBeInTheDocument();
    expect(within(dialog).getAllByRole('listitem').map((item) => item.textContent)).toEqual(POINTS);
    const paragraphs = [...dialog.querySelectorAll('.consent-paragraph')].map((p) => p.textContent);
    expect(paragraphs).toEqual(PARAGRAPHS);
  });

  it('links nothing', () => {
    const { dialog } = ask();

    expect(within(dialog).queryByRole('link')).toBeNull();
  });

  it('puts the focus on Not now', () => {
    ask();

    expect(screen.getByRole('button', { name: c.notNow })).toHaveFocus();
  });

  it('runs what agreeing keeps, then closes', async () => {
    const { onAgree, onClose, onDecline } = ask();

    await userEvent.click(screen.getByRole('button', { name: c.agree }));

    expect(onAgree).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onDecline).not.toHaveBeenCalled();
  });

  it('holds Agree while what it keeps is on its way', async () => {
    let done: () => void = () => undefined;
    const { onClose } = ask({ onAgree: () => new Promise<void>((resolve) => (done = resolve)) });

    await userEvent.click(screen.getByRole('button', { name: c.agree }));
    expect(screen.getByRole('button', { name: c.agree })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();

    done();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('says a refusal in the dialog and stays open', async () => {
    const { onClose } = ask({ onAgree: () => Promise.reject(new Error('refused')) });

    await userEvent.click(screen.getByRole('button', { name: c.agree }));

    expect(await screen.findByRole('alert')).toHaveTextContent(strings.errors.setFailed);
    expect(screen.getByRole('dialog', { name: 'Turn on dictation?' })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('declines on Not now and on Escape, running nothing of agreeing', async () => {
    const first = ask();
    await userEvent.click(screen.getByRole('button', { name: c.notNow }));
    expect(first.onDecline).toHaveBeenCalledTimes(1);
    expect(first.onClose).toHaveBeenCalledTimes(1);

    await userEvent.keyboard('{Escape}');
    expect(first.onDecline).toHaveBeenCalledTimes(2);
    expect(first.onAgree).not.toHaveBeenCalled();
  });

  it('draws no lead it was not given', () => {
    const { dialog } = ask({ lead: undefined });

    expect(dialog.querySelector('.consent-body > p.hint')).toBeNull();
  });
});
