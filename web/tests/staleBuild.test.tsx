/**
 * `docs/DESIGN.md` § "A tab older than the server": a deploy replaces the
 * build's hashed chunks, and a tab opened before it fails the first lazy import
 * it makes. The page stays standing, a light notice asks for a reload, and
 * nothing reloads by itself.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Component, lazy, type ReactNode } from 'react';
import { ChunkBoundary } from '../src/components/ChunkBoundary';
import { StaleBuildNotice } from '../src/components/StaleBuildNotice';
import { MarkdownText } from '../src/features/chat/MarkdownText';
import { reloadPage } from '../src/lib/reload';
import { en, stringTables } from '../src/strings';
import { useSettings } from '../src/stores/settings';
import { isChunkLoadError, useStaleBuild, watchForStaleBuild } from '../src/stores/staleBuild';

vi.mock('../src/lib/reload', () => ({ reloadPage: vi.fn() }));

// The transcript's Markdown renderer, failing the way its chunk does in a tab
// older than the server: with the browser's words for a 404 on an import.
vi.mock('../src/features/chat/Markdown', () => ({
  default: () => {
    throw new TypeError('Failed to fetch dynamically imported module: /assets/Markdown-old456.js');
  },
}));

const GONE = 'Failed to fetch dynamically imported module: /assets/Terminal-old456.js';

/** A lazy part whose chunk is a 404. */
const Missing = lazy(() => Promise.reject(new TypeError(GONE)));

/** Catches what the boundary under test lets through, as the page above it would. */
class Catcher extends Component<{ children: ReactNode }, { message: string | null }> {
  override state = { message: null as string | null };
  static getDerivedStateFromError(error: Error): { message: string } {
    return { message: error.message };
  }
  override render(): ReactNode {
    return this.state.message === null ? this.props.children : <p>caught: {this.state.message}</p>;
  }
}

function Bomb(): never {
  throw new Error('boom');
}

beforeEach(() => {
  useStaleBuild.setState({ stale: false });
  useSettings.setState({ language: 'en' });
  vi.mocked(reloadPage).mockReset();
  // React reports every error a boundary catches; those are expected here.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('recognising a chunk that did not arrive', () => {
  it.each([
    ['Chrome', 'Failed to fetch dynamically imported module: https://x.test/assets/a.js'],
    ['Firefox', 'error loading dynamically imported module: https://x.test/assets/a.js'],
    ['Safari', 'Importing a module script failed.'],
    ['Vite', 'Unable to preload CSS for /assets/a.css'],
    ['Vite, any dependency', 'Unable to preload /assets/a.js'],
  ])('knows %s’s words', (_browser, message) => {
    expect(isChunkLoadError(new TypeError(message))).toBe(true);
  });

  it('leaves every other failure alone', () => {
    expect(isChunkLoadError(new Error('boom'))).toBe(false);
    expect(isChunkLoadError('Failed to fetch dynamically imported module')).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
  });
});

/** The event Vite fires, with the error it carries as `payload`. */
const preloadError = (payload?: unknown): Event =>
  Object.assign(new Event('vite:preloadError', { cancelable: true }), { payload });

describe('vite:preloadError', () => {
  it('marks the tab as older than the server for a chunk that could not be loaded, and stops when asked', () => {
    const stop = watchForStaleBuild();
    expect(useStaleBuild.getState().stale).toBe(false);

    window.dispatchEvent(preloadError(new TypeError(GONE)));
    expect(useStaleBuild.getState().stale).toBe(true);

    useStaleBuild.setState({ stale: false });
    stop();
    window.dispatchEvent(preloadError(new TypeError(GONE)));
    expect(useStaleBuild.getState().stale).toBe(false);
  });

  it('marks it for a stylesheet that could not be preloaded too', () => {
    const stop = watchForStaleBuild();
    window.dispatchEvent(preloadError(new Error('Unable to preload CSS for /assets/index-old456.css')));
    expect(useStaleBuild.getState().stale).toBe(true);
    stop();
  });

  // Vite reports a module that loaded and then threw through the same event.
  it('does not blame an update for a module that failed while it ran', () => {
    const stop = watchForStaleBuild();
    window.dispatchEvent(preloadError(new SyntaxError('Unexpected token ‘<’')));
    window.dispatchEvent(preloadError(new ReferenceError('window.thing is not defined')));
    window.dispatchEvent(preloadError('a string'));
    window.dispatchEvent(preloadError());
    expect(useStaleBuild.getState().stale).toBe(false);
    stop();
  });

  it('does not stop the error: the boundary needs it to keep the page standing', () => {
    const stop = watchForStaleBuild();
    const event = preloadError(new TypeError(GONE));
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    stop();
  });
});

