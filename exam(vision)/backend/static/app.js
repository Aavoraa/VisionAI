/* EXAM Vision — AI Paper Correction with Claude. Plain JS single-page app.
   Every value from the server (student answers especially) is inserted as text, never as HTML. */
"use strict";

// ---------- tiny DOM helpers ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
// append, skipping the null / false that optional parts leave (DOM append would print "null")
const add = (el, ...kids) => el.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
const $ = (sel, root = document) => root.querySelector(sel);
const app = () => $("#app");
function mount(...nodes) { const a = app(); a.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false)); window.scrollTo(0, 0); }
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = ""), bad ? 6000 : 3000);
}
function fmt(n) { return n === null || n === undefined ? "—" : (Math.round(n * 100) / 100).toString(); }
function when(iso) { return iso ? new Date(iso).toLocaleString() : ""; }
function chip(text, kind = "") { return h("span", { class: "chip " + kind }, text); }
const STATUS_KIND = {
  uploaded: "", extracting: "brand", extracted: "brand", evaluating: "brand", evaluated: "ai",
  reviewed: "warn", approved: "ok", needs_attention: "bad", failed: "bad",
  pending: "", suggested: "ai", needs_review: "warn", draft: "", published: "ok",
  verified: "ok", unverified: "", needs_verification: "bad", queued: "brand", running: "brand", done: "ok",
};
const statusChip = (s) => chip((s || "").replaceAll("_", " "), STATUS_KIND[s] || "");

// ---------- API ----------
const state = { token: sessionStorage.getItem("exam_token"), user: null, aiStatus: null };
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers.Authorization = "Bearer " + state.token;
  let body = opts.body;
  if (body && !(body instanceof FormData) && typeof body !== "string") {
    headers["Content-Type"] = "application/json"; body = JSON.stringify(body);
  }
  const res = await fetch(path, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  if (res.status === 401 && !path.startsWith("/api/auth/")) { logout(true); throw new Error("Sign in again."); }
  if (opts.raw) { if (!res.ok) throw new Error("Request failed (" + res.status + ")"); return res; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : "Request failed (" + res.status + ")");
  }
  return data;
}
async function run(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}
async function imageUrl(pageId) {
  const res = await api(`/api/pages/${pageId}/image`, { raw: true });
  return URL.createObjectURL(await res.blob());
}

// ---------- dialogs ----------
function dialog(title, body, actions) {
  const d = h("dialog", {},
    h("div", { class: "dhead" }, h("h2", { style: "margin:0" }, title), h("div", { class: "spacer" }),
      h("button", { class: "link", onclick: () => d.close() }, "Close")),
    h("div", { class: "dbody" }, body),
    actions ? h("div", { class: "dfoot" }, actions) : null);
  d.addEventListener("close", () => d.remove());
  document.body.append(d); d.showModal(); return d;
}
let fieldSeq = 0;
function field(labelText, input, hint) {
  if (input instanceof HTMLElement && !input.id) input.id = "f" + ++fieldSeq;
  return h("div", { class: "field" }, h("label", { for: input.id }, labelText), input, hint ? h("div", { class: "muted small" }, hint) : null);
}
function lines(text) { return (text || "").split("\n").map((s) => s.trim()).filter(Boolean); }

// ---------- auth & shell ----------
function logout(silent) {
  if (!silent && state.token) api("/api/auth/logout", { method: "POST" }).catch(() => {});
  state.token = null; state.user = null; sessionStorage.removeItem("exam_token");
  location.hash = "#/login"; route();
}
function shell() {
  const top = $("#topbar"); top.hidden = !state.user;
  if (!state.user) return;
  const r = state.user.role, links = [];
  if (r === "student") links.push(["#/my", "My results"]);
  else {
    links.push(["#/exams", "Exams"], ["#/students", "Students"]);
    if (r === "admin" || r === "coordinator") links.push(["#/audit", "Audit log"]);
    if (r === "admin") links.push(["#/settings", "Settings"]);
  }
  $("#nav").replaceChildren(...links.map(([href, t]) =>
    h("a", { href, class: location.hash.startsWith(href) ? "active" : "" }, t)));
  $("#who").replaceChildren(h("span", {}, `${state.user.name} · ${r}`),
    h("button", { onclick: changePassword }, "Password"), h("button", { onclick: () => logout() }, "Sign out"));
}
function changePassword() {
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const d = dialog("Change password", [field("Current password", cur), field("New password (8+ characters)", nw)],
    [h("button", { class: "primary", onclick: () => run(() => api("/api/auth/password", { body: { current_password: cur.value, new_password: nw.value } }), "Password changed").then(() => d.close()) }, "Save")]);
}

async function viewLogin() {
  const status = await api("/api/auth/status");
  const user = h("input", { autocomplete: "username" }), pass = h("input", { type: "password", autocomplete: status.needs_setup ? "new-password" : "current-password" });
  const name = h("input", {});
  const submit = async (e) => {
    e.preventDefault();
    const body = status.needs_setup ? { username: user.value, name: name.value, password: pass.value } : { username: user.value, password: pass.value };
    const r = await run(() => api(status.needs_setup ? "/api/auth/setup" : "/api/auth/login", { body }));
    state.token = r.token; state.user = r.user; sessionStorage.setItem("exam_token", r.token);
    location.hash = r.user.role === "student" ? "#/my" : "#/exams";
  };
  mount(h("div", { class: "login" }, h("form", { class: "panel", onsubmit: submit },
    h("h1", {}, status.needs_setup ? "Create the administrator" : "Sign in"),
    h("p", { class: "muted" }, status.needs_setup ? "First run: create the administrator account. You can add teachers and students afterwards." : "EXAM Vision — AI paper correction with Claude"),
    status.needs_setup ? field("Full name", name) : null,
    field("Username", user), field(status.needs_setup ? "Password (8+ characters)" : "Password", pass),
    h("button", { class: "primary", type: "submit", style: "width:100%" }, status.needs_setup ? "Create and sign in" : "Sign in"))));
  user.focus();
}

// ---------- exams ----------
async function viewExams() {
  const exams = await api("/api/exams");
  const manager = ["admin", "coordinator"].includes(state.user.role);
  mount(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Examinations"), h("div", { class: "muted" }, manager ? "All examinations" : "Examinations assigned to you")),
      manager ? h("button", { class: "primary", onclick: () => examForm() }, "New examination") : null),
    exams.length ? h("div", { class: "panel table-wrap" }, h("table", {},
      h("tr", {}, ["Examination", "Class", "Subject", "Questions", "Sheets", "Pending review", "Approved", "Attention", "Results"].map((t) => h("th", {}, t))),
      exams.map((e) => h("tr", {},
        h("td", {}, h("a", { href: `#/exam/${e.id}` }, e.name), h("div", { class: "muted small" }, e.academic_year)),
        h("td", {}, `${e.class_name} ${e.section}`), h("td", {}, e.subject),
        h("td", { class: "num" }, `${e.question_count} (${fmt(e.maximum)} marks)`),
        h("td", { class: "num" }, e.counts.submissions), h("td", { class: "num" }, e.counts.pending_review),
        h("td", { class: "num" }, e.counts.approved), h("td", { class: "num" }, e.counts.failed ? chip(e.counts.failed, "bad") : "0"),
        h("td", {}, statusChip(e.results_status))))))
      : h("div", { class: "panel muted" }, manager ? "No examinations yet. Create one to start." : "No examinations are assigned to you yet."));
}

