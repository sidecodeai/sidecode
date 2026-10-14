/**
 * A35 — the notice a session with a pending resume carries above its
 * transcript, and the two actions the ruling allows and no more
 * (`docs/DESIGN.md` § "Paused by the usage limit").
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ResumeNotice } from '../src/features/chat/ResumeNotice';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import type { SessionResume } from '../src/protocol/types';

const at = (h: number, m: number, dayOffset = 0): number =>
  new Date(2026, 8, 17 + dayOffset, h, m).getTime();

/** Far enough ahead that the picker's own bounds never come into it. */
const later = (): number => Date.now() + 90 * 60_000;

const pending = (partial: Partial<SessionResume> = {}): SessionResume => ({
  at: later(),
  estimated: false,
  attempts: 0,
  window_minutes: 300,
  ...partial,
});

function show(resume: SessionResume, onSet = vi.fn(async () => undefined), onCancel = vi.fn()) {
  render(<ResumeNotice resume={resume} onSet={onSet} onCancel={onCancel} />);
  return { onSet, onCancel };
}

afterEach(() => {
  cleanup();
  useSettings.setState({ language: 'en' });
});

describe('the pending-resume notice', () => {
  it('says it was paused by the usage limit and when it resumes', () => {
    show(pending({ at: at(15, 50) }));
    expect(screen.getByRole('status').textContent).toContain(
      'Paused by the usage limit · resumes',
    );
  });

  it('offers Change and Cancel, and nothing else', () => {
    show(pending());
    const buttons = [...screen.getByRole('status').querySelectorAll('button')].map(
      (button) => button.textContent,
    );
    expect(buttons).toEqual([strings.chat.resumeChange, strings.chat.resumeCancel]);
  });

  it('cancels at once, with no confirmation', async () => {
    const user = userEvent.setup();
    const { onCancel } = show(pending());

    await user.click(screen.getByRole('button', { name: strings.chat.resumeCancel }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('opens a time picker prefilled with the time the device holds', async () => {
    const user = userEvent.setup();
    const resume = pending();
    show(resume);

    await user.click(screen.getByRole('button', { name: strings.chat.resumeChange }));

    const field = screen.getByLabelText(strings.chat.resumeAt) as HTMLInputElement;
    expect(field.type).toBe('datetime-local');
    expect(new Date(field.value).getTime()).toBe(Math.floor(resume.at / 60_000) * 60_000);
  });

  it('sends the time the reader picked', async () => {
    const user = userEvent.setup();
    const { onSet } = show(pending());
    const wanted = new Date(Date.now() + 3 * 3_600_000);
    wanted.setSeconds(0, 0);

    await user.click(screen.getByRole('button', { name: strings.chat.resumeChange }));
    const field = screen.getByLabelText(strings.chat.resumeAt);
    await user.clear(field);
    await user.type(field, toInputValue(wanted));
    await user.click(screen.getByRole('button', { name: strings.chat.resumeSet }));

    await waitFor(() => expect(onSet).toHaveBeenCalledWith(wanted.getTime()));
  });

  it('refuses a time less than a minute away and sends nothing', async () => {
    const user = userEvent.setup();
    const { onSet } = show(pending());

    await user.click(screen.getByRole('button', { name: strings.chat.resumeChange }));
    const field = screen.getByLabelText(strings.chat.resumeAt);
    await user.clear(field);
    await user.type(field, toInputValue(new Date(Date.now() - 3_600_000)));
    await user.click(screen.getByRole('button', { name: strings.chat.resumeSet }));

    expect(await screen.findByText(strings.chat.resumeTooSoon)).toBeInTheDocument();
    expect(onSet).not.toHaveBeenCalled();
  });

  it('refuses a time more than eight days out and sends nothing', async () => {
    const user = userEvent.setup();
    const { onSet } = show(pending());

    await user.click(screen.getByRole('button', { name: strings.chat.resumeChange }));
    const field = screen.getByLabelText(strings.chat.resumeAt);
    await user.clear(field);
    await user.type(field, toInputValue(new Date(Date.now() + 9 * 24 * 3_600_000)));
    await user.click(screen.getByRole('button', { name: strings.chat.resumeSet }));

    expect(await screen.findByText(strings.chat.resumeTooFar)).toBeInTheDocument();
    expect(onSet).not.toHaveBeenCalled();
  });

  it("says so inside the picker when the device refuses the time", async () => {
    const user = userEvent.setup();
    const onSet = vi.fn(async () => {
      throw new Error('conflict');
    });
    show(pending(), onSet);

    await user.click(screen.getByRole('button', { name: strings.chat.resumeChange }));
    await user.click(screen.getByRole('button', { name: strings.chat.resumeSet }));

    expect(await screen.findByText(strings.errors.resumeSetFailed)).toBeInTheDocument();
  });

  it('is said in the interface language', () => {
    useSettings.setState({ language: 'zh-Hans' });
    show(pending());
    expect(screen.getByRole('status').textContent).toContain('已因用量限额暂停');
  });
});

/** What `userEvent.type` must be given for a `datetime-local` field. */
function toInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
