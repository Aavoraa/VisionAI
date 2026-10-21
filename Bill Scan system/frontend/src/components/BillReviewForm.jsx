// frontend/src/components/BillReviewForm.jsx
import { useEffect, useRef, useState } from "react";
import { CATEGORIES, PAYMENT_METHODS, rupees } from "../api";

const EMPTY_LINE = {
  description: "",
  quantity: 1,
  unit: null,
  rate: 0,
  amount: 0,
  tax_percent: null,
};

/**
 * A number field that keeps what the user typed.
 *
 * Coercing with `+e.target.value` on every keystroke makes "" collapse to 0
 * and makes "12." unrepresentable, so the caret jumps and a half-typed decimal
 * is impossible. This keeps the text locally and only pushes a number up.
 */
function NumberInput({ value, onValue, allowNull = false, ...rest }) {
  const [text, setText] = useState(fmt(value));
  const focused = useRef(false);

  // Follow external changes (autocorrect, a reload) unless being edited.
  useEffect(() => {
    if (!focused.current) setText(fmt(value));
  }, [value]);

  function fmt(v) {
    return v === null || v === undefined || Number.isNaN(v) ? "" : String(v);
  }

  return (
    <input
      {...rest}
      type="text"
      inputMode="decimal"
      value={text}
      onFocus={() => {
        focused.current = true;
      }}
      onBlur={() => {
        focused.current = false;
        setText(fmt(value));
      }}
      onChange={(e) => {
        const raw = e.target.value;
        if (raw !== "" && !/^-?\d*\.?\d*$/.test(raw)) return;
        setText(raw);
        if (raw === "" || raw === "-") {
          onValue(allowNull ? null : 0);
        } else {
          const n = Number(raw);
          if (!Number.isNaN(n)) onValue(n);
        }
      }}
    />
  );
}