function examForm(exam) {
  const f = {};
  const inp = (k, ph) => (f[k] = h("input", { value: exam ? exam[k] || "" : "", placeholder: ph || "" }));
  f.instructions = h("textarea", {}, exam ? exam.instructions : "");
  const d = dialog(exam ? "Edit examination" : "New examination", h("div", {},
    field("Name", inp("name", "Unit Test 1")),
    h("div", { class: "grid two" }, field("Academic year", inp("academic_year", "2026-27")), field("Subject", inp("subject", "Science"))),
    h("div", { class: "grid two" }, field("Class", inp("class_name", "7")), field("Section (optional)", inp("section", "A"))),
    field("Grade level / board (optional)", inp("grade_level", "CBSE Grade 7")),
    field("Instructions for markers (optional)", f.instructions)),
    [h("button", { class: "primary", onclick: async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value || ""]));
      const r = await run(() => api(exam ? `/api/exams/${exam.id}` : "/api/exams", { method: exam ? "PUT" : "POST", body }), "Saved");
      d.close(); location.hash = `#/exam/${r.id}`; route();
    } }, "Save")]);
}

async function viewExam(id, tab) {
  tab = tab || "sheets";
  const exam = await api(`/api/exams/${id}`);
  const manager = ["admin", "coordinator"].includes(state.user.role);
  const tabs = [["sheets", "Answer sheets"], ["questions", "Questions & rubrics"], ["results", "Results & reports"]];
  if (manager) tabs.push(["setup", "Exam settings"]);
  const steps = { draft: [["reviewed", "Mark as reviewed"]], reviewed: [["approved", "Approve results"], ["draft", "Back to draft"]],
    approved: [["published", "Publish to students"], ["draft", "Back to draft"]], published: [["draft", "Unpublish"]] };
  const body = h("div", {});
  mount(
    h("div", { class: "page-head" },
      h("div", {}, h("div", { class: "muted small" }, h("a", { href: "#/exams" }, "Examinations"), " / ", exam.academic_year),
        h("h1", {}, exam.name), h("div", { class: "muted" }, `Class ${exam.class_name} ${exam.section} · ${exam.subject} · ${exam.question_count} questions · ${fmt(exam.maximum)} marks`)),
      h("div", { class: "row" }, h("span", { class: "muted" }, "Results:"), statusChip(exam.results_status),
        manager ? (steps[exam.results_status] || []).map(([t, label]) => h("button", { class: t === "published" ? "ok" : "", onclick: async () => {
          if (t === "published" && !confirm("Publish results? Students will see their marks and feedback.")) return;
          await run(() => api(`/api/exams/${id}/results/${t}`, { method: "POST" }), "Results: " + t); route();
        } }, label)) : null)),
    h("div", { class: "tabs" }, tabs.map(([k, t]) => h("a", { href: `#/exam/${id}/${k}`, class: k === tab ? "active" : "" }, t))),
    body);
  if (tab === "sheets") return tabSheets(exam, body);
  if (tab === "questions") return tabQuestions(exam, body, manager);
  if (tab === "results") return tabResults(exam, body);
  if (tab === "setup") return tabSetup(exam, body);
}

// ----- answer sheets tab -----
async function tabSheets(exam, body) {
  const [subs, students, ai] = await Promise.all([api(`/api/exams/${exam.id}/submissions`), api(`/api/students?class_name=${encodeURIComponent(exam.class_name)}`), api("/api/settings/ai/status")]);
  state.aiStatus = ai;
  const uploaded = new Set(subs.map((s) => s.student.id));
  const eligible = students.filter((s) => !uploaded.has(s.id) && (!exam.section || !s.section || s.section === exam.section));
  const classLabel = `Class ${exam.class_name}${exam.section ? " " + exam.section : ""}`;
  const studentSel = h("select", {}, h("option", { value: "" },
    eligible.length ? "Choose the student…" : students.length ? `Every ${classLabel} student already has a sheet` : `No students in ${classLabel} yet`),
    eligible.map((s) => h("option", { value: s.id }, `${s.name} (${s.admission_no}${s.roll_no ? ", roll " + s.roll_no : ""})`)));
  // only this exam's class is listed: say how to add students when there are none
  const studentHint = eligible.length || students.length
    ? "Identity comes from your choice here, never from what the AI reads."
    : h("span", {}, `Only ${classLabel} students are listed. Add them under `, h("a", { href: "#/students" }, "Students"),
        ` with class "${exam.class_name}"${exam.section ? ` and section "${exam.section}"` : ""}, then come back.`);
  const files = h("input", { type: "file", multiple: true, accept: ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" });
  const auto = h("input", { type: "checkbox", checked: true });
  const bulk = h("input", { type: "file", multiple: true, accept: ".pdf,.jpg,.jpeg,.png" });
  const bulkOut = h("div", {});
  const upload = async () => {
    if (!studentSel.value || !files.files.length) return toast("Choose the student and the page files.", true);
    const fd = new FormData(); fd.append("student_id", studentSel.value); fd.append("auto_process", auto.checked);
    for (const f of files.files) fd.append("files", f);
    await run(() => api(`/api/exams/${exam.id}/submissions`, { method: "POST", body: fd }), "Uploaded");
    route();
  };
  const bulkUpload = async () => {
    if (!bulk.files.length) return toast("Choose files named with admission numbers.", true);
    const fd = new FormData(); fd.append("auto_process", auto.checked);
    for (const f of bulk.files) fd.append("files", f);
    const r = await run(() => api(`/api/exams/${exam.id}/submissions/bulk`, { method: "POST", body: fd }));
    bulkOut.replaceChildren(h("ul", {}, r.results.map((x) => h("li", {}, `${x.file}: `, x.ok ? chip(x.student, "ok") : chip(x.error, "bad")))));
    setTimeout(route, 2500);
  };
  add(body,
    h("div", { class: "grid two" },
      h("div", { class: "panel" }, h("h2", {}, "Upload one answer sheet"),
        field("Student", studentSel, studentHint),
        field("Pages (PDF, or photos in page order)", files),
        h("label", { class: "check" }, auto, `Read and mark automatically with Claude (${ai.vision_model} / ${ai.eval_model})`),
        h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "primary", onclick: upload }, "Upload"))),
      h("div", { class: "panel" }, h("h2", {}, "Bulk upload"),
        h("p", { class: "muted small" }, "One file per student, named with the admission number, e.g. A1001.pdf. You still confirm each student's identity in the review screen."),
        field("Files", bulk), h("button", { onclick: bulkUpload }, "Upload all"), bulkOut)),
    !ai.reading_available ? h("div", { class: "notice warn" }, `Sheets can be uploaded, but Claude cannot read them yet: ${ai.reading_reason}`) : null,
    h("div", { class: "panel table-wrap" }, h("h2", {}, `Answer sheets (${subs.length})`),
      subs.length ? h("table", {},
        h("tr", {}, ["Student", "Status", "Marks", "Pages", "Identity", "Name on sheet", "Claude cost", "Activity", ""].map((t) => h("th", {}, t))),
        subs.map((s) => h("tr", {},
          h("td", {}, h("a", { href: `#/sheet/${s.id}` }, s.student.name), h("div", { class: "muted small" }, s.student.admission_no)),
          h("td", {}, statusChip(s.status), s.status_note ? h("div", { class: "small", style: "color:var(--bad);max-width:280px" }, s.status_note) : null),
          h("td", { class: "num" }, `${fmt(s.totals.total)} / ${fmt(s.totals.maximum)}`, h("div", { class: "muted small" }, `${s.totals.marked}/${s.totals.questions} marked`)),
          h("td", { class: "num" }, s.pages),
          h("td", {}, s.identity_verified ? chip("confirmed", "ok") : chip("to confirm", "warn")),
          h("td", {}, !s.name_check ? "" : chip(s.name_check.status === "match" ? "matches" : s.name_check.status === "differs" ? "differs" : "none",
            s.name_check.status === "match" ? "ok" : s.name_check.status === "differs" ? "bad" : ""), s.name_check && s.name_check.status === "differs" ? h("div", { class: "small muted" }, s.name_check.detail) : null),
          h("td", { class: "num small" }, s.cost.cost_usd ? `$${s.cost.cost_usd}` : "—"),
          h("td", { class: "small" }, s.job ? [statusChip(s.job.status), " ", s.job.kind, s.job.progress ? h("div", { class: "muted" }, s.job.progress) : null, s.job.error ? h("div", { style: "color:var(--bad)" }, s.job.error) : null] : ""),
          h("td", {}, h("a", { href: `#/sheet/${s.id}` }, "Review →")))))
        : h("p", { class: "muted" }, "No answer sheets uploaded yet.")));
  if (subs.some((s) => s.job && ["queued", "running"].includes(s.job.status))) poll(() => location.hash.startsWith(`#/exam/${exam.id}`) && !location.hash.includes("/questions") && !location.hash.includes("/results") && !location.hash.includes("/setup"));
}
function poll(stillHere) {
  clearTimeout(poll._t);
  poll._t = setTimeout(() => { if (stillHere()) route(); }, 3000);
}

