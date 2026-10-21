"use client";

import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

type DialogProps = {
  open: boolean;
  title: string;
  description?: ReactNode;
  onClose: () => void;
  /** `false` while a request is running or when the user must acknowledge (e.g. a new secret). */
  dismissible?: boolean;
  /** `lg` for forms with several columns. */
  size?: "md" | "lg";
  children: ReactNode;
};

/**
 * Accessible modal: labelled, focus moves in on open and back to the trigger on close,
 * Tab stays inside, Escape and the backdrop close it (when dismissible), and the page
 * behind does not scroll.
 */
export function Dialog({ open, title, description, onClose, dismissible = true, size = "md", children }: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const dismissibleRef = useRef(dismissible);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    dismissibleRef.current = dismissible;
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const focusables = () => Array.from(panel?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    // Explicit `data-autofocus` first, else the first control in the body (not the header's
    // close button), else the panel itself.
    const body = panel?.querySelector<HTMLElement>("[data-dialog-body]");
    const initial =
      focusables().find((el) => el.dataset.autofocus !== undefined) ??
      body?.querySelector<HTMLElement>(FOCUSABLE) ??
      panel;
    initial?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && dismissibleRef.current) {
        event.preventDefault();
        onCloseRef.current();
      } else if (event.key === "Tab") {
        const items = focusables();
        if (items.length === 0) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = overflow;
      previouslyFocused?.focus?.();
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center">
      <div
        aria-hidden
        className="absolute inset-0 bg-fg/40"
        onClick={() => dismissibleRef.current && onCloseRef.current()}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={`relative max-h-[calc(100vh-2rem)] w-full overflow-y-auto rounded-xl border border-border bg-surface p-6 shadow-xl ${
          size === "lg" ? "max-w-2xl" : "max-w-md"
        }`}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 id={titleId} className="text-lg font-semibold text-fg">
              {title}
            </h2>
            {description && (
              <div id={descriptionId} className="mt-1 text-sm text-muted">
                {description}
              </div>
            )}
          </div>
          {dismissible && (
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="-m-1 cursor-pointer rounded-md p-1 text-muted transition-colors duration-150 hover:bg-surface-muted hover:text-fg"
            >
              <X aria-hidden className="size-5" />
            </button>
          )}
        </div>
        <div data-dialog-body>{children}</div>
      </div>
    </div>
  );
}
