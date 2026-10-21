// frontend/src/pages/BillListPage.jsx
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { BillUploader } from "../components/BillUploader";
import { ExportBar } from "../components/ExportBar";
import { ConfirmButton } from "../components/ConfirmButton";
import { api, CATEGORIES, rupees } from "../api";

export function BillListPage() {
  const [bills, setBills] = useState([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState({
    status: "",
    category: "",
    date_from: "",
    date_to: "",
  });
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const justSaved = useLocation().state?.justSaved;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listBills(filters)
      .then((data) => {
        if (cancelled) return;
        setBills(data.bills);
        setTotal(data.total);
        setError(null);
      })
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters]);

  const set = (key) => (e) =>
    setFilters((f) => ({ ...f, [key]: e.target.value }));

  const remove = async (id) => {
    try {
      await api.deleteBill(id);
      // Drop it locally rather than refetching: the list is already correct
      // and a refetch would flash the loading state for a single row.
      setBills((bs) => bs.filter((b) => b.id !== id));
      setTotal((t) => t - (bills.find((b) => b.id === id)?.grand_total || 0));
      setError(null);
    } catch (e) {
      setError(`Could not delete bill ${id}: ${e.message}`);
    }
  };

  return (
    <div className="list-page">
      <header className="list-header">
        <h1>Daily Bills</h1>
        <div className="list-total">
          {bills.length} bill{bills.length === 1 ? "" : "s"} ·{" "}
          <strong>{rupees(total)}</strong>
        </div>
      </header>

      <BillUploader />

      <div className="filters">
        <select value={filters.status} onChange={set("status")}>
          <option value="">All statuses</option>
          <option value="pending_review">Pending review</option>
          <option value="confirmed">Confirmed</option>
        </select>
        <select value={filters.category} onChange={set("category")}>
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <input
          type="date"
          value={filters.date_from}
          onChange={set("date_from")}
          title="From date"
        />
        <input
          type="date"
          value={filters.date_to}
          onChange={set("date_to")}
          title="To date"
        />
      </div>

      <ExportBar filters={filters} count={bills.length} />

      {error && (
        <p className="page-error" role="alert">
          {error}
        </p>
      )}

      {loading ? (
        <p className="loading">Loading…</p>
      ) : bills.length === 0 ? (
        <p className="empty">No bills yet. Upload the first one above.</p>
      ) : (
        <table className="bill-list">
          <thead>
            <tr>
              <th>Date</th>
              <th>Vendor</th>
              <th>Category</th>
              <th>Paid by</th>
              <th className="col-num">Total</th>
              <th>Status</th>
              <th className="col-act" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {bills.map((b) => (
              <tr
                key={b.id}
                className={b.id === justSaved ? "row-highlight" : ""}
              >
                <td>{b.bill_date || <span className="hint">no date</span>}</td>
                <td>
                  <Link to={`/bills/${b.id}`}>
                    {b.vendor_name || "(unnamed vendor)"}
                  </Link>
                </td>
                <td>{b.category.replace(/_/g, " ")}</td>
                <td>{b.payment_method}</td>
                <td className="col-num">{rupees(b.grand_total)}</td>
                <td>
                  {!b.validation_ok && (
                    <span className="pill pill-error" title="Math did not reconcile">
                      check math
                    </span>
                  )}
                  {b.status === "pending_review" ? (
                    <span className="pill">pending</span>
                  ) : (
                    <span className="pill pill-ok">confirmed</span>
                  )}
                  {b.effective_confidence === "low" && (
                    <span className="pill pill-warn">low confidence</span>
                  )}
                </td>
                <td className="col-act">
                  <ConfirmButton
                    className="danger-btn"
                    title={`Delete this bill${
                      b.vendor_name ? ` from ${b.vendor_name}` : ""
                    } and its photo`}
                    onConfirm={() => remove(b.id)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