// ----- questions & rubrics tab -----
function tabQuestions(exam, body, manager) {
  add(body,
    h("div", { class: "row", style: "margin-bottom:12px" },
      manager ? h("button", { class: "primary", onclick: () => questionForm(exam) }, "Add question") : null,
      exam.questions.some((q) => !q.rubric) ? h("button", { class: "ai", onclick: async (e) => {
        if (!confirm("Draft and save rubrics with AI for every question that has none? They are marked 'AI draft' — please review them.")) return;
        e.target.disabled = true; e.target.textContent = "Drafting rubrics…";
        const r = await run(() => api(`/api/exams/${exam.id}/rubrics/draft-all`, { body: { only_missing: true } })).finally(() => { e.target.disabled = false; });
        toast(`Rubrics drafted for ${r.saved.length} question(s)` + (r.failed.length ? `; ${r.failed.length} failed: ${r.failed[0].error}` : "")); route();
      } }, "Draft missing rubrics with AI") : null,
      h("button", { onclick: async () => {
        if (!confirm("Re-evaluate every answer that is not yet approved with the current rubrics?")) return;
        const r = await run(() => api(`/api/exams/${exam.id}/reevaluate`, { body: {} })); toast(`${r.jobs_queued} sheet(s) queued for re-evaluation`);
      } }, "Re-evaluate unapproved answers")),
    exam.questions.length ? exam.questions.map((q) => h("div", { class: "panel" },
      h("div", { class: "row" }, h("span", { class: "qnum" }, `Q${q.number}`), chip(q.qtype), chip(`${fmt(q.max_marks)} marks`, "brand"),
        q.rubric ? chip(`rubric v${q.rubric.version}`, "ok") : chip("no rubric", "bad"),
        q.rubric && q.rubric.source === "ai_draft" ? chip("AI draft — review", "ai") : null, h("div", { class: "spacer" }),
        manager ? h("button", { onclick: () => questionForm(exam, q) }, "Edit question") : null,
        h("button", { class: "primary", onclick: () => rubricForm(q) }, q.rubric ? "Edit rubric (new version)" : "Define rubric"),
        q.rubric ? h("button", { onclick: () => rubricHistory(q) }, "History") : null),
      h("p", {}, q.text),
      q.numeric_answer ? h("p", { class: "small muted" }, `Checked by code: final answer ${q.numeric_answer}${q.unit ? " " + q.unit : ""} (±${q.numeric_tolerance})`) : null,
      q.rubric_problems.length ? h("div", { class: "notice warn" }, "Needs teacher clarification: ", q.rubric_problems.join("; ")) : null,
      q.rubric ? h("table", {}, h("tr", {}, h("th", {}, "Criterion"), h("th", { class: "num" }, "Marks")),
        q.rubric.criteria.map((c) => h("tr", {}, h("td", {}, h("b", {}, c.id + " "), c.name, c.description ? h("div", { class: "muted small" }, c.description) : null), h("td", { class: "num" }, fmt(c.max_marks))))) : null,
      q.rubric && q.rubric.reference_answer ? h("p", { class: "small" }, h("b", {}, "Reference answer: "), q.rubric.reference_answer) : null))
      : h("div", { class: "panel muted" }, "No questions yet."));
}
function questionForm(exam, q) {
  const f = {
    number: h("input", { value: q ? q.number : String(exam.questions.length + 1) }),
    text: h("textarea", {}, q ? q.text : ""),
    qtype: h("select", {}, ["short", "long", "math", "science", "diagram", "essay", "comprehension", "translation"].map((t) => h("option", { value: t, selected: q && q.qtype === t }, t))),
    max_marks: h("input", { type: "number", step: "0.5", min: "0.5", value: q ? q.max_marks : 2 }),
    mark_increment: h("select", {}, [["0.5", "half marks"], ["1", "whole marks"], ["0.25", "quarter marks"]].map(([v, t]) => h("option", { value: v, selected: q ? String(q.mark_increment) === v : v === "0.5" }, t))),
    numeric_answer: h("input", { value: q && q.numeric_answer ? q.numeric_answer : "", placeholder: "e.g. 60 (optional)" }),
    numeric_tolerance: h("input", { type: "number", step: "any", value: q ? q.numeric_tolerance : 0.01 }),
    unit: h("input", { value: q && q.unit ? q.unit : "", placeholder: "e.g. km/h (optional)" }),
  };
  const d = dialog(q ? `Edit question ${q.number}` : "Add question", h("div", {},
    h("div", { class: "grid two" }, field("Number", f.number, "1, 2 or 2a"), field("Type", f.qtype)),
    field("Question text", f.text),
    h("div", { class: "grid two" }, field("Maximum marks", f.max_marks), field("Marks in steps of", f.mark_increment)),
    h("h3", {}, "Numerical answer check (optional)"),
    h("p", { class: "muted small" }, "For calculations: the backend checks the student's final number by code, independently of the AI."),
    h("div", { class: "grid two" }, field("Expected final answer", f.numeric_answer), field("Tolerance", f.numeric_tolerance)),
    field("Unit", f.unit)),
    [q ? h("button", { class: "danger", onclick: async () => { if (!confirm("Delete this question?")) return; await run(() => api(`/api/questions/${q.id}`, { method: "DELETE" }), "Deleted"); d.close(); route(); } }, "Delete") : null,
     h("button", { class: "primary", onclick: async () => {
       const body = { number: f.number.value, text: f.text.value, qtype: f.qtype.value, max_marks: parseFloat(f.max_marks.value),
         mark_increment: parseFloat(f.mark_increment.value), numeric_answer: f.numeric_answer.value || null,
         numeric_tolerance: parseFloat(f.numeric_tolerance.value || "0.01"), unit: f.unit.value || null };
       await run(() => api(q ? `/api/questions/${q.id}` : `/api/exams/${exam.id}/questions`, { method: q ? "PUT" : "POST", body }), "Saved");
       d.close(); route();
     } }, "Save")]);
}
function rubricForm(q) {
  const r = q.rubric || { criteria: [{ id: "C1", name: "", max_marks: q.max_marks, description: "" }] };
  const rows = h("div", {});
  const addRow = (c) => {
    const row = h("div", { class: "grid", style: "grid-template-columns:70px 1fr 90px 32px;align-items:end;margin-bottom:6px" },
      field("Id", h("input", { value: c.id, "data-k": "id" })), field("What earns the marks", h("input", { value: c.name, "data-k": "name" })),
      field("Marks", h("input", { type: "number", step: q.mark_increment, value: c.max_marks, "data-k": "max_marks" })),
      h("button", { class: "danger", title: "Remove", onclick: () => row.remove() }, "×"));
    row.append(h("div", { style: "grid-column:2/4" }, h("input", { value: c.description || "", placeholder: "Details for the marker (optional)", "data-k": "description" })));
    rows.append(row);
  };
  r.criteria.forEach(addRow);
  const f = {
    reference_answer: h("textarea", {}, r.reference_answer || ""),
    expected_concepts: h("textarea", {}, (r.expected_concepts || []).join("\n")),
    alternatives: h("textarea", {}, (r.alternatives || []).join("\n")),
    misconceptions: h("textarea", {}, (r.misconceptions || []).join("\n")),
    partial_credit_rules: h("textarea", {}, r.partial_credit_rules || ""),
    required_elements: h("input", { value: r.required_elements || "", placeholder: "e.g. formula, working, unit" }),
    ignore_criteria: h("input", { value: r.ignore_criteria || "", placeholder: "e.g. spelling and grammar are not assessed" }),
    subject_rules: h("input", { value: r.subject_rules || "" }),
  };
  let source = "teacher";
  const fill = (dr) => {
    rows.replaceChildren(); dr.criteria.forEach(addRow);
    f.reference_answer.value = dr.reference_answer || "";
    f.expected_concepts.value = (dr.expected_concepts || []).join("\n");
    f.alternatives.value = (dr.alternatives || []).join("\n");
    f.misconceptions.value = (dr.misconceptions || []).join("\n");
    f.partial_credit_rules.value = dr.partial_credit_rules || "";
    f.ignore_criteria.value = dr.ignore_criteria || "";
  };
  const draftBtn = h("button", { class: "ai", onclick: async () => {
    draftBtn.disabled = true; draftBtn.textContent = "Drafting…";
    try { fill(await run(() => api(`/api/questions/${q.id}/rubric/draft`, { method: "POST" }))); source = "ai_draft"; toast("AI draft filled in — check it before saving"); }
    finally { draftBtn.disabled = false; draftBtn.textContent = "Draft with AI"; }
  } }, "Draft with AI");
  const d = dialog(`Rubric for Q${q.number} (${fmt(q.max_marks)} marks)`, h("div", {},
    h("div", { class: "row" }, h("p", { class: "muted small", style: "flex:1" }, q.text), draftBtn),
    h("h3", {}, "Criteria (must add up to the question's marks)"), rows,
    h("button", { onclick: () => addRow({ id: "C" + (rows.children.length + 1), name: "", max_marks: q.mark_increment }) }, "+ Criterion"),
    h("div", { style: "height:12px" }),
    field("Reference answer", f.reference_answer),
    field("Expected concepts (one per line)", f.expected_concepts),
    field("Also accept — valid alternative answers (one per line)", f.alternatives),
    field("Common misconceptions — no credit (one per line)", f.misconceptions),
    field("Partial-credit rules", f.partial_credit_rules, "e.g. method mark even if the arithmetic is wrong"),
    field("Required reasoning / formulas / units", f.required_elements),
    field("Do not assess", f.ignore_criteria),
    field("Subject-specific rules", f.subject_rules),
    h("div", { class: "notice" }, "Saving creates a new rubric version. Approved marks never change by themselves; use Re-evaluate for unapproved answers.")),
    [h("button", { class: "primary", onclick: async () => {
      const criteria = [...rows.children].map((row) => {
        const get = (k) => row.querySelector(`[data-k=${k}]`).value;
        return { id: get("id"), name: get("name"), max_marks: parseFloat(get("max_marks")), description: get("description") };
      });
      const body = { criteria, reference_answer: f.reference_answer.value, expected_concepts: lines(f.expected_concepts.value),
        alternatives: lines(f.alternatives.value), misconceptions: lines(f.misconceptions.value), partial_credit_rules: f.partial_credit_rules.value,
        required_elements: f.required_elements.value, ignore_criteria: f.ignore_criteria.value, subject_rules: f.subject_rules.value, source };
      const res = await run(() => api(`/api/questions/${q.id}/rubric`, { body }), "Rubric saved");
      if (res.answers_to_reevaluate) toast(`${res.answers_to_reevaluate} answer(s) were marked with the old rubric — use Re-evaluate.`);
      d.close(); route();
    } }, "Save new version")]);
}
async function rubricHistory(q) {
  const hist = await api(`/api/questions/${q.id}/rubrics`);
  dialog(`Rubric history — Q${q.number}`, hist.map((r) => h("div", { class: "panel" },
    h("div", { class: "row" }, chip(`v${r.version}`, "brand"), h("span", { class: "muted small" }, when(r.created_at))),
    h("ul", {}, r.criteria.map((c) => h("li", {}, `${c.id} (${fmt(c.max_marks)}): ${c.name}`))))));
}

