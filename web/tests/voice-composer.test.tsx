/**
 * How dictation meets the message field. The controller itself is tested in
 * voice.test.ts; what matters here is the wiring: the mic button starts it,
 * Done leaves the transcript to be edited, a keystroke takes the field back,
 * and nothing reaches the agent until Send is clicked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { claudeAgent } from '../mock/fixtures';
import { strings } from '../src/strings';
import type * as ReactModule from 'react';
import type { Session } from '../src/protocol/types';

const voice = vi.hoisted(() => ({
  publish: null as null | ((text: string, isFinal: boolean) => void),
}));

vi.mock('../src/features/voice/useVoice', async () => {
  const { useState } = await vi.importActual<typeof ReactModule>('react');
  return {
    useVoice: (options: { onTranscript: (text: string, isFinal: boolean) => void }) => {
      const [state, setState] = useState('idle');
      // Like the controller: Done starts the wait for the backend's last word,
      // and that word, when it comes, is what ends the run.
      voice.publish = (text, isFinal) => {
        options.onTranscript(text, isFinal);
        if (isFinal) setState('idle');
      };
      return {
        state,
        level: 0.4,
        elapsedMs: 12_000,
        error: null,
        start: () => setState('listening'),
        done: () => setState('finishing'),
        cancel: () => setState('idle'),
        dismissError: () => setState('idle'),
      };
    },
  };
});

const { Composer } = await import('../src/features/chat/Composer');

const session: Session = {
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

function setup() {
  const onSend = vi.fn().mockResolvedValue(undefined);
  render(
    <Composer
      session={session}
      agent={claudeAgent}
      deviceOnline
      queue={[]}
      sttEnabled
      question={null}
      onSend={onSend}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onSetOption={vi.fn()}
      onRemoveQueued={vi.fn()}
      onTakeover={vi.fn()}
    />,
  );
  return { onSend, field: () => screen.getByLabelText(strings.composer.placeholder) };
}

const say = (text: string, isFinal = false) => act(() => voice.publish?.(text, isFinal));

/**
 * jsdom has no layout, so the field is handed the measurements a transcript
 * longer than eight lines would produce: `scrollHeight` is the height the text
 * wants, and `scrollTop` a real property rather than jsdom's no-op setter, so
 * where the field was scrolled to can be read back.
 */
function measured(el: HTMLTextAreaElement, scrollHeight: number): HTMLTextAreaElement {
  let top = 0;
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = value;
    },
  });
  return el;
}

const LONG_TRANSCRIPT = Array.from(
  { length: 14 },
  (_, line) => `line ${line + 1} of what was just said`,
).join(' ');

const controlRowButtons = (): string[] =>
  [...document.querySelectorAll('.voice-controls button')].map((b) => b.textContent ?? '');

beforeEach(() => {
  voice.publish = null;
});

