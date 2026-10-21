// frontend/src/components/ExportBar.jsx
import { useState } from "react";
import { api } from "../api";

/**
 * Download the current view as a workbook, or bind Excel to it live.
 *
 * Both carry whatever filters are on screen, so "only confirmed groceries"
 * exports as exactly that. The CSV link is an absolute URL because Excel
 * fetches it itself, outside the browser and outside the Vite proxy — it has
 * to point at the backend's own port.
 */
export function ExportBar({ filters, count }) {
  const [showLink, setShowLink] = useState(false);
  const [granularity, setGranularity] = useState("bills");
  const [copied, setCopied] = useState(false);

  const csvPath = api.exportUrl("csv", { ...filters, granularity });
  // The dev server proxies /api to :8000; Excel cannot, so name the backend.
  const csvUrl = `${window.location.protocol}//${window.location.hostname}:8000${csvPath}`;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(csvUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false); // clipboard blocked; the URL is on screen to select
    }
  };

  return (
    <div className="export-bar">
      <a
        className="btn"
        href={api.exportUrl("xlsx", filters)}
        title={`Download the ${count} bill${count === 1 ? "" : "s"} shown`}
      >
        Download Excel
      </a>
      <button
        type="button"
        className="link-btn"
        onClick={() => setShowLink((v) => !v)}
        aria-expanded={showLink}
      >
        {showLink ? "hide Excel link" : "link to Excel"}
      </button>

      {showLink && (
        <div className="export-link">
          <p>
            In Excel: <strong>Data → Get Data → From Other Sources → From
            Web</strong>, paste this URL, then <strong>Load</strong>. After
            that, <strong>Refresh All</strong> re-pulls the current bills — no
            re-importing.
          </p>

          <label className="export-granularity">
            Rows:
            <select
              value={granularity}
              onChange={(e) => setGranularity(e.target.value)}
            >
              <option value="bills">one per bill</option>
              <option value="lines">one per line item</option>
            </select>
          </label>

          <div className="export-url">
            <input readOnly value={csvUrl} onFocus={(e) => e.target.select()} />
            <button type="button" onClick={copy}>
              {copied ? "copied" : "copy"}
            </button>
          </div>

          <p className="hint">
            The link carries the filters above, and works while this app is
            running on your machine. Google Sheets cannot read it — its
            formulas run on Google's servers, which can't reach localhost.
          </p>
        </div>
      )}
    </div>
  );
}