// ----- results tab -----
async function tabResults(exam, body) {
  const r = await api(`/api/exams/${exam.id}/report`);
  const s = r.summary, av = r.ai_vs_teacher;
  const stat = (v, label) => h("div", { class: "stat" }, h("b", {}, v), h("span", {}, label));
  const download = async () => {
    const res = await run(() => api(`/api/exams/${exam.id}/report.csv`, { raw: true }));
    const a = h("a", { href: URL.createObjectURL(await res.blob()), download: `${exam.name}_results.csv` }); a.click();
  };
  add(body,
    h("div", { class: "grid four", style: "margin-bottom:16px" },
      stat(s.submissions, "answer sheets"), stat(s.approved_sheets, "fully approved"), stat(s.pending_review, "answers to review"),
      stat(s.failed_or_attention, "need attention"), stat(fmt(s.class_average), `class average / ${fmt(r.exam.maximum)}`),
      stat(`${fmt(s.highest)} / ${fmt(s.lowest)}`, "highest / lowest")),
    h("div", { class: "panel table-wrap" }, h("div", { class: "row" }, h("h2", {}, "Students"), h("div", { class: "spacer" }), h("button", { onclick: download }, "Download CSV")),
      h("table", {}, h("tr", {}, ["Student", "Admission no", "Total", "%", "Approved", "Status"].map((t) => h("th", {}, t))),
        r.students.map((x) => h("tr", {}, h("td", {}, h("a", { href: `#/sheet/${x.submission_id}` }, x.name)), h("td", {}, x.admission_no),
          h("td", { class: "num" }, `${fmt(x.total)} / ${fmt(x.maximum)}`), h("td", { class: "num" }, fmt(x.percent)),
          h("td", {}, x.approved ? chip("yes", "ok") : chip("no", "warn")), h("td", {}, statusChip(x.status)))))),
    h("div", { class: "panel table-wrap" }, h("h2", {}, "Question-wise performance"),
      h("table", {}, h("tr", {}, ["Q", "Question", "Max", "Average", "Avg %", "Full marks", "Zero", "To review"].map((t) => h("th", {}, t))),
        r.questions.map((q) => h("tr", {}, h("td", {}, q.number), h("td", {}, q.text), h("td", { class: "num" }, fmt(q.max_marks)),
          h("td", { class: "num" }, fmt(q.average)), h("td", { class: "num" }, fmt(q.average_percent)), h("td", { class: "num" }, q.full_marks),
          h("td", { class: "num" }, q.zero), h("td", { class: "num" }, q.needs_review))))),
    h("div", { class: "grid two" },
      h("div", { class: "panel" }, h("h2", {}, "Common misconceptions"), r.common_misconceptions.length ? h("ol", {}, r.common_misconceptions.map(([t, n]) => h("li", {}, `${t} (${n})`))) : h("p", { class: "muted" }, "None found yet.")),
      h("div", { class: "panel" }, h("h2", {}, "Commonly missing concepts"), r.common_missing_concepts.length ? h("ol", {}, r.common_missing_concepts.map(([t, n]) => h("li", {}, `${t} (${n})`))) : h("p", { class: "muted" }, "None found yet."))),
    h("div", { class: "grid two" },
      h("div", { class: "panel" }, h("h2", {}, "AI suggestion vs teacher-approved marks"),
        h("table", {}, [["Approved answers with an AI suggestion", av.approved_with_ai_suggestion], ["Exact agreement", av.exact_agreement],
          ["Within half a mark", av.within_half_mark], ["Mean absolute difference", fmt(av.mean_absolute_difference)],
          ["Teacher gave more", av.teacher_higher], ["Teacher gave less", av.teacher_lower], ["Mark overrides recorded", r.overrides.count],
          ["Transcriptions corrected by teachers", `${r.ocr.corrected_by_teacher} of ${r.ocr.answers}`]].map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", { class: "num" }, v))))),
      h("div", { class: "panel" }, h("h2", {}, "Processing"),
        h("table", {}, h("tr", {}, ["Claude readings", "Failed", "Avg time per sheet", "Reading cost (USD)"].map((t) => h("th", {}, t))),
          h("tr", {}, h("td", { class: "num" }, r.processing.runs), h("td", { class: "num" }, r.processing.failed),
            h("td", { class: "num" }, r.processing.average_seconds === null ? "—" : r.processing.average_seconds + " s"), h("td", { class: "num" }, r.processing.cost_usd))),
        h("p", { class: "muted small" }, `Marking cost so far: $${r.evaluation_cost_usd} (estimated from token usage).`))));
}

// ----- exam settings tab -----
async function tabSetup(exam, body) {
  const users = await api("/api/users");
  const teachers = users.filter((u) => u.role === "teacher" && u.active);
  const assigned = new Set(exam.teachers.map((t) => t.id));
  const boxes = teachers.map((t) => h("label", { class: "check" }, h("input", { type: "checkbox", value: t.id, checked: assigned.has(t.id) }), `${t.name} (${t.username})`));
  add(body,
    h("div", { class: "grid two" },
      h("div", { class: "panel" }, h("h2", {}, "Examination details"), h("p", { class: "muted" }, "Name, class, subject and instructions for this exam."),
        h("button", { onclick: () => examForm(exam) }, "Edit details")),
      h("div", { class: "panel" }, h("h2", {}, "Assigned teachers"),
        teachers.length ? boxes : h("p", { class: "muted" }, "No teacher accounts yet (Settings → Users)."),
        h("button", { class: "primary", style: "margin-top:8px", onclick: async () => {
          const ids = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
          await run(() => api(`/api/exams/${exam.id}/teachers`, { method: "PUT", body: { user_ids: ids } }), "Teachers assigned");
        } }, "Save"))));
}

// ---------- sheet review ----------
async function viewSheet(id, focusQ) {
  const [s, ai] = await Promise.all([api(`/api/submissions/${id}`), api("/api/settings/ai/status")]);
  state.aiStatus = ai;
  const busy = s.job && ["queued", "running"].includes(s.job.status);
  const ext = s.active_extraction;
  // viewer
  let pageIdx = 0, zoom = 1;
  const img = h("img", { alt: "answer sheet page" });
  const pageLabel = h("span", { class: "chip" });
  const qualityBox = h("span", { class: "small" });
  const showPage = async () => {
    const p = s.page_list[pageIdx]; if (!p) return;
    pageLabel.textContent = `Page ${p.page_no} / ${s.page_list.length}`;
    qualityBox.replaceChildren(...(p.quality.warnings || []).map((w) => chip(w, "warn")));
    img.src = await imageUrl(p.id);
    img.style.width = `${zoom * 100}%`;
  };
  const go = (d) => { pageIdx = Math.max(0, Math.min(s.page_list.length - 1, pageIdx + d)); showPage(); };
  const viewer = h("div", { class: "viewer" },
    h("div", { class: "bar" }, h("button", { onclick: () => go(-1) }, "‹"), pageLabel, h("button", { onclick: () => go(1) }, "›"),
      h("button", { onclick: () => { zoom = Math.max(.4, zoom - .2); img.style.width = `${zoom * 100}%`; } }, "−"),
      h("button", { onclick: () => { zoom = Math.min(3, zoom + .2); img.style.width = `${zoom * 100}%`; } }, "+"),
      h("button", { title: "Rotate the stored page 90° clockwise", onclick: async () => { await run(() => api(`/api/pages/${s.page_list[pageIdx].id}/rotate`, { body: { degrees: 90 } }), "Rotated; read the sheet again to use it"); showPage(); } }, "⟳"),
      s.page_list.length > 1 ? h("button", { onclick: () => pageOrder(s) }, "Page order") : null, qualityBox),
    h("div", { class: "canvas" }, img));

  // header: identity + reading
  const identity = h("div", { class: "panel" },
    h("div", { class: "row" }, h("div", {}, h("div", { class: "muted small" }, h("a", { href: `#/exam/${s.exam.id}` }, s.exam.name), " / answer sheet"),
      h("h1", {}, s.student_full.name), h("div", { class: "muted" }, `Admission ${s.student_full.admission_no} · Class ${s.student_full.class_name} ${s.student_full.section} · Roll ${s.student_full.roll_no || "—"}`)),
      h("div", { class: "spacer" }), h("div", { style: "text-align:right" }, h("div", { style: "font-size:22px;font-weight:700" }, `${fmt(s.totals.total)} / ${fmt(s.totals.maximum)}`),
        h("div", { class: "muted small" }, s.totals.approved ? "all marks approved" : `${s.totals.marked}/${s.totals.questions} marked`), statusChip(s.status))),
    s.status_note ? h("div", { class: "notice bad", style: "margin-top:10px" }, s.status_note) : null,
    h("div", { class: "row", style: "margin-top:10px" },
      s.identity_verified ? chip("identity confirmed", "ok") : [chip("identity not confirmed", "warn"),
        h("button", { onclick: async () => { await run(() => api(`/api/submissions/${id}/identity`, { body: { verified: true } }), "Identity confirmed"); route(); } }, "Confirm: this sheet is this student's")],
      h("button", { class: "link", onclick: () => reassign(s) }, "Wrong student?")));

  const nc = s.name_check;
  const readActions = h("div", { class: "row" },
    ai.reading_available
      ? h("button", { class: "ai", disabled: busy, onclick: () => startJob(`/api/submissions/${id}/process`, {}, "Claude is reading the sheet") }, ext ? "Read again with Claude" : "Read & mark with Claude")
      : h("span", { class: "notice warn small", style: "margin:0" }, `Reading unavailable: ${ai.reading_reason}`),
    ext && ai.marking_available ? h("button", { disabled: busy, onclick: () => startJob(`/api/submissions/${id}/evaluate`, { force: false }, "Marking") }, "Mark unmarked answers") : null);

  const reading = h("div", { class: "panel" }, h("h2", {}, "Reading"),
    busy ? h("div", { class: "notice" }, `${s.job.kind} in progress… ${s.job.progress || ""}`) : null,
    s.job && s.job.status === "failed" ? h("div", { class: "notice bad" }, `Last job failed: ${s.job.error || ""}. You can retry.`) : null,
    ext ? h("div", { class: "small" }, "Read by ", chip(ext.model || "Claude", "ai"), ` in ${(ext.duration_ms / 1000).toFixed(1)} s`,
      ext.redacted ? [" ", chip("name area blanked before sending", "")] : null,
      " ", h("button", { class: "link", onclick: () => fullText(ext) }, "Full transcription")) : h("p", { class: "muted" }, "Not read yet."),
    nc ? h("div", { class: "row small", style: "margin-top:6px" }, chip(nc.status === "match" ? "name matches" : nc.status === "differs" ? "name differs" : "no name on sheet",
      nc.status === "match" ? "ok" : nc.status === "differs" ? "bad" : ""), nc.detail) : null,
    h("div", { class: "small muted", style: "margin-top:6px" }, `Claude usage for this sheet: ${s.cost.input_tokens + s.cost.output_tokens} tokens · about $${s.cost.cost_usd}`),
    ext && ext.unassigned_text ? h("details", { class: "small", style: "margin-top:6px" }, h("summary", {}, "Writing not under any question"), h("div", { class: "answer-text" }, ext.unassigned_text)) : null,
    ext && ext.warnings.length ? h("ul", { class: "flags small" }, ext.warnings.map((w) => h("li", {}, w))) : null,
    s.extractions.length > 1 ? h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, `${s.extractions.length} readings`),
      h("table", {}, s.extractions.map((e) => h("tr", {}, h("td", {}, e.model || ""), h("td", {}, statusChip(e.status)),
        h("td", { class: "small" }, when(e.created_at)), h("td", {}, e.active ? chip("in use", "ok") : e.status === "done"
          ? h("button", { onclick: async () => { if (!confirm("Use this reading for the answers? Teacher corrections and approved marks are kept.")) return; await run(() => api(`/api/submissions/${id}/extractions/${e.id}/activate`, { method: "POST" }), "Reading switched"); route(); } }, "Use this") : (e.error || "")))))) : null,
    h("div", { style: "margin-top:10px" }, readActions));

  const cards = s.questions.map((row, i) => questionCard(s, row, i, (pages) => { if (pages && pages.length) { pageIdx = pages[0] - 1; showPage(); } }));
  mount(h("div", { class: "review" }, viewer, h("div", {}, identity, reading, cards,
    h("div", { class: "row" }, h("a", { href: `#/exam/${s.exam.id}` }, "← Back to answer sheets")))));
  showPage();
  if (focusQ) { const c = document.getElementById("q-" + focusQ); if (c) { c.scrollIntoView({ block: "start" }); c.classList.add("focus"); } }
  if (busy) poll(() => location.hash === `#/sheet/${id}`);

  async function startJob(path, body, msg) { await run(() => api(path, { body }), msg + "…"); route(); }
}

