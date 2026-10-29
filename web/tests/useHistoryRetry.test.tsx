/**
 * `docs/DESIGN.md` § "When the history cannot be read": a conversation that
 * could not be read is asked for again by itself — at once when its device
 * comes back online, and after a short, growing pause while the device is
 * online and the read keeps failing. An offline device is not asked at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { RETRY_DELAYS_MS, useHistoryRetry } from '../src/features/chat/useHistoryRetry';

const retry = vi.fn();

interface Props {
  key?: string;
  failed: boolean;
  online: boolean;
}

/**
 * One pause at a time: React draws the state a retry leaves behind, and
 * schedules the next pause from it, only once the act around the timer ends.
 */
const pass = (ms: number) => act(() => vi.advanceTimersByTime(ms));

const render = (initial: Props) =>
  renderHook(
    (props: Props) => useHistoryRetry(props.key ?? 'dev/ses', props.failed, props.online, retry),
    { initialProps: initial },
  );

beforeEach(() => {
  retry.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('while the device is online', () => {
  it('asks again after 2, 4, 8 and then 15 seconds, and stops after five tries', () => {
    expect(RETRY_DELAYS_MS).toEqual([2_000, 4_000, 8_000, 15_000, 15_000]);
    render({ failed: true, online: true });

    let elapsed = 0;
    for (const [index, delay] of RETRY_DELAYS_MS.entries()) {
      act(() => vi.advanceTimersByTime(delay - 1));
      expect(retry).toHaveBeenCalledTimes(index);
      act(() => vi.advanceTimersByTime(1));
      expect(retry).toHaveBeenCalledTimes(index + 1);
      elapsed += delay;
    }
    expect(elapsed).toBe(44_000);

    act(() => vi.advanceTimersByTime(10 * 60_000));
    expect(retry).toHaveBeenCalledTimes(5);
    expect(retry).toHaveBeenCalledWith('dev/ses');
  });

  it('asks nothing of a conversation that was read', () => {
    render({ failed: false, online: true });
    act(() => vi.advanceTimersByTime(60_000));
    expect(retry).not.toHaveBeenCalled();
  });

  it('stops asking the moment the read succeeds', () => {
    const { rerender } = render({ failed: true, online: true });
    act(() => vi.advanceTimersByTime(2_000));
    expect(retry).toHaveBeenCalledTimes(1);

    rerender({ failed: false, online: true });
    act(() => vi.advanceTimersByTime(60_000));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('starts the schedule over for a new failure', () => {
    const { rerender } = render({ failed: true, online: true });
    pass(2_000);
    pass(4_000);
    expect(retry).toHaveBeenCalledTimes(2);

    rerender({ failed: false, online: true });
    rerender({ failed: true, online: true });
    pass(2_000);
    expect(retry).toHaveBeenCalledTimes(3);
  });

  it('stops asking when the pane is closed', () => {
    const { unmount } = render({ failed: true, online: true });
    unmount();
    act(() => vi.advanceTimersByTime(60_000));
    expect(retry).not.toHaveBeenCalled();
  });
});

describe('while the device is offline', () => {
  it('does not ask on a timer, because there is nobody to answer', () => {
    render({ failed: true, online: false });
    act(() => vi.advanceTimersByTime(10 * 60_000));
    expect(retry).not.toHaveBeenCalled();
  });

  it('asks at once when the device comes back, and then on the schedule', () => {
    const { rerender } = render({ failed: true, online: false });
    act(() => vi.advanceTimersByTime(30_000));

    rerender({ failed: true, online: true });
    expect(retry).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(2_000));
    expect(retry).toHaveBeenCalledTimes(2);
  });

  it('does not ask a device that came back when nothing had failed', () => {
    const { rerender } = render({ failed: false, online: false });
    rerender({ failed: false, online: true });
    expect(retry).not.toHaveBeenCalled();
  });

  it('gives a device that keeps dropping a fresh schedule each time it returns', () => {
    const { rerender } = render({ failed: true, online: true });
    for (const delay of RETRY_DELAYS_MS) pass(delay);
    expect(retry).toHaveBeenCalledTimes(5);

    rerender({ failed: true, online: false });
    rerender({ failed: true, online: true });
    expect(retry).toHaveBeenCalledTimes(6);
    pass(2_000);
    expect(retry).toHaveBeenCalledTimes(7);
  });
});

/**
 * The page keeps one instance across a move from one conversation to another, so the
 * schedule has to belong to the conversation and not to the pane.
 */
describe('another conversation in the same pane', () => {
  it('gets its own schedule when it fails the same way after the first used up its five tries', () => {
    const { rerender } = render({ key: 'dev/a', failed: true, online: true });
    for (const delay of RETRY_DELAYS_MS) pass(delay);
    expect(retry).toHaveBeenCalledTimes(5);

    rerender({ key: 'dev/b', failed: true, online: true });
    pass(2_000);
    expect(retry).toHaveBeenCalledTimes(6);
    expect(retry).toHaveBeenLastCalledWith('dev/b');
  });

  it('does not inherit the pauses the first one had gone through', () => {
    const { rerender } = render({ key: 'dev/a', failed: true, online: true });
    pass(2_000);
    pass(4_000);
    expect(retry).toHaveBeenCalledTimes(2);

    rerender({ key: 'dev/b', failed: true, online: true });
    // The first pause again, not the third.
    pass(1_999);
    expect(retry).toHaveBeenCalledTimes(2);
    pass(1);
    expect(retry).toHaveBeenCalledTimes(3);
  });

  it('does not take the move to a conversation of an online device for that device coming back', () => {
    const { rerender } = render({ key: 'dev-a/s', failed: true, online: false });
    rerender({ key: 'dev-b/s', failed: true, online: true });
    expect(retry).not.toHaveBeenCalled();

    pass(2_000);
    expect(retry).toHaveBeenCalledWith('dev-b/s');
  });
});

it('does not ask a conversation that is only just opened to retry', () => {
  render({ failed: false, online: true });
  expect(retry).not.toHaveBeenCalled();
});
