import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react';
import { cx } from '../lib/cx';

type Variant = 'default' | 'primary' | 'danger' | 'ghost';

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  small?: boolean;
  block?: boolean;
  busy?: boolean;
  children?: ReactNode;
  /** The button element, for a caller that has to move focus to it. */
  ref?: Ref<HTMLButtonElement>;
}

export function Button({
  variant = 'default',
  small,
  block,
  busy,
  className,
  children,
  disabled,
  type = 'button',
  ...rest
}: Props) {
  return (
    <button
      type={type}
      className={cx('btn', variant !== 'default' && variant, small && 'small', block && 'block', className)}
      disabled={disabled || busy}
      {...rest}
    >
      {busy ? <span className="spinner" aria-hidden /> : null}
      {children}
    </button>
  );
}