function questionCard(s, row, index, showPages) {
  const q = row.question, a = row.answer, ev = row.evaluation, r = ev && ev.result;
  const id = "q-" + q.number;
  const approved = a && a.mark_status === "approved";
  if (!a) return h("div", { class: "qcard", id }, h("div", { class: "qhead" }, h("span", { class: "qnum" }, `Q${q.number}`), h("span", {}, q.text), h("div", { class: "spacer" }), chip("not read yet")));
  const textArea = h("textarea", { style: "min-height:110px;font-family:Consolas,monospace", disabled: approved }, a.text);
  const crits = (q.rubric && q.rubric.criteria) || [];
  const inputs = {};
  const markRows = crits.map((c) => {
    const current = a.criterion_marks && a.criterion_marks[c.id] !== undefined ? a.criterion_marks[c.id] : "";
    inputs[c.id] = h("input", { type: "number", min: 0, max: c.max_marks, step: q.mark_increment, value: current, disabled: approved });
    return h("div", { class: "marks-row" }, h("div", {}, h("b", {}, c.id + " "), c.name, h("span", { class: "muted" }, ` / ${fmt(c.max_marks)}`)), inputs[c.id]);
  });
  const reason = h("input", { placeholder: "Reason for changing the AI suggestion (required when changing marks)", disabled: approved });
  const feedback = h("textarea", { placeholder: "Feedback for the student (optional)", style: "min-height:50px", disabled: approved }, a.feedback || "");
  const total = h("b", {}, fmt(a.final_marks));
  const recalc = () => { const t = crits.reduce((acc, c) => acc + (parseFloat(inputs[c.id].value) || 0), 0); total.textContent = fmt(t); };
  Object.values(inputs).forEach((i) => i.addEventListener("input", recalc));

  const saveMarks = async () => {
    const criterion_marks = Object.fromEntries(crits.map((c) => [c.id, parseFloat(inputs[c.id].value)]));
    if (Object.values(criterion_marks).some((v) => Number.isNaN(v))) return toast("Enter marks for every criterion.", true);
    await run(() => api(`/api/answers/${a.id}/marks`, { method: "PUT", body: { criterion_marks, reason: reason.value, feedback: feedback.value } }), "Marks saved");
    return true;
  };
  const body = h("div", { class: "qbody" },
    h("p", { style: "margin-top:0" }, q.text),
    h("div", { class: "row small", style: "margin-bottom:6px" }, h("b", {}, "Student's answer"), statusChip(a.ocr_status),
      a.page_refs.length ? h("button", { class: "link", onclick: () => showPages(a.page_refs) }, `page ${a.page_refs.join(", ")}`) : null,
      a.corrected_text !== null ? chip("corrected by teacher", "brand") : null),
    textArea,
    a.corrected_text !== null ? h("details", { class: "small" }, h("summary", {}, "Original reading"), h("div", { class: "answer-text" }, a.original_text || "(empty)")) : null,
    approved ? null : h("div", { class: "row", style: "margin:6px 0" },
      h("button", { onclick: async () => { await run(() => api(`/api/answers/${a.id}/text`, { method: "PATCH", body: { text: textArea.value } }), "Transcription saved"); route(); } }, "Save corrected text"),
      a.ocr_status !== "verified" ? h("button", { onclick: async () => { await run(() => api(`/api/answers/${a.id}/verify`, { method: "POST" }), "Transcription verified"); route(); } }, "Transcription is correct") : null),
    a.flags.length ? h("ul", { class: "flags small" }, a.flags.map((f) => h("li", {}, f))) : null,
    ev ? aiBox(ev, r) : null,
    q.rubric ? h("div", { class: "panel", style: "margin:10px 0 0;box-shadow:none" },
      h("div", { class: "row" }, h("h3", { style: "margin:0" }, approved ? "Approved marks" : "Teacher marks"), h("div", { class: "spacer" }), h("span", {}, "Total: "), total, h("span", { class: "muted" }, ` / ${fmt(q.max_marks)}`)),
      h("div", { style: "margin-top:8px" }, markRows),
      approved ? (a.feedback ? h("p", { class: "small" }, h("b", {}, "Feedback: "), a.feedback) : null) : [reason, h("div", { style: "height:6px" }), feedback],
      h("div", { class: "row", style: "margin-top:8px" },
        approved ? [chip("approved", "ok"), h("span", { class: "muted small" }, when(a.approved_at)),
          h("button", { onclick: async () => { const why = prompt("Why reopen these approved marks?"); if (!why) return; await run(() => api(`/api/answers/${a.id}/reopen`, { body: { reason: why } }), "Reopened"); route(); } }, "Reopen")]
        : [h("button", { onclick: async () => { if (await saveMarks()) route(); } }, "Save marks"),
           h("button", { class: "ok", onclick: async () => {
             const changed = crits.some((c) => String(parseFloat(inputs[c.id].value)) !== String(a.criterion_marks[c.id])) || feedback.value !== (a.feedback || "");
             if (changed && !(await saveMarks())) return;
             await run(() => api(`/api/answers/${a.id}/approve`, { body: {} }), `Q${q.number} approved`);
             route(`#/sheet/${s.id}`, s.questions[index + 1] && s.questions[index + 1].question.number);
           } }, "Approve"),
           h("button", { class: "ai", onclick: async () => { await run(() => api(`/api/answers/${a.id}/evaluate`, { method: "POST" }), "Re-evaluating…"); route(); } }, "Re-evaluate"),
           h("button", { onclick: async () => { const why = prompt("What needs further review?"); if (!why) return; await run(() => api(`/api/answers/${a.id}/request-review`, { body: { reason: why } }), "Flagged"); route(); } }, "Request review")],
        h("div", { class: "spacer" }), h("button", { class: "link", onclick: () => history(a) }, "History"))) : h("div", { class: "notice warn" }, "This question has no rubric yet. Define it under Questions & rubrics."));
  const head = h("div", { class: "qhead", onclick: () => { body.hidden = !body.hidden; } },
    h("span", { class: "qnum" }, `Q${q.number}`), h("span", { class: "muted small" }, `${fmt(q.max_marks)} marks`), statusChip(a.mark_status),
    ev && ev.requires_review ? chip("review flagged", "warn") : null, h("div", { class: "spacer" }),
    h("b", {}, `${fmt(a.final_marks)} / ${fmt(q.max_marks)}`));
  return h("div", { class: "qcard", id }, head, body);
}

