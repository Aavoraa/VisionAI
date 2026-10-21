// frontend/src/components/ConfirmButton.jsx
import { useEffect, useRef, useState } from "react";

/**
 * A destructive action that asks once, inline.
 *
 * The first click arms the button, the second runs the action, and it disarms
 * itself after a few seconds. Preferred over window.confirm here because
 * deleting a bill is a small, frequent action — a blocking modal for every row
 * is heavier than the risk warrants, and an armed button that reverts on its
 * own is hard to trigger by accident.
 */
export function ConfirmButton({
  onConfirm,
  children = "Delete",
  armedLabel = "Really delete?",
  className = "",
  title,
  disabled = false,
  timeout = 4000,
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const disarm = () => {
    clearTimeout(timer.current);
    setArmed(false);
  };

  const click = async () => {
    if (!armed) {
      setArmed(true);
      timer.current = setTimeout(() => setArmed(false), timeout);
      return;
    }
    disarm();
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      className={`${className} ${armed ? "armed" : ""}`}
      onClick={click}
      onBlur={disarm}
      onKeyDown={(e) => e.key === "Escape" && disarm()}
      disabled={disabled || busy}
      title={title}
    >
      {busy ? "…" : armed ? armedLabel : children}
    </button>
  );
}