export function BillReviewForm({ bill, errorFields, onChange }) {
  const update = (path, value) => {
    // path like "vendor_name" or "line_items[0].amount"
    const next = structuredClone(bill);
    const keys = path.replace(/\[(\d+)\]/g, ".$1").split(".");
    let obj = next;
    for (let i = 0; i < keys.length - 1; i++) {
      obj = obj[keys[i]];
    }
    obj[keys[keys.length - 1]] = value;
    onChange(next);
  };

  const lines = bill.line_items || [];

  const addLine = () =>
    onChange({ ...bill, line_items: [...lines, { ...EMPTY_LINE }] });

  const removeLine = (i) =>
    onChange({ ...bill, line_items: lines.filter((_, j) => j !== i) });

  // The one arithmetic shortcut worth having on the client: it fills the
  // totals from the lines that are already on screen. The backend still has
  // the last word on whether the result reconciles.
  const recalcTotals = () => {
    const subtotal = round2(
      lines.reduce((sum, l) => sum + (Number(l.amount) || 0), 0)
    );
    const tax = round2(
      (Number(bill.cgst) || 0) + (Number(bill.sgst) || 0) + (Number(bill.igst) || 0)
    ) || Number(bill.tax_amount) || 0;
    onChange({
      ...bill,
      subtotal,
      tax_amount: tax,
      grand_total: round2(subtotal + tax + (Number(bill.round_off) || 0)),
    });
  };

  const fieldClass = (path) =>
    errorFields.has(path) ? "field field-error" : "field";

  const cellClass = (path) => (errorFields.has(path) ? "cell-error" : "");

  const lineSum = round2(
    lines.reduce((sum, l) => sum + (Number(l.amount) || 0), 0)
  );

  return (
    <div className="review-form">
      <div className="form-row">
        <div className={fieldClass("vendor_name")}>
          <label htmlFor="vendor_name">Vendor</label>
          <input
            id="vendor_name"
            value={bill.vendor_name || ""}
            placeholder="Shop or supplier name"
            onChange={(e) => update("vendor_name", e.target.value)}
          />
        </div>

        <div className={fieldClass("bill_date")}>
          <label htmlFor="bill_date">Date</label>
          <input
            id="bill_date"
            type="date"
            value={bill.bill_date || ""}
            onChange={(e) => update("bill_date", e.target.value || null)}
          />
          {!bill.bill_date && bill.bill_date_raw && (
            <span className="hint">printed as “{bill.bill_date_raw}”</span>
          )}
        </div>
      </div>

      <div className="form-row">
        <div className="field">
          <label htmlFor="category">Category</label>
          <select
            id="category"
            value={bill.category || "miscellaneous"}
            onChange={(e) => update("category", e.target.value)}
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="payment_method">Payment method</label>
          <select
            id="payment_method"
            value={bill.payment_method || "unknown"}
            onChange={(e) => update("payment_method", e.target.value)}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m} value={m}>
                {m === "upi" ? "UPI" : m[0].toUpperCase() + m.slice(1)}
              </option>
            ))}
          </select>
        </div>

        <div className={fieldClass("bill_number")}>
          <label htmlFor="bill_number">Bill no.</label>
          <input
            id="bill_number"
            value={bill.bill_number || ""}
            onChange={(e) => update("bill_number", e.target.value)}
          />
        </div>
      </div>

      <div className="section-head">
        <h3>Line items</h3>
        <button type="button" className="link-btn" onClick={addLine}>
          + Add line
        </button>
      </div>

      <table className="line-items">
        <thead>
          <tr>
            <th className="col-desc">Item</th>
            <th className="col-num">Qty</th>
            <th className="col-num">Rate</th>
            <th className="col-num">Amount</th>
            <th className="col-num">Tax %</th>
            <th className="col-act" aria-label="Remove" />
          </tr>
        </thead>
        <tbody>
          {lines.map((line, i) => (
            <tr key={i} className={cellClass(`line_items[${i}].amount`)}>
              <td>
                <input
                  aria-label={`Item ${i + 1} description`}
                  value={line.description || ""}
                  onChange={(e) =>
                    update(`line_items[${i}].description`, e.target.value)
                  }
                />
              </td>
              <td>
                <NumberInput
                  aria-label={`Item ${i + 1} quantity`}
                  value={line.quantity}
                  onValue={(v) => update(`line_items[${i}].quantity`, v)}
                />
              </td>
              <td>
                <NumberInput
                  aria-label={`Item ${i + 1} rate`}
                  value={line.rate}
                  onValue={(v) => update(`line_items[${i}].rate`, v)}
                />
              </td>
              <td className={cellClass(`line_items[${i}].amount`)}>
                <NumberInput
                  aria-label={`Item ${i + 1} amount`}
                  value={line.amount}
                  onValue={(v) => update(`line_items[${i}].amount`, v)}
                />
              </td>
              <td>
                <NumberInput
                  aria-label={`Item ${i + 1} tax percent`}
                  value={line.tax_percent}
                  allowNull
                  onValue={(v) => update(`line_items[${i}].tax_percent`, v)}
                />
              </td>
              <td className="col-act">
                <button
                  type="button"
                  className="icon-btn"
                  title="Remove this line"
                  aria-label={`Remove item ${i + 1}`}
                  onClick={() => removeLine(i)}
                >
                  ×
                </button>
              </td>
            </tr>
          ))}
          {lines.length === 0 && (
            <tr>
              <td colSpan={6} className="empty-lines">
                No line items were read from this bill. Add them by hand.
              </td>
            </tr>
          )}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={3}>Lines add up to</td>
            <td className="col-num">{rupees(lineSum)}</td>
            <td colSpan={2} />
          </tr>
        </tfoot>
      </table>

      <div className="section-head">
        <h3>Totals</h3>
        <button type="button" className="link-btn" onClick={recalcTotals}>
          Recalculate from lines
        </button>
      </div>

      <div className="totals">
        <div className={fieldClass("subtotal")}>
          <label htmlFor="subtotal">Subtotal</label>
          <NumberInput
            id="subtotal"
            value={bill.subtotal}
            onValue={(v) => update("subtotal", v)}
          />
        </div>
        <div className="field">
          <label htmlFor="cgst">CGST</label>
          <NumberInput
            id="cgst"
            value={bill.cgst}
            allowNull
            onValue={(v) => update("cgst", v)}
          />
        </div>
        <div className="field">
          <label htmlFor="sgst">SGST</label>
          <NumberInput
            id="sgst"
            value={bill.sgst}
            allowNull
            onValue={(v) => update("sgst", v)}
          />
        </div>
        <div className={fieldClass("tax_amount")}>
          <label htmlFor="tax_amount">Tax</label>
          <NumberInput
            id="tax_amount"
            value={bill.tax_amount}
            onValue={(v) => update("tax_amount", v)}
          />
        </div>
        <div className="field">
          <label htmlFor="round_off">Round off</label>
          <NumberInput
            id="round_off"
            value={bill.round_off}
            onValue={(v) => update("round_off", v)}
          />
        </div>
        <div className={`${fieldClass("grand_total")} field-total`}>
          <label htmlFor="grand_total">Grand Total</label>
          <NumberInput
            id="grand_total"
            value={bill.grand_total}
            onValue={(v) => update("grand_total", v)}
          />
        </div>
      </div>

      {/* Confidence describes how well the model read the image, so it is
          meaningless on a bill somebody typed in. */}
      {bill._source !== "manual" && (
        <div className={`confidence confidence-${bill._effective_confidence}`}>
          AI confidence:{" "}
          <strong>{bill._effective_confidence || "unknown"}</strong>
          {bill.confidence &&
            bill._effective_confidence !== bill.confidence && (
              <span className="hint">
                {" "}
                (the model said {bill.confidence}; lowered because the
                arithmetic did not reconcile)
              </span>
            )}
        </div>
      )}
    </div>
  );
}

function round2(n) {
  return Math.round((Number(n) || 0) * 100) / 100;
}
