// frontend/src/api.js
// One place that knows how to talk to the backend, so every screen reports
// failures the same way instead of each one inventing its own error text.

async function request(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch {
      // Non-JSON error body: the status line is all we have.
    }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  scanBill(file, { signal } = {}) {
    const form = new FormData();
    form.append("file", file);
    return request("/api/bills", { method: "POST", body: form, signal });
  },

  // Starts a bill with no extraction, for typing in by hand. Needs no API
  // credentials: only the reading step is skipped.
  createManualBill() {
    return request("/api/bills/manual", { method: "POST" });
  },

  attachImage(id, file) {
    const form = new FormData();
    form.append("file", file);
    return request(`/api/bills/${id}/image`, { method: "POST", body: form });
  },

  listBills(params = {}) {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null)
    );
    return request(`/api/bills?${qs}`);
  },

  getBill(id) {
    return request(`/api/bills/${id}`);
  },

  // Export URLs are plain links rather than fetches: the browser handles the
  // download, and the CSV one is meant to be copied into Excel.
  exportUrl(kind, params = {}) {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null)
    );
    const query = qs.toString();
    return `/api/bills.${kind}${query ? `?${query}` : ""}`;
  },

  async getBillImage(id, { signal } = {}) {
    const res = await fetch(`/api/bills/${id}/image`, { signal });
    if (!res.ok) throw new Error("Could not load the original image");
    return res.blob();
  },

  // Re-runs the backend's reconciliation against unsaved edits, so the
  // highlighted fields track what is on screen rather than the first
  // extraction. Same code path as confirm, so the two cannot disagree.
  validateBill(id, bill, { signal } = {}) {
    return request(`/api/bills/${id}/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bill),
      signal,
    });
  },

  confirmBill(id, bill, train, vendorNotes = null) {
    return request(`/api/bills/${id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bill, train, vendor_notes: vendorNotes }),
    });
  },

  deleteBill(id) {
    return request(`/api/bills/${id}`, { method: "DELETE" });
  },

  listVendors() {
    return request("/api/vendors");
  },
};

export const CATEGORIES = [
  "groceries", "vegetables", "fruits", "dairy", "bakery",
  "transport", "fuel", "food_and_beverage", "stationery",
  "utilities", "maintenance", "medical", "courier",
  "raw_material", "packaging", "miscellaneous",
];

export const PAYMENT_METHODS = ["cash", "upi", "card", "unknown"];

export const rupees = (n) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(Number(n) || 0);
