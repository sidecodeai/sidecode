import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react';
import { cx } from '../lib/cx';
import type { TooltipSide } from './popoverPlacement';
import { useTooltip } from './useTooltip';

type Props = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label' | 'type' | 'children'> & {
  /** The button's accessible name, and what its tooltip says. */
  label: string;
  /** Which side of the button the tooltip opens on. */
  side?: TooltipSide;
  /** A keyboard shortcut the tooltip prints after the label, written as the platform writes it. */
  shortcut?: string;
  /** The icon. */
  children: ReactNode;
  ref?: Ref<HTMLButtonElement>;
};

/**
 * An icon button that names itself twice: to assistive technology through its
 * `aria-label`, and on screen through a tooltip that says the same (and the
 * shortcut, where it has one). Its pointer and focus handlers are the tooltip's.
 */
export function TooltipButton({
  label,
  side = 'bottom',
  shortcut,
  className,
  children,
  ref,
  ...rest
}: Props) {
  const { triggerProps, tooltip } = useTooltip(label, side, shortcut);
  return (
    <>
      <button
        ref={ref}
        type="button"
        {...rest}
        {...triggerProps}
        className={cx('icon-btn', className)}
        aria-label={label}
      >
        {children}
      </button>
      {tooltip}
    </>
  );
}
