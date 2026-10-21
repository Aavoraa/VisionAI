// frontend/src/pages/ScanBillPage.jsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ImageViewer } from "../components/ImageViewer";
import { BillReviewForm } from "../components/BillReviewForm";
import { ValidationErrorBanner } from "../components/ValidationErrorBanner";
import { ConfirmButton } from "../components/ConfirmButton";
import { api } from "../api";

// Long enough that typing a four-digit total is one request, short enough that
// the red highlight feels immediate.
const REVALIDATE_MS = 400;

/** Drop the server-only keys before sending a bill back. */
function payload(bill) {
  const out = {};
  for (const [k, v] of Object.entries(bill)) {
    if (!k.startsWith("_") && k !== "id" && k !== "status" && k !== "created_at") {
      out[k] = v;
    }
  }
  return out;
}

export function ScanBillPage() {
  const { billId } = useParams();
  const navigate = useNavigate();

  const [bill, setBill] = useState(null);
  const [imageUrl, setImageUrl] = useState(null);
  const [flags, setFlags] = useState([]);
  const [confidence, setConfidence] = useState(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const dirty = useRef(false);
  const photoRef = useRef(null);

  // --- load the bill and its original image ---------------------------------
  useEffect(() => {
    let cancelled = false;
    let objectUrl = null;
    const controller = new AbortController();

    (async () => {
      try {
        const data = await api.getBill(billId);
        if (cancelled) return;
        setBill(data);
        setFlags(data._validation_flags || []);
        setConfidence(data._effective_confidence);

        // A hand-entered bill may not have a photo attached yet.
        if (!data._has_image) return;

        const blob = await api.getBillImage(billId, { signal: controller.signal });
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setImageUrl(objectUrl);
      } catch (e) {
        if (!cancelled && e.name !== "AbortError") setLoadError(e.message);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      // Without this the blob stays in memory for the life of the tab, which
      // adds up fast when a user works through a stack of bills.
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [billId]);

  // A URL created by attachPhoto outlives the load effect that owns the
  // original one, so it needs its own release on unmount. Revoking twice is a
  // no-op, so the overlap with that effect's cleanup is harmless.
  const imageUrlRef = useRef(null);
  useEffect(() => {
    imageUrlRef.current = imageUrl;
  }, [imageUrl]);
  useEffect(
    () => () => {
      if (imageUrlRef.current) URL.revokeObjectURL(imageUrlRef.current);
    },
    []
  );

  const attachPhoto = async (file) => {
    if (!file) return;
    try {
      await api.attachImage(billId, file);
      const blob = await api.getBillImage(billId);
      setImageUrl((old) => {
        if (old) URL.revokeObjectURL(old);
        return URL.createObjectURL(blob);
      });
      setBill((b) => ({ ...b, _has_image: true }));
    } catch (e) {
      setSaveError(e.message);
    }
  };

  // --- keep the flags in step with what is on screen -------------------------
  // The extraction's flags describe the *original* numbers. Once someone fixes
  // a total, the red fields have to move with it, so the backend re-runs the
  // same reconciliation against the edited bill.
  useEffect(() => {
    if (!bill || !dirty.current) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setChecking(true);
      try {
        const result = await api.validateBill(billId, payload(bill), {
          signal: controller.signal,
        });
        setFlags(result.flags);
        setConfidence(result.effective_confidence);
      } catch (e) {
        if (e.name !== "AbortError") {
          // A failed recheck must not blank the flags the user is reading.
          console.warn("Revalidation failed:", e.message);
        }
      } finally {
        setChecking(false);
      }
    }, REVALIDATE_MS);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [bill, billId]);

  // --- warn before losing unsaved edits --------------------------------------
  useEffect(() => {
    const onBeforeUnload = (e) => {
      if (!dirty.current) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const handleChange = useCallback((next) => {
    dirty.current = true;
    setBill(next);
  }, []);

  const handleSave = async (trainSupplier) => {
    setSaving(true);
    setSaveError(null);
    try {
      await api.confirmBill(billId, payload(bill), trainSupplier);
      dirty.current = false;
      navigate("/", { state: { justSaved: Number(billId) } });
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    try {
      await api.deleteBill(billId);
      // Clear the guard first, or navigating away prompts about unsaved edits
      // to a bill that no longer exists.
      dirty.current = false;
      navigate("/");
    } catch (e) {
      setSaveError(e.message);
    }
  };

  // Fields that failed validation get highlighted.
  const errorFields = useMemo(
    () =>
      new Set(
        flags.filter((f) => f.severity === "error").map((f) => f.field_path)
      ),
    [flags]
  );

  if (loadError) {
    return (
      <div className="scan-page">
        <div className="page-error" role="alert">
          <p>Could not open this bill: {loadError}</p>
          <button type="button" onClick={() => navigate("/")}>
            Back to bills
          </button>
        </div>
      </div>
    );
  }

  if (!bill) return <div className="loading">Loading bill…</div>;

  const hasErrors = errorFields.size > 0;

  return (
    <div className="scan-page">
      <header className="scan-header">
        <button className="back-btn" onClick={() => navigate("/")}>
          ← Back
        </button>
        <h1>
          {bill._source === "manual" ? "Enter Daily Bill" : "Scan Daily Bill"}
          {bill._source === "manual" && (
            <span className="pill">entered by hand</span>
          )}
          {bill.status === "confirmed" && (
            <span className="pill pill-ok">confirmed</span>
          )}
        </h1>
        <div className="actions">
          {saveError && (
            <span className="save-error" role="alert">
              {saveError}
            </span>
          )}
          <ConfirmButton
            className="danger-btn"
            disabled={saving}
            title="Delete this bill and its photo"
            onConfirm={handleDelete}
          />
          <button
            className="save-btn"
            onClick={() => handleSave(false)}
            disabled={saving}
          >
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            className="train-btn"
            onClick={() => handleSave(true)}
            disabled={saving || !bill.vendor_name}
            title={
              bill.vendor_name
                ? `Remember how ${bill.vendor_name} bills look`
                : "Fill in the vendor name first"
            }
          >
            Save &amp; Train Vendor
          </button>
        </div>
      </header>

      <ValidationErrorBanner flags={flags} checking={checking} />

      {hasErrors && (
        <p className="banner-note">
          You can still save: the arithmetic on a printed bill really can be
          wrong. The flags are stored with the bill either way.
        </p>
      )}

      <div className="split-view">
        {/* LEFT: original image */}
        <div className="image-pane">
          {bill._has_image ? (
            <ImageViewer
              src={imageUrl}
              alt={`Bill from ${bill.vendor_name || "unknown vendor"}`}
            />
          ) : (
            <div className="no-photo">
              <p>No photo attached</p>
              <p className="hint">
                Attach one to check the numbers against the paper as you type.
              </p>
              <button type="button" onClick={() => photoRef.current?.click()}>
                Attach a photo
              </button>
              <input
                ref={photoRef}
                type="file"
                accept="image/jpeg,image/png,image/webp,image/heic,image/heif"
                hidden
                onChange={(e) => {
                  attachPhoto(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
            </div>
          )}
        </div>

        {/* RIGHT: editable form */}
        <div className="form-pane">
          <BillReviewForm
            bill={{ ...bill, _effective_confidence: confidence }}
            errorFields={errorFields}
            onChange={handleChange}
          />
        </div>
      </div>
    </div>
  );
}