function aiBox(ev, r) {
  if (ev.status !== "ok") return h("div", { class: "notice bad" }, `AI marking ${ev.status}: ${ev.error || ""}`, ev.review_reasons.length ? h("div", {}, ev.review_reasons.join("; ")) : null);
  return h("div", { class: "ai-box" },
    h("div", { class: "row" }, h("h3", { style: "margin:0" }, "AI suggestion — not final"), h("div", { class: "spacer" }),
      chip(`${fmt(ev.suggested_marks)} / ${fmt(r.maximum_marks)}`, "ai"),
      h("span", { class: "muted small" }, `${ev.provider}${ev.model ? " · " + ev.model : ""} · rubric v${ev.rubric_version} · ${ev.prompt_version}`)),
    r.criteria.map((c) => h("div", { class: "crit" },
      h("div", {}, h("b", {}, `${c.criterion_id} `), c.criterion_name, h("div", { class: "small" }, c.explanation),
        c.evidence ? h("div", { class: "evidence" }, "Evidence: ", h("q", {}, c.evidence)) : null),
      h("div", { class: "num" }, h("b", {}, fmt(c.awarded_marks)), ` / ${fmt(c.maximum_marks)}`))),
    r.strengths.length ? h("p", { class: "small" }, h("b", {}, "Strengths: "), r.strengths.join("; ")) : null,
    r.missing_concepts.length ? h("p", { class: "small" }, h("b", {}, "Missing: "), r.missing_concepts.join("; ")) : null,
    r.misconceptions.length ? h("p", { class: "small", style: "color:var(--bad)" }, h("b", {}, "Misconceptions: "), r.misconceptions.join("; ")) : null,
    (ev.checks || []).filter((c) => c.check !== "total" || !c.ok).length ? h("ul", { class: "small" }, ev.checks.filter((c) => c.check !== "total" || !c.ok).map((c) =>
      h("li", {}, chip(c.ok === true ? "check ok" : c.ok === false ? "check failed" : "check", c.ok === true ? "ok" : c.ok === false ? "bad" : ""), " ", c.detail))) : null,
    ev.review_reasons.length ? h("div", { class: "notice warn small", style: "margin:8px 0 0" }, "Review: ", ev.review_reasons.join("; ")) : null);
}

