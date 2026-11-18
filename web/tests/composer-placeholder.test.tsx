/**
 * The empty field grows to show its whole placeholder, the way it grows for
 * words (`src/features/chat/fieldFit.ts`). On a 390 px phone "Message will be
 * queued…" wraps beside the controls, and the field used to keep the height it
 * had measured for an earlier, shorter placeholder, cutting the hint to its
 * first line.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Composer } from '../src/features/chat/Composer';
import { strings } from '../src/strings';
import { claudeAgent } from '../mock/fixtures';
import type { Session, SessionState } from '../src/protocol/types';

// jsdom has no layout. A textarea's words wrap here at its inline width, 7.5 px
// a character, on 21 px lines inside 14 px of padding — the composer's field.
const CHAR_PX = 7.5;
const LINE_PX = 21;
const PADDING_PX = 14;
const original = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'scrollHeight');

beforeAll(() => {
  Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
    configurable: true,
    get(this: HTMLTextAreaElement) {
      const width = Number.parseFloat(this.style.width);
      const lines = width > 0 ? Math.max(1, Math.ceil((this.value.length * CHAR_PX) / width)) : 1;
      return lines * LINE_PX + PADDING_PX;
    },
  });
});

afterAll(() => {
  if (original) Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', original);
});

const ONE_LINE = '35px';
const TWO_LINES = '56px';

/** A ResizeObserver the test drives, and animation frames it runs by hand. */
const observers: ResizeObserverCallback[] = [];
let frames: FrameRequestCallback[] = [];

class FakeResizeObserver {
  constructor(callback: ResizeObserverCallback) {
    observers.push(callback);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** The layout changed the field's width: the observers run, then the frame. */
function resize() {
  act(() => {
    for (const callback of observers) callback([], {} as ResizeObserver);
  });
  act(() => {
    const due = frames;
    frames = [];
    for (const frame of due) frame(0);
  });
}

beforeEach(() => {
  observers.length = 0;
  frames = [];
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', () => undefined);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const session: Session = {
  session_id: 'ses-flaky',
  device_id: 'dev-1',
  agent: 'claude',
  title: 'Fix flaky auth test',
  cwd: '/Users/me/dev/gateway',
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
  queued: 0,
};

function setup(initial: { state: SessionState; deviceOnline: boolean }) {
  const element = ({ state, deviceOnline }: typeof initial) => (
    <Composer
      session={{ ...session, state }}
      agent={claudeAgent}
      deviceOnline={deviceOnline}
      queue={[]}
      sttEnabled={false}
      question={null}
      onSend={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />
  );
  const view = render(element(initial));
  const field = screen.getByLabelText(strings.composer.placeholder) as HTMLTextAreaElement;
  return {
    field,
    rerender: (next: typeof initial) => act(() => view.rerender(element(next))),
  };
}

describe('the empty field and its placeholder', () => {
  it('grows to the whole placeholder when a longer one takes over', () => {
    // A first arrival: the device is not known to be online yet, and once it
    // is the running session's hint is the one that wraps at a phone's width.
    const h = setup({ state: 'running', deviceOnline: false });
    h.field.style.width = '169px';
    expect(h.field.placeholder).toBe(strings.composer.placeholderOffline);

    h.rerender({ state: 'running', deviceOnline: true });

    expect(h.field.placeholder).toBe(strings.composer.placeholderQueued);
    expect(h.field.style.height).toBe(TWO_LINES);
  });

  it('fits again when its width changes, on the next frame', () => {
    const h = setup({ state: 'running', deviceOnline: true });
    expect(h.field.style.height).toBe(ONE_LINE);

    h.field.style.width = '169px';
    resize();
    expect(h.field.style.height).toBe(TWO_LINES);

    h.field.style.width = '400px';
    resize();
    expect(h.field.style.height).toBe(ONE_LINE);
  });

  it('measures the words once there are some, and the placeholder again when they go', async () => {
    const user = userEvent.setup();
    const h = setup({ state: 'running', deviceOnline: true });
    h.field.style.width = '169px';

    await user.click(h.field);
    await user.keyboard('hi');
    expect(h.field.style.height).toBe(ONE_LINE);

    await user.clear(h.field);
    expect(h.field.style.height).toBe(TWO_LINES);

    // The measurement leaves nothing behind and takes nothing from the field.
    expect(document.querySelectorAll('textarea')).toHaveLength(1);
    expect(h.field).toHaveValue('');
    expect(h.field).toHaveFocus();
  });

  it('keeps one line for a placeholder that fits', () => {
    const h = setup({ state: 'idle', deviceOnline: false });
    h.field.style.width = '169px';

    h.rerender({ state: 'idle', deviceOnline: true });

    expect(h.field.placeholder).toBe(strings.composer.placeholder);
    expect(h.field.style.height).toBe(ONE_LINE);
  });
});