describe('the notice', () => {
  it('is not there while the tab is current', () => {
    render(<StaleBuildNotice />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says so in English, with a Reload button that is the only way it reloads', async () => {
    render(<StaleBuildNotice />);
    act(() => useStaleBuild.getState().mark());

    expect(await screen.findByRole('alert')).toHaveTextContent('SideCode was updated. Reload to continue.');
    expect(reloadPage).not.toHaveBeenCalled();

    await userEvent.setup().click(screen.getByRole('button', { name: 'Reload' }));
    expect(reloadPage).toHaveBeenCalledTimes(1);
  });

  it('says so in 中文 under the product’s own name, and follows a change of language', async () => {
    render(<StaleBuildNotice />);
    act(() => useStaleBuild.getState().mark());
    expect(await screen.findByRole('alert')).toHaveTextContent('SideCode');

    act(() => useSettings.setState({ language: 'zh-Hans' }));
    expect(screen.getByRole('alert')).toHaveTextContent('随码已更新，请重新加载以继续。');
    expect(screen.getByRole('button', { name: '重新加载' })).toBeInTheDocument();
  });

  it('is worded as the two tables have it', () => {
    expect(en.appUpdate).toEqual({ stale: 'SideCode was updated. Reload to continue.', reload: 'Reload' });
    expect(stringTables['zh-Hans'].appUpdate).toEqual({
      stale: '随码已更新，请重新加载以继续。',
      reload: '重新加载',
    });
  });
});

describe('a lazy part whose chunk is gone', () => {
  it('stays local: the rest of the page, and what was typed in it, stands', async () => {
    render(
      <>
        <textarea aria-label="draft" defaultValue="an unsent message" />
        <ChunkBoundary failed={<p>the terminal is missing</p>}>
          <Missing />
        </ChunkBoundary>
        <StaleBuildNotice />
      </>,
    );

    expect(await screen.findByText('the terminal is missing')).toBeInTheDocument();
    expect(screen.getByLabelText('draft')).toHaveValue('an unsent message');
    expect(await screen.findByRole('alert')).toHaveTextContent(en.appUpdate.stale);
    expect(reloadPage).not.toHaveBeenCalled();
  });

  it('draws nothing in its place unless told to', async () => {
    const { container } = render(
      <>
        <ChunkBoundary>
          <Missing />
        </ChunkBoundary>
        <StaleBuildNotice />
      </>,
    );
    await screen.findByRole('alert');
    expect(container.querySelector('p')).toBeNull();
  });

  it('lets any other failure go on up', async () => {
    render(
      <Catcher>
        <ChunkBoundary failed={<p>the terminal is missing</p>}>
          <Bomb />
        </ChunkBoundary>
      </Catcher>,
    );

    expect(await screen.findByText('caught: boom')).toBeInTheDocument();
    expect(screen.queryByText('the terminal is missing')).toBeNull();
    expect(useStaleBuild.getState().stale).toBe(false);
  });
});

describe('the transcript’s Markdown', () => {
  it('stays readable as plain text when its chunk is gone', async () => {
    render(
      <>
        <MarkdownText text="a **bold** answer" />
        <StaleBuildNotice />
      </>,
    );

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('a **bold** answer')).toBeInTheDocument();
  });
});
