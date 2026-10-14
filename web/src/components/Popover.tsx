import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown } from 'lucide-react';
import { cx } from '../lib/cx';
import { placementFor, type Align, type Side } from './popoverPlacement';
import './popover.css';

interface PopoverProps {
  label: ReactNode;
  children: (close: () => void) => ReactNode;
  align?: Align;
  side?: Side;
  className?: string;
  triggerClassName?: string;
  ariaLabel?: string;
  disabled?: boolean;
  chevron?: boolean;
}

export function Popover({
  label,
  children,
  align = 'start',
  side = 'bottom',
  className,
  triggerClassName,
  ariaLabel,
  disabled,
  chevron = true,
}: PopoverProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const panelId = useId();
  // The panel is a portal on the body, so it is placed against the viewport,
  // before the browser paints and again on every scroll under it.
  useLayoutEffect(() => {
    if (!open) return;
    const place = (): void => {
      const anchor = root.current;
      const el = panel.current;
      if (!anchor || !el) return;
      const at = placementFor(
        anchor.getBoundingClientRect(),
        el.getBoundingClientRect(),
        { width: window.innerWidth, height: window.innerHeight },
        align,
        side,
      );
      el.style.left = `${at.left}px`;
      el.style.top = at.top === null ? '' : `${at.top}px`;
      el.style.bottom = at.bottom === null ? '' : `${at.bottom}px`;
    };

    place();
    // Capture, so the panel follows a scroll in any pane it is anchored inside.
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [open, align, side]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      // The panel is a portal, so it is outside the trigger's subtree.
      if (root.current?.contains(target) || panel.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className={cx('popover-root', className)} ref={root}>
      <button
        type="button"
        className={cx('pill', triggerClassName)}
        aria-expanded={open}
        aria-haspopup="true"
        aria-controls={open ? panelId : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
      >
        {label}
        {chevron ? <ChevronDown size={13} aria-hidden className="popover-chevron" /> : null}
      </button>
      {open
        ? createPortal(
            <div className="popover-panel" id={panelId} ref={panel}>
              {children(() => setOpen(false))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

export interface MenuOption {
  id: string;
  label: ReactNode;
  description?: string;
  disabled?: boolean;
}

interface MenuProps {
  label: ReactNode;
  options: MenuOption[];
  value: string | null;
  onSelect: (id: string) => void;
  ariaLabel: string;
  align?: Align;
  side?: Side;
  disabled?: boolean;
}

export function Menu({ label, options, value, onSelect, ariaLabel, align, side, disabled }: MenuProps) {
  return (
    <Popover label={label} ariaLabel={ariaLabel} align={align} side={side} disabled={disabled}>
      {(close) => (
        <ul className="menu" role="listbox" aria-label={ariaLabel}>
          {options.map((option) => (
            <li key={option.id}>
              <button
                type="button"
                role="option"
                aria-selected={value === option.id}
                disabled={option.disabled}
                className={cx('menu-item', value === option.id && 'selected')}
                onClick={() => {
                  onSelect(option.id);
                  close();
                }}
              >
                <span className="menu-label">{option.label}</span>
                {option.description ? <span className="menu-desc">{option.description}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Popover>
  );
}
