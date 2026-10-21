// frontend/src/components/BillUploader.jsx
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

const ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif";

/**
 * Drop, pick, or paste a bill photo. Uploading takes the user straight to the
 * review screen for that bill, since nothing is trustworthy until a human has
 * looked at it next to the paper.
 */
export function BillUploader() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef(null);
  const navigate = useNavigate();

  const send = async (file) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const bill = await api.scanBill(file);
      navigate(`/bills/${bill.id}`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const enterByHand = async () => {
    setBusy(true);
    setError(null);
    try {
      const bill = await api.createManualBill();
      navigate(`/bills/${bill.id}`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className={`uploader ${dragOver ? "uploader-over" : ""} ${
        busy ? "uploader-busy" : ""
      }`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        send(e.dataTransfer.files?.[0]);
      }}
      onPaste={(e) => {
        const file = [...(e.clipboardData?.files || [])][0];
        if (file) send(file);
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        capture="environment"
        hidden
        onChange={(e) => {
          send(e.target.files?.[0]);
          e.target.value = ""; // allow re-picking the same file
        }}
      />

      {busy ? (
        <>
          <div className="spinner" />
          <p>Reading the bill…</p>
          <p className="hint">This takes a few seconds.</p>
        </>
      ) : (
        <>
          <p className="uploader-title">Drop a bill photo here</p>
          <p className="hint">or paste from the clipboard</p>
          <div className="uploader-actions">
            <button type="button" onClick={() => inputRef.current?.click()}>
              Choose a photo
            </button>
            <button type="button" className="secondary" onClick={enterByHand}>
              Enter by hand
            </button>
          </div>
          <p className="hint uploader-note">
            Entering by hand skips the AI read — the arithmetic is still
            checked, and no API key is needed.
          </p>
        </>
      )}

      {error && (
        <p className="uploader-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