async function history(a) {
  const hst = await api(`/api/answers/${a.id}/history`);
  dialog("Marking history", h("div", {},
    h("h3", {}, "Teacher overrides"), hst.overrides.length ? h("table", {}, h("tr", {}, ["When", "By", "From", "To", "Reason"].map((t) => h("th", {}, t))),
      hst.overrides.map((o) => h("tr", {}, h("td", { class: "small" }, when(o.at)), h("td", {}, o.by), h("td", { class: "num" }, fmt(o.original_marks)), h("td", { class: "num" }, fmt(o.new_marks)), h("td", {}, o.reason)))) : h("p", { class: "muted" }, "None."),
    h("h3", {}, "AI evaluations"), hst.evaluations.map((e) => h("div", { class: "small", style: "margin-bottom:6px" }, statusChip(e.status), ` ${when(e.created_at)} · ${e.provider} ${e.model || ""} · rubric v${e.rubric_version} · ${fmt(e.suggested_marks)} marks`, e.error ? ` · ${e.error}` : ""))));
}
function fullText(ext) {
  dialog(`Full transcription (${ext.model || "Claude"})`, ext.pages.map((p) => h("div", { class: "panel" },
    h("div", { class: "row" }, h("b", {}, `Page ${p.page_no}`), p.warnings.map((w) => chip(w, "warn")), p.uncertain.length ? chip(`${p.uncertain.length} uncertain`, "bad") : null,
      h("span", { class: "muted small" }, p.confidence === null ? "confidence: not reported by Claude" : `confidence ${p.confidence}`)),
    h("div", { class: "answer-text", style: "margin-top:6px" }, p.text || "(nothing read)"))));
}
async function reassign(s) {
  const students = await api(`/api/students?class_name=${encodeURIComponent(s.student_full.class_name)}`);
  const sel = h("select", {}, students.map((st) => h("option", { value: st.id, selected: st.admission_no === s.student_full.admission_no }, `${st.name} (${st.admission_no})`)));
  const d = dialog("Assign this sheet to a student", field("Student", sel), [h("button", { class: "primary", onclick: async () => {
    await run(() => api(`/api/submissions/${s.id}/identity`, { body: { verified: true, student_id: sel.value } }), "Student updated"); d.close(); route();
  } }, "Save and confirm")]);
}
function pageOrder(s) {
  const list = h("ol", {}, s.page_list.map((p) => h("li", { "data-id": p.id }, `Page ${p.page_no} `,
    h("button", { onclick: (e) => { const li = e.target.closest("li"); if (li.previousElementSibling) li.parentNode.insertBefore(li, li.previousElementSibling); } }, "↑"),
    h("button", { onclick: (e) => { const li = e.target.closest("li"); if (li.nextElementSibling) li.parentNode.insertBefore(li.nextElementSibling, li); } }, "↓"))));
  const d = dialog("Page order", [list, h("p", { class: "muted small" }, "After reordering, read the sheet again so answers follow the new order.")],
    [h("button", { class: "primary", onclick: async () => {
      await run(() => api(`/api/submissions/${s.id}/pages/order`, { body: { page_ids: [...list.children].map((li) => li.dataset.id) } }), "Order saved"); d.close(); route();
    } }, "Save")]);
}

// ---------- students ----------
async function viewStudents() {
  const students = await api("/api/students");
  const manager = ["admin", "coordinator"].includes(state.user.role);
  const f = { admission_no: h("input", {}), name: h("input", {}), class_name: h("input", {}), section: h("input", {}), roll_no: h("input", {}) };
  const csvBox = h("textarea", { placeholder: "admission_no,name,class_name,section,roll_no\nA1001,Arun Kumar,7,A,1" });
  mount(h("div", { class: "page-head" }, h("h1", {}, "Students")),
    manager ? h("div", { class: "grid two" },
      h("div", { class: "panel" }, h("h2", {}, "Add a student"), h("div", { class: "grid two" }, field("Admission no", f.admission_no), field("Name", f.name), field("Class", f.class_name), field("Section", f.section), field("Roll no", f.roll_no)),
        h("button", { class: "primary", onclick: async () => { await run(() => api("/api/students", { body: Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.value])) }), "Student added"); route(); } }, "Add")),
      h("div", { class: "panel" }, h("h2", {}, "Import from CSV"), csvBox, h("div", { style: "height:8px" }),
        h("button", { onclick: async () => { const r = await run(() => api("/api/students/import", { method: "POST", body: csvBox.value, headers: { "Content-Type": "text/plain" } })); toast(`${r.created} added, ${r.updated} updated${r.errors.length ? ", " + r.errors.length + " errors" : ""}`); route(); } }, "Import"))) : null,
    h("div", { class: "panel table-wrap" }, h("table", {}, h("tr", {}, ["Admission no", "Name", "Class", "Section", "Roll"].map((t) => h("th", {}, t))),
      students.map((s) => h("tr", {}, h("td", {}, s.admission_no), h("td", {}, s.name), h("td", {}, s.class_name), h("td", {}, s.section), h("td", {}, s.roll_no))))));
}

