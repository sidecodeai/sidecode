/**
 * The listening row's scrolling waveform (`docs/DESIGN.md` § "The composer",
 * A57): a bar every 80 ms enters at the trailing edge and glides left, its
 * height the loudest level of its window; silence is a gliding row of dots.
 * Once Done is tapped the glide slows to a stop and a highlight sweeps the
 * rested bars until the words are in.
 *
 * Decorative: hidden from assistive technology, which hears the row's own
 * labels. The levels reach the canvas without a render, and the drawing loop
 * stops when the row goes away or the page is hidden.
 */
import { useEffect, useRef } from 'react';
import { useReducedMotion } from '../../lib/useReducedMotion';
import type { LevelFeed } from './levelFeed';
import { WaveformCanvas } from './waveformCanvas';
import { LevelSampler } from './waveformSampler';

/** More bars than the widest composer shows. */
const CAPACITY = 512;

interface Props {
  levels: LevelFeed;
  /** Done was tapped, or the run is over: no new bars, and the row rests. */
  resting: boolean;
  /** Read once, as the row rests: how much of the trailing edge the label then covers. */
  cover: () => number;
}

export function Waveform({ levels, resting, cover }: Props) {
  const reducedMotion = useReducedMotion();
  const canvas = useRef<HTMLCanvasElement>(null);
  const painter = useRef<WaveformCanvas | null>(null);
  const reduced = useRef(reducedMotion);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const sampler = new LevelSampler({ capacity: CAPACITY, now: () => performance.now() });
    const view = new WaveformCanvas(element, sampler);
    view.setReducedMotion(reduced.current);
    const unsubscribe = levels.subscribe((energy, samples) => sampler.push(energy, samples));
    sampler.start();
    view.start();
    painter.current = view;
    return () => {
      unsubscribe();
      sampler.stop();
      view.dispose();
      painter.current = null;
    };
  }, [levels]);

  useEffect(() => {
    reduced.current = reducedMotion;
    painter.current?.setReducedMotion(reducedMotion);
  }, [reducedMotion]);

  useEffect(() => {
    if (resting) painter.current?.rest(performance.now(), cover());
  }, [resting, cover]);

  return <canvas ref={canvas} className="voice-wave" aria-hidden />;
}
