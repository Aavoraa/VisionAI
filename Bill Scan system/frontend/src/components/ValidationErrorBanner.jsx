// frontend/src/components/ValidationErrorBanner.jsx
import { useState } from "react";

export function ValidationErrorBanner({ flags, checking }) {
  const [open, setOpen] = useState(false);

  if (!flags || flags.length === 0) {
    return (
      <div className="banner banner-ok">
        <span className="banner-icon">✓</span>
        <strong>All totals match</strong>
        {checking && <span className="banner-checking">rechecking…</span>}
      </div>
    );
  }

  const errors = flags.filter((f) => f.severity === "error");
  const warnings = flags.filter((f) => f.severity === "warning");

  // Errors first: they are what stops a bill from being trustworthy.
  const ordered = [...errors, ...warnings];

  return (
    <div className={`banner ${errors.length ? "banner-error" : "banner-warn"}`}>
      <div className="banner-head">
        <span className="banner-icon">{errors.length ? "⚠" : "ℹ"}</span>
        <strong>
          {errors.length > 0
            ? `${errors.length} math issue${errors.length > 1 ? "s" : ""} found`
            : `${warnings.length} thing${warnings.length > 1 ? "s" : ""} to check`}
        </strong>
        {errors.length > 0 && warnings.length > 0 && (
          <span className="banner-sub">
            and {warnings.length} warning{warnings.length > 1 ? "s" : ""}
          </span>
        )}
        {checking && <span className="banner-checking">rechecking…</span>}
        <button
          type="button"
          className="banner-toggle"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "hide" : "details"}
        </button>
      </div>

      {open && (
        <ul className="banner-list">
          {ordered.map((f, i) => (
            <li key={`${f.field_path}-${i}`} className={`flag-${f.severity}`}>
              <code>{f.field_path}</code>
              <span>{f.message}</span>
              {f.expected != null && (
                <em className="flag-expected">expected {f.expected}</em>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
