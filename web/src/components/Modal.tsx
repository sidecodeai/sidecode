import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { cx } from '../lib/cx';
import { strings } from '../strings';
import { useEscapeLayer } from './escapeLayer';
import './overlay.css';

interface Props {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  labelledBy?: string;
  showClose?: boolean;
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = 580,
  labelledBy,
  showClose,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  useOverlay(open, onClose, panelRef);
  if (!open) return null;
  const headingId = labelledBy ?? 'rc-modal-title';
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? headingId : undefined}
        style={{ maxWidth: width }}
        ref={panelRef}
      >
        {title ? (
          <div className="modal-head">
            <h2 id={headingId}>{title}</h2>
            {showClose ? (
              <button type="button" className="icon-btn" onClick={onClose} aria-label={strings.common.close}>
                <X size={16} />
              </button>
            ) : null}
          </div>
        ) : null}
        <div className="modal-body scroll-thin">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  );
}

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;
}

export function Drawer({ open, onClose, title, subtitle, children, footer }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  useOverlay(open, onClose, panelRef);
  if (!open) return null;
  return (
    <div className="overlay drawer-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="rc-drawer-title" ref={panelRef}>
        <div className="drawer-head">
          <div>
            <h2 id="rc-drawer-title">{title}</h2>
            {subtitle ? <p className="hint">{subtitle}</p> : null}
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label={strings.common.close}>
            <X size={16} />
          </button>
        </div>
        <div className={cx('drawer-body', 'scroll-thin')}>{children}</div>
        {footer ? <div className="drawer-foot">{footer}</div> : null}
      </div>
    </div>
  );
}

function useOverlay(
  open: boolean,
  onClose: () => void,
  panelRef: React.RefObject<HTMLDivElement | null>,
): void {
  // Escape reaches the latest `onClose` without re-arming anything: a parent
  // that re-renders hands a new one every time, as an inline arrow does, and an
  // effect that depended on it would be torn down and set up again — focus
  // given back to the opener and then to the first field, in the middle of what
  // the person is typing, an input method's composition included. The effect
  // below runs on open and on close alone.
  useEscapeLayer(open, onClose);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const focusable = panelRef.current?.querySelector<HTMLElement>(
      'input, textarea, select, button:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    focusable?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus?.();
    };
  }, [open, panelRef]);
}

interface ConfirmProps {
  open: boolean;
  title: string;
  /** What is asked: one sentence, or several, each a paragraph of its own. */
  body: string | readonly string[];
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  /** Why the last confirm was refused, in one line under the question. */
  error?: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  danger,
  busy,
  error = null,
  onConfirm,
  onClose,
}: ConfirmProps) {
  const paragraphs = typeof body === 'string' ? [body] : body;
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      width={440}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {strings.common.cancel}
          </button>
          <button
            type="button"
            className={cx('btn', danger ? 'danger' : 'primary')}
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className="confirm-body">
        {paragraphs.map((sentence) => (
          <p key={sentence} className="hint">
            {sentence}
          </p>
        ))}
        {error === null ? null : (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
