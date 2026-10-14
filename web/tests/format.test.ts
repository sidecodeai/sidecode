import { describe, expect, it } from 'vitest';
import {
  baseName,
  bytes,
  clock,
  compactNumber,
  duration,
  foldLines,
  latency,
  relativeAgo,
  relativeTime,
  tildePath,
} from '../src/lib/format';
import { useSettings } from '../src/stores/settings';

const NOW = 1_700_000_000_000;
const ago = (ms: number): number => NOW - ms;

describe('relative time', () => {
  it('uses the compact scale shown in session rows', () => {
    expect(relativeTime(ago(1_000), NOW)).toBe('now');
    expect(relativeTime(ago(4 * 60_000), NOW)).toBe('4m');
    expect(relativeTime(ago(3 * 3_600_000), NOW)).toBe('3h');
    expect(relativeTime(ago(2 * 86_400_000), NOW)).toBe('2d');
  });

  it('uses the longer scale for recent directories', () => {
    expect(relativeAgo(ago(30_000), NOW)).toBe('just now');
    expect(relativeAgo(ago(2 * 3_600_000), NOW)).toBe('2h ago');
    expect(relativeAgo(ago(30 * 3_600_000), NOW)).toBe('yesterday');
  });
});

describe('duration and clock', () => {
  it('formats tool durations', () => {
    expect(duration(820)).toBe('820ms');
    expect(duration(6_400)).toBe('6.4s');
    expect(duration(38_020)).toBe('38s');
    expect(duration(72_000)).toBe('1m 12s');
    expect(duration(3 * 3_600_000 + 5 * 60_000)).toBe('3h 5m');
  });

  it('formats countdown clocks', () => {
    expect(clock(12_000)).toBe('0:12');
    expect(clock(587_000)).toBe('9:47');
    expect(clock(-5)).toBe('0:00');
  });
});

describe('numbers and paths', () => {
  it('compacts token counts the way the usage chip shows them', () => {
    expect(compactNumber(940)).toBe('940');
    expect(compactNumber(48_200)).toBe('48.2k');
    expect(compactNumber(203_000)).toBe('203k');
    expect(compactNumber(1_500_000)).toBe('1.5M');
  });

  it('formats byte sizes for attachments', () => {
    expect(bytes(512)).toBe('512 B');
    expect(bytes(6 * 1024 * 1024)).toBe('6.0 MiB');
  });

  it('collapses home directories to a tilde', () => {
    expect(tildePath('/Users/me/dev/gateway')).toBe('~/dev/gateway');
    expect(tildePath('/home/ci/work/api')).toBe('~/work/api');
    expect(tildePath('/opt/tools')).toBe('/opt/tools');
    expect(tildePath('/Users/me/dev', '/Users/me')).toBe('~/dev');
  });

  it('takes the last path segment for sidebar subtitles', () => {
    expect(baseName('/Users/me/dev/gateway')).toBe('gateway');
    expect(baseName('/Users/me/dev/gateway/')).toBe('gateway');
    expect(baseName('/')).toBe('/');
  });

  it('renders latency or an em dash', () => {
    expect(latency(18)).toBe('18 ms');
    expect(latency(null)).toBe('—');
  });

  it('renders latency in 中文 too', () => {
    useSettings.setState({ language: 'zh-Hans' });
    try {
      expect(latency(42.4)).toBe('42 毫秒');
      expect(latency(null)).toBe('—');
    } finally {
      useSettings.setState({ language: 'en' });
    }
  });
});

describe('foldLines', () => {
  it('leaves short output alone', () => {
    const result = foldLines('a\nb\nc', 20);
    expect(result.folded).toBe(false);
    expect(result.head).toBe('a\nb\nc');
    expect(result.total).toBe(3);
  });

  it('folds beyond the line budget and reports the real total', () => {
    const text = Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n');
    const result = foldLines(text, 20);
    expect(result.folded).toBe(true);
    expect(result.total).toBe(50);
    expect(result.head.split('\n')).toHaveLength(20);
  });
});