// ---------- settings ----------
async function viewSettings() {
  const [{ config: c, options }, users, students] = await Promise.all([api("/api/settings/ai"), api("/api/users"), api("/api/students")]);
  const f = {};
  const text = (k, ph, type = "text") => (f[k] = h("input", { type, value: c[k] ?? "", placeholder: ph || "" }));
  const num = (k) => (f[k] = h("input", { type: "number", value: c[k] ?? "" }));
  const bool = (k, label) => { f[k] = h("input", { type: "checkbox", checked: !!c[k] }); return h("label", { class: "check" }, f[k], label); };
  const select = (k, opts) => (f[k] = h("select", {}, opts.map(([v, t]) => h("option", { value: v, selected: c[k] === v }, t))));
  const secret = (k) => (f[k] = h("input", { type: "password", autocomplete: "off", placeholder: c[k] ? `set (${c[k]}) — type to replace` : "not set" }));
  const testOut = h("div", { class: "small" });
  const test = async (target, sample) => {
    testOut.replaceChildren(h("span", { class: "muted" }, `Testing ${target}…`));
    const r = await api(`/api/settings/ai/test/${target}${sample ? "?sample=true" : ""}`, { method: "POST" }).catch((e) => ({ ok: false, detail: e.message }));
    testOut.replaceChildren(chip(r.ok ? "OK" : "FAILED", r.ok ? "ok" : "bad"), ` ${target}: ${r.detail || ""} ${r.model ? "· " + r.model : ""} ${r.duration_ms ? "· " + r.duration_ms + " ms" : ""}`,
      ...(r.sample_text ? [h("div", { class: "answer-text", style: "margin-top:6px" }, (r.sample_name ? `Name: ${r.sample_name}
` : "") + r.sample_text)] : []));
  };
  const save = async () => {
    const changes = {};
    for (const [k, el] of Object.entries(f)) {
      if (el.type === "checkbox") changes[k] = el.checked;
      else if (el.type === "password") { if (el.value) changes[k] = el.value; }
      else if (el.type === "number") changes[k] = el.value === "" ? null : Number(el.value);
      else changes[k] = el.value;
    }
    await run(() => api("/api/settings/ai", { method: "PUT", body: { changes } }), "AI Configuration saved"); route();
  };
  const ack = c.external_processing_acknowledged_by;
  const models = options.models.map((m) => [m.id, m.label]);
  const modelSel = (k) => select(k, models.some(([v]) => v === c[k]) ? models : [...models, [c[k], c[k]]]);
  mount(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings → AI Configuration"), h("div", { class: "muted" }, "Claude reads the answer sheets and marks them. The key is stored encrypted on the server and never shown again."))),
    h("div", { class: "grid two" },
      h("div", { class: "panel" }, h("h2", {}, "Claude (Anthropic)"),
        bool("claude_enabled", "Enabled"),
        field("Claude API key", secret("claude_api_key"), "Stored encrypted on the server; never sent back to the browser."),
        field("Model for reading sheets", modelSel("claude_vision_model")),
        field("Model for marking answers", modelSel("claude_eval_model")),
        h("div", { class: "grid two" }, field("Effort", select("claude_effort", options.efforts.map((e) => [e, e])), "higher = more thorough, slower, costlier"), field("Timeout (s)", num("claude_timeout"))),
        field("Retries for temporary failures", num("max_retries")),
        bool("claude_refusal_fallback", "Use Anthropic's server-side refusal fallback"),
        h("div", { class: "row" }, h("button", { onclick: () => test("marking") }, "Test marking model"), h("button", { onclick: () => test("reading") }, "Test reading model"), h("button", { onclick: () => test("reading", true) }, "Read a sample page"))),
      h("div", { class: "panel" }, h("h2", {}, "Sending student pages to Anthropic"),
        h("div", { class: "notice warn" }, "Every answer sheet is read by Claude, so photographs of students' work are sent to Anthropic. Before using it, make sure the school's consent and data-processing arrangements cover this (for children's data, parental consent)."),
        bool("external_processing_acknowledged", "I acknowledge this external processing on behalf of the school"),
        ack ? h("p", { class: "small muted" }, `Acknowledged by ${ack.user} on ${when(ack.at)}`) : null,
        field("Blank this % of the top of page 1 before sending (0 = off)", num("redact_top_percent"), "Blanking hides the name and roll number, but then the name on the sheet cannot be checked against the chosen student."))),
    h("div", { class: "panel" }, h("div", { class: "row" }, h("button", { class: "primary", onclick: save }, "Save AI Configuration"), h("div", { class: "spacer" })), h("div", { style: "margin-top:10px" }, testOut)),
    usersPanel(users, students));
}
function usersPanel(users, students) {
  const f = { username: h("input", {}), name: h("input", {}), password: h("input", { type: "password", autocomplete: "new-password" }),
    role: h("select", {}, ["teacher", "coordinator", "admin", "student"].map((r) => h("option", { value: r }, r))),
    student_id: h("select", {}, h("option", { value: "" }, "(for student accounts)"), students.map((s) => h("option", { value: s.id }, `${s.name} (${s.admission_no})`))) };
  return h("div", { class: "panel table-wrap" }, h("h2", {}, "Users"),
    h("table", {}, h("tr", {}, ["Name", "Username", "Role", "Active", ""].map((t) => h("th", {}, t))),
      users.map((u) => h("tr", {}, h("td", {}, u.name), h("td", {}, u.username), h("td", {}, u.role), h("td", {}, u.active ? chip("active", "ok") : chip("disabled", "bad")),
        h("td", {}, u.id === state.user.id ? "" : h("button", { onclick: async () => { await run(() => api(`/api/users/${u.id}`, { method: "PATCH", body: { active: !u.active } }), "Updated"); route(); } }, u.active ? "Disable" : "Enable"),
          " ", h("button", { onclick: async () => { const p = prompt(`New password for ${u.username} (8+ characters)`); if (!p) return; await run(() => api(`/api/users/${u.id}`, { method: "PATCH", body: { password: p } }), "Password reset"); } }, "Reset password"))))),
    h("h3", { style: "margin-top:16px" }, "Add a user"),
    h("div", { class: "grid four" }, field("Full name", f.name), field("Username", f.username), field("Role", f.role), field("Initial password", f.password)),
    field("Linked student (student accounts only)", f.student_id),
    h("button", { class: "primary", onclick: async () => {
      await run(() => api("/api/users", { body: { username: f.username.value, name: f.name.value, role: f.role.value, password: f.password.value, student_id: f.student_id.value || null } }), "User added"); route();
    } }, "Add user"));
}

// ---------- audit & student results ----------
async function viewAudit() {
  const rows = await api("/api/audit");
  mount(h("div", { class: "page-head" }, h("h1", {}, "Audit log")), h("div", { class: "panel table-wrap" }, h("table", {},
    h("tr", {}, ["When", "User", "Action", "Entity", "Details"].map((t) => h("th", {}, t))),
    rows.map((r) => h("tr", {}, h("td", { class: "small" }, when(r.at)), h("td", {}, r.user || ""), h("td", {}, r.action), h("td", { class: "small" }, `${r.entity} ${r.entity_id ? r.entity_id.slice(0, 8) : ""}`),
      h("td", { class: "small muted" }, Object.entries(r.details || {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ")))))));
}
async function viewMyResults() {
  const res = await api("/api/my/results");
  mount(h("div", { class: "page-head" }, h("h1", {}, "My results")),
    res.length ? res.map((r) => h("div", { class: "panel" }, h("div", { class: "row" }, h("h2", { style: "margin:0" }, r.exam), h("span", { class: "muted" }, `${r.subject} · ${r.academic_year}`), h("div", { class: "spacer" }), h("b", { style: "font-size:18px" }, `${fmt(r.total)} / ${fmt(r.maximum)}`)),
      h("table", { style: "margin-top:8px" }, h("tr", {}, ["Question", "Marks", "Feedback"].map((t) => h("th", {}, t))),
        r.questions.map((q) => h("tr", {}, h("td", {}, `Q${q.number}`), h("td", { class: "num" }, `${fmt(q.marks)} / ${fmt(q.max_marks)}`), h("td", {}, q.feedback || "")))))) : h("div", { class: "panel muted" }, "No published results yet."));
}

// ---------- router ----------
async function route(hash, focus) {
  if (hash && hash !== location.hash) { location.hash = hash; return; }
  if (!state.token) { shell(); return viewLogin(); }
  if (!state.user) {
    try { state.user = await api("/api/auth/me"); } catch { return; }
  }
  shell();
  const parts = (location.hash || "#/").slice(2).split("/");
  try {
    if (state.user.role === "student") return viewMyResults();
    switch (parts[0]) {
      case "exam": return await viewExam(parts[1], parts[2]);
      case "sheet": return await viewSheet(parts[1], focus);
      case "students": return await viewStudents();
      case "settings": return await viewSettings();
      case "audit": return await viewAudit();
      case "login": location.hash = "#/exams"; return;
      default: return await viewExams();
    }
  } catch (e) { mount(h("div", { class: "panel" }, h("h2", {}, "Something went wrong"), h("p", {}, e.message), h("a", { href: "#/exams" }, "Back to examinations"))); }
}
window.addEventListener("hashchange", () => route());
route();