describe('dictation in the composer', () => {
  it('starts listening on the mic button, with no chord to hold', async () => {
    const user = userEvent.setup();
    setup();

    await user.keyboard('{Alt>} {/Alt}');
    expect(document.querySelector('.voice-controls')).toBeNull();

    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));

    expect(document.querySelector('.voice-controls')).not.toBeNull();
    expect(screen.getByText(strings.voice.transcribing)).toBeInTheDocument();
  });

  it('offers Done alone, in Send\'s place, and an elapsed timer while listening', async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));

    expect(controlRowButtons()).toEqual([strings.voice.done]);
    // Done takes Send's slot, at Send's size: the one primary in the row.
    expect(screen.getByRole('button', { name: strings.voice.done }).className).toContain('send-btn');
    expect(screen.getByLabelText(strings.voice.listeningFor('0:12'))).toHaveTextContent('0:12');
    // The send button and the mic itself are not part of the listening row.
    expect(screen.queryByRole('button', { name: strings.composer.send })).toBeNull();
    expect(screen.queryByRole('button', { name: strings.composer.micStart })).toBeNull();
  });

  it('appends the transcript to the draft the mic was pressed on', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await user.click(field());
    await user.keyboard('after lunch');
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));

    say('run the');
    say('run the auth suite');

    expect(field()).toHaveValue('after lunch run the auth suite');
  });

  it('leaves the transcript in the field on Done and sends nothing', async () => {
    const user = userEvent.setup();
    const { field, onSend } = setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    say('run the auth suite');

    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    say('run the auth suite on CI', true);

    expect(field()).toHaveValue('run the auth suite on CI');
    expect(onSend).not.toHaveBeenCalled();

    // It is an ordinary draft now: the ordinary Send button is what sends it.
    await user.click(screen.getByRole('button', { name: strings.composer.send }));
    expect(onSend).toHaveBeenCalledWith('run the auth suite on CI', [], 'auto');
  });

  it('answers the click on Done with a spinner, and offers Send once the words are in', async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    say('run the auth suite');

    await user.click(screen.getByRole('button', { name: strings.voice.done }));

    // Nothing in the row can be clicked while the transcript is on its way.
    expect(controlRowButtons()).toEqual([]);
    expect(screen.queryByRole('button', { name: strings.composer.send })).toBeNull();
    const working = screen.getByRole('status', { name: strings.voice.finishing });
    expect(working.className).toContain('working-pill');
    expect(working.querySelector('.spinner')).not.toBeNull();
    // The status line says what is being waited for, in the same words.
    expect(screen.getByText(strings.voice.finishing)).toBeInTheDocument();

    say('run the auth suite on CI', true);

    // With no polish the final transcript is what will be sent, so Send is back.
    expect(screen.queryByRole('status', { name: strings.voice.finishing })).toBeNull();
    expect(document.querySelector('.working-pill')).toBeNull();
    expect(screen.getByRole('button', { name: strings.composer.send })).toBeInTheDocument();
  });

  it('lets a keystroke take the field back from dictation', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    say('run the auth suite');

    // Focused from the code, the way taking a command row focuses it: that is
    // not a person reaching for the field, so the keystroke is what ends this.
    act(() => field().focus());
    await user.keyboard(' twice');
    say('run the auth suite again');

    expect(field()).toHaveValue('run the auth suite twice');
    expect(document.querySelector('.voice-controls')).toBeNull();
  });

  it('follows the words while dictating, so the newest line stays in view', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    const el = measured(field() as HTMLTextAreaElement, 600);

    say(LONG_TRANSCRIPT);

    // The field stops growing at its eight lines and scrolls inside, and what
    // it shows is the end of the words: `docs/DESIGN.md` § "The composer".
    expect(el).toHaveValue(LONG_TRANSCRIPT);
    expect(el.style.height).toBe('220px');
    expect(el.scrollTop).toBe(600);
  });

  it('lets a click into the field take it back, so the words can be read back', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    const el = measured(field() as HTMLTextAreaElement, 600);
    say(LONG_TRANSCRIPT);
    expect(el.scrollTop).toBe(600);

    // Reaching for the field is a takeover, exactly as a keystroke is.
    await user.click(el);

    expect(document.querySelector('.voice-controls')).toBeNull();
    expect(el).toHaveValue(LONG_TRANSCRIPT);

    // Scrolled back to read, the field stays where it was put: a late
    // transcript writes nothing and moves nothing.
    el.scrollTop = 40;
    say(`${LONG_TRANSCRIPT} and one more`);

    expect(el).toHaveValue(LONG_TRANSCRIPT);
    expect(el.scrollTop).toBe(40);
  });

  it('keeps following through the finishing spinner, and lets go once it is over', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    await user.click(screen.getByRole('button', { name: strings.composer.micStart }));
    const el = measured(field() as HTMLTextAreaElement, 600);

    await user.click(screen.getByRole('button', { name: strings.voice.done }));
    say(`${LONG_TRANSCRIPT} and one more`, true);

    expect(el.scrollTop).toBe(600);

    // The words are an ordinary draft now, and where it is read from is the
    // person's business: a typed edit moves nothing.
    el.scrollTop = 120;
    await user.click(el);
    await user.keyboard(' twice');

    expect(el.scrollTop).toBe(120);
  });

  it('leaves a typed draft where it is, because a caret keeps itself in view', async () => {
    const user = userEvent.setup();
    const { field } = setup();
    const el = measured(field() as HTMLTextAreaElement, 600);
    const typed = 'typed draft';

    await user.click(el);
    await user.keyboard(typed);

    expect(el).toHaveValue(typed);
    expect(el.style.height).toBe('220px');
    expect(el.scrollTop).toBe(0);
  });

  it('hides the microphone entirely when the gateway has no speech backend', () => {
    render(
      <Composer
        session={session}
        agent={claudeAgent}
        deviceOnline
        queue={[]}
        sttEnabled={false}
        question={null}
        onSend={vi.fn()}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onSetOption={vi.fn()}
        onRemoveQueued={vi.fn()}
        onTakeover={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: strings.composer.micStart })).toBeNull();
  });
});

describe('product name', () => {
  it('reads "SideCode" wherever the user sees it in English', () => {
    expect(strings.productName).toBe('SideCode');
    expect(strings.login.title).toBe('SideCode');
  });
});
