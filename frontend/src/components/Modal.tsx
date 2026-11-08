import { useEffect, useRef, type ReactNode } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface Props {
  /** The dialog's name for screen readers. */
  label: string;
  onClose: () => void;
  /** Extra classes of the dialog box (modal-wide, machine-settings-modal). */
  className?: string;
  /** A click on the dark background closes it too (not for dialogs with a running task). */
  closeOnBackdrop?: boolean;
  children: ReactNode;
}

/**
 * A dialog over the page: Escape closes it, Tab stays inside it, and the
 * focus goes back to what opened it when it closes.
 */
export function Modal({ label, onClose, className = "", closeOnBackdrop = false, children }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const node = box.current;
    if (node && !node.contains(document.activeElement)) {
      const first = node.querySelector<HTMLElement>("[autofocus]") ?? node.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? node).focus();
    }
    function key(e: KeyboardEvent) {
      if (!node) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        close.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      if (opener && opener.isConnected) opener.focus();
    };
  }, []);

  return (
    <div className="modal-backdrop" onClick={closeOnBackdrop ? onClose : undefined}>
      <div
        ref={box}
        className={`modal-content ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}
