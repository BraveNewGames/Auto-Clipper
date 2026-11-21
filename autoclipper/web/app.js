/* Auto-Clipper UI — vanilla JS, no build step, works offline. */
"use strict";

// ---------------------------------------------------------------- utilities
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked") el.checked = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function fmt(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "—";
  const sign = sec < 0 ? "-" : "";
  let ms = Math.round(Math.abs(sec) * 1000);
  const hh = Math.floor(ms / 3600000); ms -= hh * 3600000;
  const mm = Math.floor(ms / 60000); ms -= mm * 60000;
  const ss = Math.floor(ms / 1000); ms -= ss * 1000;
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${sign}${p(hh)}:${p(mm)}:${p(ss)}.${p(ms, 3)}`;
}
function fmtShort(sec) {
  if (sec === null || sec === undefined) return "?";
  const s = Math.round(sec), m = Math.floor(Math.abs(s) / 60), r = Math.abs(s) % 60;
  return `${s < 0 ? "-" : ""}${m}:${String(r).padStart(2, "0")}`;
}
function fmtDur(sec) {
  if (sec === null || sec === undefined) return "?";
  return sec >= 60 ? `${Math.floor(sec / 60)}m${String(Math.round(sec % 60)).padStart(2, "0")}s` : `${sec.toFixed(1)}s`;
}
function appendAll(el, ...children) {
  for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) el.append(c);
  return el;
}
function basename(p) { return p ? String(p).split(/[\\/]/).pop() : ""; }
function dirname(p) { const s = String(p || ""); const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\")); return i > 0 ? s.slice(0, i) : s; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function toast(msg, kind = "") {
  const el = h("div", { class: "toast " + kind }, msg);
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 3500);
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const ct = res.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

// Wrap an async action: disables the button while running, toasts errors.
function action(fn) {
  return async (ev) => {
    const btn = ev && ev.currentTarget && ev.currentTarget.tagName === "BUTTON" ? ev.currentTarget : null;
    if (btn) btn.disabled = true;
    try { await fn(ev); } catch (e) { toast(e.message, "error"); console.error(e); }
    finally { if (btn) btn.disabled = false; }
  };
}

// ---------------------------------------------------------------- state
const state = {
  tab: "session",
  session: null, coverage: null, reviewers: [],
  pendingCreate: null,
  alignSuggestions: null,
  transcriptQ: "",
  importWarnings: null,
  guide: null,
  filters: { q: "", reviewer: "", status: "", pov: "", event: "", priority: "", kind: "" },
  focusIdx: -1,
  exportResult: null, exportInfo: null, exportMode: "exclude_rejected",
  resolve: null,
};

function applyPayload(p) {
  if (p && p.session) {
    if (!state.session || state.session.folder !== p.session.folder) {
      state.exportResult = null; state.alignSuggestions = null; state.transcriptQ = "";
      state.resolve = null;
    }
    state.exportInfo = null;
    state.session = p.session; state.coverage = p.coverage; state.reviewers = p.reviewers || [];
    state.guide = null; // invalidate
  }
  updateChrome();
}

function updateChrome() {
  const s = state.session;
  $$("#tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === state.tab);
    b.disabled = !s && b.dataset.tab !== "session";
  });
  $("#session-badge").textContent = s ? `${s.session_id || "(unnamed)"} — ${s.folder}` : "No session open";
  $("#session-badge").title = s ? s.folder : "";
}

function setTab(tab) {
  if (!state.session && tab !== "session") return;
  state.tab = tab;
  location.hash = tab;
  updateChrome();
  render();
}

function render() {
  const main = $("#main");
  main.innerHTML = "";
  const fn = { session: renderSession, recordings: renderRecordings, transcript: renderTranscript,
    reviews: renderReviews, guide: renderGuide, export: renderExport }[state.tab];
  fn(main);
}

function povNameOf(partId) {
  for (const pov of state.session?.povs || []) for (const p of pov.parts) if (p.part_id === partId) return pov.name;
  return "?";
}

// ---------------------------------------------------------------- 1. Session
function renderSession(main) {
  const pathInput = h("input", { class: "grow", placeholder: "F:\\Recordings\\NB-2026-11-16", value: state.session?.folder || "" });
  const doOpen = action(async () => {
    const folder = pathInput.value.trim();
    if (!folder) throw new Error("Enter or pick a session folder");
    const res = await api("/api/session/open", { folder });
    if (res.needs_create) {
      state.pendingCreate = res;
      toast("No Auto-Clipper session in this folder yet — scan it to create one.");
      render();
      return;
    }
    state.pendingCreate = null;
    applyPayload(res);
    toast("Session opened", "ok");
    render();
  });
  pathInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doOpen(e); });

  const openPanel = h("div", { class: "panel" },
    h("h2", null, "Open or create a session"),
    h("div", { class: "row" },
      pathInput,
      h("button", { class: "btn", onclick: action(async () => {
        const r = await api("/api/pick-folder", {});
        if (r.folder) { pathInput.value = r.folder; doOpen(); }
      }) }, "Browse…"),
      h("button", { class: "btn primary", onclick: doOpen }, "Open")),
    h("div", { class: "muted small", style: { marginTop: "6px" } },
      "Pick the folder that holds every POV's video files and their Whisper SRTs. Original files are never modified; " +
      "Auto-Clipper writes only to .autoclipper\\ and autoclipper_exports\\ inside it."));
  main.appendChild(openPanel);

  if (state.pendingCreate) {
    const pc = state.pendingCreate;
    const idIn = h("input", { value: pc.suggested_id || "" });
    const guessPrefix = (pc.suggested_id || "").split(/[-_ ]/)[0].toUpperCase().slice(0, 6) || "EV";
    const prefixIn = h("input", { value: guessPrefix, style: { width: "90px" } });
    openPanel.appendChild(h("div", { class: "panel", style: { marginTop: "10px", background: "var(--panel2)" } },
      h("h3", { style: { marginTop: 0 } }, "Create session from ", h("span", { class: "mono" }, pc.folder)),
      h("div", { class: "row" },
        h("label", { class: "f" }, "Session ID", idIn),
        h("label", { class: "f" }, "Event prefix", prefixIn),
        h("button", { class: "btn primary", style: { alignSelf: "flex-end" }, onclick: action(async () => {
          const res = await api("/api/session/create", { folder: pc.folder, session_id: idIn.value.trim(), event_prefix: prefixIn.value.trim() });
          state.pendingCreate = null; applyPayload(res);
          toast("Session created — check recordings & alignment next", "ok");
          setTab("recordings");
        }) }, "Scan folder & create"))));
  }

  const recentBox = h("ul", { class: "list-recent" }, h("li", { class: "muted" }, "Loading…"));
  main.appendChild(h("div", { class: "panel" }, h("h2", null, "Recent sessions"), recentBox));
  api("/api/recent").then((r) => {
    recentBox.innerHTML = "";
    if (!r.recent.length) recentBox.appendChild(h("li", { class: "muted" }, "None yet."));
    for (const it of r.recent) {
      const folder = it.folder || it.path || "";
      recentBox.appendChild(h("li", null,
        h("a", { onclick: () => { pathInput.value = folder; doOpen(); } }, it.session_id || basename(folder)),
        " ", h("span", { class: "muted mono small" }, folder),
        it.opened_at || it.updated_at ? h("span", { class: "muted small" }, "  · last used " + new Date(it.opened_at || it.updated_at).toLocaleString()) : null));
    }
  }).catch(() => { recentBox.innerHTML = ""; recentBox.appendChild(h("li", { class: "muted" }, "Unavailable.")); });

  const s = state.session;
  if (!s) return;
  const f = {
    session_id: h("input", { value: s.session_id }), event_prefix: h("input", { value: s.event_prefix, style: { width: "90px" } }),
    game: h("input", { value: s.game || "" }), pad_before: h("input", { class: "num", value: s.pad_before, type: "number", step: "0.5", min: "0" }),
    pad_after: h("input", { class: "num", value: s.pad_after, type: "number", step: "0.5", min: "0" }),
    notes: h("textarea", { rows: 3 }, s.notes || ""),
    game_context: h("textarea", { rows: 3 }, s.game_context || ""),
  };
  const nParts = s.povs.reduce((a, p) => a + p.parts.length, 0);
  main.appendChild(h("div", { class: "panel" },
    h("h2", null, "Session details"),
    h("div", { class: "row" },
      h("label", { class: "f" }, "Session ID", f.session_id),
      h("label", { class: "f" }, "Event prefix", f.event_prefix),
      h("label", { class: "f grow" }, "Game / client", f.game),
      h("label", { class: "f" }, "Pad before (s)", f.pad_before),
      h("label", { class: "f" }, "Pad after (s)", f.pad_after)),
    h("div", { class: "row" }, h("label", { class: "f grow" }, "Notes", f.notes)),
    h("div", { class: "row" }, h("label", { class: "f grow" },
      "Game & editorial context for AI reviewers (included in every review packet; empty = default)", f.game_context)),
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: action(async () => {
        const body = {}; for (const [k, el] of Object.entries(f)) body[k] = el.value;
        applyPayload(await api("/api/session/meta", body)); toast("Saved", "ok"); render();
      }) }, "Save details"),
      h("button", { class: "btn", onclick: action(() => api("/api/open-path", { path: s.folder })) }, "Open folder"),
      h("span", { class: "muted" }, `${s.povs.length} POVs · ${nParts} parts · ${s.suggestions.length} AI suggestions · ${s.events.length} events`)),
    h("div", { class: "muted small", style: { marginTop: "6px" } },
      "Padding is added before/after each suggested range in the LosslessCut imports so you can adjust the cut visually. " +
      "Event IDs look like ", h("span", { class: "mono" }, `${s.event_prefix || "NB"}-003`), ".")));
}

// ---------------------------------------------------------------- 2. Recordings & Alignment
function partEnd(p) {
  const d = p.duration ?? p.duration_estimate;
  return d == null ? null : p.offset + d;
}

const SEAM_TOL = 0.5; // gaps/overlaps this small between consecutive parts are alignment noise

function analyzeCoverage(s) {
  // Per POV: gaps between consecutive parts and overlaps, computed from offset/duration.
  const out = {};
  for (const pov of s.povs) {
    const parts = pov.parts.slice().sort((a, b) => a.offset - b.offset);
    const gaps = [], overlaps = [], unknown = [], seams = [];
    for (const p of parts) if (p.duration == null) unknown.push(p);
    for (let i = 1; i < parts.length; i++) {
      const prevEnd = partEnd(parts[i - 1]);
      if (prevEnd == null) continue;
      const st = parts[i].offset;
      if (Math.abs(st - prevEnd) <= SEAM_TOL) { if (Math.abs(st - prevEnd) > 0.001) seams.push([prevEnd, st, parts[i - 1], parts[i]]); }
      else if (st > prevEnd) gaps.push([prevEnd, st, parts[i - 1], parts[i]]);
      else overlaps.push([st, Math.min(prevEnd, partEnd(parts[i]) ?? prevEnd), parts[i - 1], parts[i]]);
    }
    out[pov.pov_id] = { gaps, overlaps, unknown, seams };
  }
  return out;
}

function renderTimeline(s) {
  let lo = Infinity, hi = -Infinity;
  for (const pov of s.povs) for (const p of pov.parts) {
    lo = Math.min(lo, p.offset); hi = Math.max(hi, partEnd(p) ?? p.offset + 1);
  }
  if (!isFinite(lo)) return h("div", { class: "muted" }, "No parts.");
  lo = Math.min(lo, 0);
  const span = Math.max(hi - lo, 1);
  const pct = (t) => ((t - lo) / span) * 100;
  const cov = analyzeCoverage(s);
  const wrap = h("div", { class: "timeline" });
  // axis
  const axis = h("div", { class: "tl-axis" });
  const steps = [30, 60, 120, 300, 600, 900, 1800, 3600];
  const step = steps.find((x) => span / x <= 12) || 3600;
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step)
    axis.appendChild(h("div", { class: "tl-tick", style: { left: pct(t) + "%" } }, fmtShort(t)));
  wrap.appendChild(axis);
  for (const pov of s.povs) {
    const track = h("div", { class: "tl-track" });
    for (const g of cov[pov.pov_id].gaps)
      track.appendChild(h("div", { class: "tl-gap", style: { left: pct(g[0]) + "%", width: (pct(g[1]) - pct(g[0])) + "%" },
        title: `Gap ${fmt(g[0])} – ${fmt(g[1])} (${fmtDur(g[1] - g[0])}): no ${pov.name} footage` }));
    for (const p of pov.parts) {
      const end = partEnd(p);
      const unknown = p.duration == null;
      const w = end == null ? 0.8 : Math.max(pct(end) - pct(p.offset), 0.4);
      const cls = "tl-bar" + (unknown ? " unknown" : "") + (p.offset_source === "default" && p.index > 1 ? " unaligned" : "");
      track.appendChild(h("div", { class: cls, style: { left: pct(p.offset) + "%", width: w + "%" },
        title: `${pov.name} part ${p.index}: ${fmt(p.offset)} → ${end == null ? "?" : fmt(end)}` +
          (unknown ? `\nDuration unknown${p.duration_estimate ? " (SRT estimate " + fmtDur(p.duration_estimate) + ")" : ""}` : "") +
          `\nOffset source: ${p.offset_source}` },
        `P${p.index}${unknown ? " ?" : ""}`));
    }
    for (const o of cov[pov.pov_id].overlaps)
      track.appendChild(h("div", { class: "tl-overlap", style: { left: pct(o[0]) + "%", width: Math.max(pct(o[1]) - pct(o[0]), 0.3) + "%" },
        title: `Overlap ${fmt(o[0])} – ${fmt(o[1])}` }));
    wrap.appendChild(h("div", { class: "tl-row" }, h("div", { class: "tl-label", title: pov.name }, pov.name), track));
  }
  wrap.appendChild(h("div", { class: "legend" },
    h("span", null, h("i", { style: { background: "var(--accent2)" } }), "part coverage"),
    h("span", null, h("i", { style: { background: "rgba(239,93,93,.35)" } }), "gap (no footage)"),
    h("span", null, h("i", { style: { background: "var(--warn)" } }), "overlap"),
    h("span", null, h("i", { style: { background: "#5a4a20" } }), "unknown duration (SRT estimate)"),
    h("span", null, h("i", { style: { borderStyle: "dashed", borderColor: "var(--accent)" } }), "offset never set")));
  return wrap;
}

function renderRecordings(main) {
  const s = state.session;
  const povNames = s.povs.map((p) => p.name);
  const dl = h("datalist", { id: "pov-names" }, povNames.map((n) => h("option", { value: n })));

  const update = (part_id, field) => action(async (ev) => {
    const el = ev.target;
    const body = { part_id }; body[field] = el.value;
    if (field === "duration" && !el.value.trim()) body.duration = null;
    applyPayload(await api("/api/parts/update", body));
    render();
  });

  const refSel = h("select", { title: "Reference part: keeps its current offset; every other part is placed relative to it" },
    h("option", { value: "" }, "Reference: first part"),
    s.povs.flatMap((pov) => pov.parts.map((p) => h("option", { value: p.part_id }, `Reference: ${pov.name} part ${p.index} (${fmtShort(p.offset)})`))));
  refSel.value = state.alignRef || "";
  main.appendChild(h("div", { class: "panel" },
    h("div", { class: "row" },
      h("h2", { style: { margin: 0 } }, "Session timeline"),
      h("span", { class: "grow" }),
      h("button", { class: "btn", title: "Within each POV, start part N+1 where part N ends (manual offsets are kept)",
        onclick: action(async () => { applyPayload(await api("/api/align/chain", {})); toast("Parts chained by duration", "ok"); render(); }) },
        "Chain parts by duration"),
      refSel,
      h("button", { class: "btn primary", title: "Match Team/named lines across recordings to estimate offsets",
        onclick: action(async () => {
          state.alignRef = refSel.value;
          const r = await api("/api/align/suggest", { reference_part_id: refSel.value || null });
          state.alignSuggestions = r.suggestions;
          if (!r.suggestions.length) toast("No transcript-based offset suggestions found");
          render();
        }) }, "Suggest offsets from transcripts")),
    renderTimeline(s),
    renderCoverageNotes(s)));

  if (state.alignSuggestions) main.appendChild(renderAlignSuggestions());

  const panel = h("div", { class: "panel" }, dl,
    h("h2", null, "Recordings"),
    h("div", { class: "muted small" }, "Offset = session time at which the part's local 00:00:00 occurs. Edit values inline (HH:MM:SS.mmm or seconds); changes save immediately. ",
      "Type a different POV name to move a part (a new name creates a new POV)."));
  for (const pov of s.povs) {
    panel.appendChild(h("div", { class: "pov-head" },
      h("h3", null, pov.name), h("span", { class: "muted" }, `${pov.parts.length} part(s)`),
      pov.parts.length > 1 ? h("button", { class: "btn small", onclick: action(async () => {
        applyPayload(await api("/api/align/chain", { pov_id: pov.pov_id })); render();
      }) }, "Chain this POV") : null));
    const tbody = h("tbody");
    for (const p of pov.parts) {
      const end = partEnd(p);
      tbody.appendChild(h("tr", null,
        h("td", null, h("input", { class: "num", type: "number", min: "1", value: p.index, onchange: update(p.part_id, "index") })),
        h("td", null, h("input", { list: "pov-names", value: pov.name, style: { width: "110px" }, onchange: update(p.part_id, "pov_name") })),
        h("td", { class: "mono small" }, h("div", { title: p.video_path || "" }, p.video_path ? basename(p.video_path) : h("span", { class: "warn-text" }, "no video")),
          h("div", { class: "muted", title: p.srt_path || "" }, p.srt_path ? basename(p.srt_path) : h("span", { class: "warn-text" }, "no SRT"))),
        h("td", null, h("input", { class: "time", value: p.duration == null ? "" : fmt(p.duration), placeholder: "unknown", onchange: update(p.part_id, "duration") }),
          h("div", { class: "src " + (p.duration == null ? "default" : "") },
            p.duration == null ? `unknown${p.duration_estimate ? " · SRT ≈ " + fmtDur(p.duration_estimate) : ""}` : (p.duration_source || ""))),
        h("td", null, h("input", { class: "time", value: fmt(p.offset), onchange: update(p.part_id, "offset") }),
          h("div", { class: "src " + p.offset_source }, p.offset_source +
            (p.offset_confidence != null ? ` · ${Math.round(p.offset_confidence * 100)}%` : ""))),
        h("td", { class: "mono small" }, `${fmt(p.offset)} → ${end == null ? "?" : fmt(end)}`),
        h("td", null, h("button", { class: "btn small", title: "Open the folder with this file",
          onclick: action(() => api("/api/open-path", { path: dirname(p.video_path || p.srt_path) || "." })) }, "Folder"))));
    }
    panel.appendChild(h("table", { class: "t parts" },
      h("thead", null, h("tr", null, ["Part", "POV", "Files", "Duration", "Offset", "Session range", ""].map((x) => h("th", null, x)))), tbody));
  }
  main.appendChild(panel);
}

function renderCoverageNotes(s) {
  const cov = analyzeCoverage(s);
  const items = [];
  for (const pov of s.povs) {
    const c = cov[pov.pov_id];
    for (const g of c.gaps) items.push(h("li", null, h("span", { class: "badge bad" }, "gap"), ` ${pov.name}: no footage ${fmt(g[0])} – ${fmt(g[1])} (${fmtDur(g[1] - g[0])}) between part ${g[2].index} and part ${g[3].index}`));
    for (const o of c.overlaps) items.push(h("li", null, h("span", { class: "badge warn" }, "overlap"), ` ${pov.name}: part ${o[2].index} and part ${o[3].index} overlap ${fmt(o[0])} – ${fmt(o[1])}`));
    for (const sm of c.seams) items.push(h("li", { class: "muted small" }, `${pov.name}: part ${sm[2].index} → part ${sm[3].index} join is ${sm[1] > sm[0] ? "a gap" : "an overlap"} of ${Math.abs(sm[1] - sm[0]).toFixed(2)}s (likely estimate noise; set offsets manually or chain if the files are back-to-back)`));
    for (const p of c.unknown) items.push(h("li", null, h("span", { class: "badge warn" }, "duration?"), ` ${pov.name} part ${p.index}: video duration unknown — enter it manually for exact cuts`));
    for (const p of pov.parts) if (p.index > 1 && p.offset_source === "default")
      items.push(h("li", null, h("span", { class: "badge warn" }, "unaligned"), ` ${pov.name} part ${p.index}: offset never set — chain parts or suggest offsets`));
  }
  if (state.coverage && state.coverage.error) items.push(h("li", { class: "muted" }, "coverage report: " + state.coverage.error));
  if (!items.length) return h("div", { class: "good-text small", style: { marginTop: "6px" } }, "No gaps, overlaps or unknown durations detected.");
  return h("ul", { style: { margin: "8px 0 0", paddingLeft: "18px" } }, items);
}

function renderAlignSuggestions() {
  const sugg = state.alignSuggestions;
  const checks = [];
  const rows = sugg.map((x) => {
    const usable = x.offset != null && x.method !== "reference";
    const cb = h("input", { type: "checkbox", disabled: !usable,
      checked: usable && (x.confidence ?? 0) >= 0.5 && x.current_source !== "manual" && Math.abs(x.delta ?? 1) > 0.001 });
    checks.push([cb, x]);
    const delta = x.current_offset != null && x.offset != null ? x.offset - x.current_offset : null;
    const conf = x.confidence == null ? null : Math.round(x.confidence * 100);
    return h("tr", null, h("td", null, cb), h("td", null, x.label || x.part_id),
      h("td", { class: "mono" }, fmt(x.current_offset), " ", h("span", { class: "src " + (x.current_source || "") }, x.current_source || "")),
      h("td", { class: "mono" }, x.method === "reference" ? "reference (kept)" : x.offset == null ? h("span", { class: "muted" }, "no reliable match") : fmt(x.offset)),
      h("td", { class: "mono " + (delta && Math.abs(delta) > 1 ? "warn-text" : "muted") }, delta == null ? "" : (delta >= 0 ? "+" : "") + delta.toFixed(2) + "s"),
      h("td", null, conf == null ? "—" : h("span", { class: "badge " + (conf >= 70 ? "good" : conf >= 40 ? "warn" : "bad") }, conf + "%")),
      h("td", null, x.support ?? ""), h("td", null, x.matched_label || x.matched_with || ""), h("td", { class: "muted small" }, x.method || ""));
  });
  return h("div", { class: "panel" },
    h("div", { class: "row" }, h("h2", { style: { margin: 0 } }, "Suggested offsets"), h("span", { class: "grow" }),
      h("button", { class: "btn", onclick: () => { state.alignSuggestions = null; render(); } }, "Dismiss"),
      h("button", { class: "btn primary", onclick: action(async () => {
        const chosen = checks.filter(([cb]) => cb.checked).map(([, x]) => x);
        if (!chosen.length) throw new Error("Select at least one suggestion");
        const r = await api("/api/align/apply", { suggestions: chosen });
        applyPayload(r); state.alignSuggestions = null; toast(`Applied ${r.applied} offset(s)`, "ok"); render();
      }) }, "Apply selected")),
    h("div", { class: "muted small" }, "Nothing is applied until you click Apply. Low-confidence suggestions are unchecked; manual offsets are unchecked by default."),
    h("table", { class: "t", style: { marginTop: "6px" } },
      h("thead", null, h("tr", null, ["", "Part", "Current", "Suggested", "Change", "Confidence", "Matches", "Matched with", "Method"].map((x) => h("th", null, x)))),
      h("tbody", null, rows)));
}

// ---------------------------------------------------------------- 3. Transcript
function renderTranscript(main) {
  const q = h("input", { type: "search", placeholder: "Search text or speaker…", value: state.transcriptQ, class: "grow" });
  const info = h("span", { class: "muted" });
  const list = h("div", null, h("div", { class: "empty" }, "Building session transcript…"));
  const panel = h("div", { class: "panel" },
    h("div", { class: "row" }, h("h2", { style: { margin: 0 } }, "Session transcript"), q, info),
    h("div", { class: "muted small", style: { margin: "4px 0 8px" } },
      "One shared conversation on the session clock. Each speaker's own named mic is preferred; Team lines are only attributed when matched to a named line elsewhere. Expand a line to see every source."),
    list);
  main.appendChild(panel);
  const load = async () => {
    try {
      const r = await api("/api/transcript?q=" + encodeURIComponent(state.transcriptQ));
      info.textContent = state.transcriptQ ? `${r.lines.length} of ${r.total} lines` : `${r.total} lines`;
      list.innerHTML = "";
      if (!r.lines.length) list.appendChild(h("div", { class: "empty" }, "No lines."));
      const frag = document.createDocumentFragment();
      for (const ln of r.lines) frag.appendChild(transcriptLine(ln));
      list.appendChild(frag);
    } catch (e) { list.innerHTML = ""; list.appendChild(h("div", { class: "empty bad-text" }, e.message)); }
  };
  q.addEventListener("input", debounce(() => { state.transcriptQ = q.value; load(); }, 250));
  load();
}

function transcriptLine(ln) {
  const who = ln.speaker
    ? h("span", { class: "badge named" }, ln.speaker)
    : h("span", { class: "badge team" }, ln.channel === "team" ? "Team (unattributed)" : "Unknown speaker");
  const srcs = ln.sources || [];
  return h("div", { class: "tr-line" + (ln.uncertain ? " uncertain" : "") },
    h("div", { class: "tr-time" }, fmt(ln.session_start)),
    h("div", { class: "tr-who" }, who,
      h("div", { class: "row", style: { gap: "3px" } },
        h("span", { class: "badge " + (ln.channel || "") }, ln.channel || "?"),
        ln.confidence ? h("span", { class: "badge " + (ln.confidence === "high" ? "good" : ln.confidence === "low" ? "bad" : "warn") }, ln.confidence) : null,
        ln.uncertain ? h("span", { class: "badge warn", title: ln.note || "" }, "uncertain") : null)),
    h("div", { class: "tr-text" }, ln.text,
      ln.note ? h("div", { class: "muted small" }, ln.note) : null,
      srcs.length ? h("details", null, h("summary", null, `${srcs.length} source${srcs.length > 1 ? "s" : ""}`),
        h("table", { class: "t tr-sources" },
          h("thead", null, h("tr", null, ["POV", "Part", "SRT time", "Channel", "Label", "Raw text", "Sim"].map((x) => h("th", null, x)))),
          h("tbody", null, srcs.map((src) => {
            const part = findPart(src.part_id);
            return h("tr", null, h("td", null, src.pov_name || povNameOf(src.part_id)),
              h("td", { class: "mono" }, part ? `part ${part.index}` : src.part_id),
              h("td", { class: "mono" }, `${fmt(src.local_start)} → ${fmt(src.local_end)}`),
              h("td", null, h("span", { class: "badge " + (src.channel || "") }, src.channel === "named" ? "Named speaker" : src.channel === "team" ? "Team" : (src.channel || "?"))),
              h("td", null, src.label || ""), h("td", null, src.text),
              h("td", { class: "muted" }, src.similarity == null ? "" : Number(src.similarity).toFixed(2)));
          })))) : null));
}

function findPart(partId) {
  for (const pov of state.session?.povs || []) for (const p of pov.parts) if (p.part_id === partId) return p;
  return null;
}

// ---------------------------------------------------------------- 4. AI Reviews
function renderReviews(main) {
  const s = state.session;
  const rname = h("input", { value: "Claude", style: { width: "140px" } });
  const copyBtn = h("button", { class: "btn", onclick: action(async () => {
    const text = await api("/api/review-packet?reviewer=" + encodeURIComponent(rname.value.trim() || "Claude"));
    await navigator.clipboard.writeText(text);
    toast("Review packet copied — paste it into a new chat", "ok");
  }) }, "Copy to clipboard");
  main.appendChild(h("div", { class: "panel" },
    h("h2", null, "1. Export a review packet"),
    h("div", { class: "muted small" }, "The packet contains the instructions, the JSON answer format and the reconciled session transcript. ",
      "Paste it into a fresh ChatGPT or Claude chat. Run each reviewer independently — don't show one reviewer the other's answer."),
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("label", { class: "f" }, "Reviewer", rname),
      h("button", { class: "btn small", style: { alignSelf: "flex-end" }, onclick: () => { rname.value = "Claude"; } }, "Claude"),
      h("button", { class: "btn small", style: { alignSelf: "flex-end" }, onclick: () => { rname.value = "ChatGPT"; } }, "ChatGPT"),
      h("button", { class: "btn primary", style: { alignSelf: "flex-end" }, onclick: () => {
        const a = h("a", { href: "/api/review-packet?reviewer=" + encodeURIComponent(rname.value.trim() || "Claude"), download: "" });
        document.body.appendChild(a); a.click(); a.remove();
      } }, "Download review packet"),
      h("span", { style: { alignSelf: "flex-end" } }, copyBtn))));

  const iname = h("input", { placeholder: "e.g. Claude", style: { width: "160px" } });
  const itext = h("textarea", { rows: 10, placeholder: "Paste the reviewer's JSON answer here (code fences are fine)…", class: "mono" });
  const ifile = h("input", { type: "file", accept: ".json,.txt,application/json" });
  ifile.addEventListener("change", async () => {
    const f = ifile.files[0]; if (!f) return;
    itext.value = await f.text();
    try { const j = JSON.parse(itext.value); if (j.reviewer && !iname.value) iname.value = j.reviewer; } catch (_) { /* ignore */ }
  });
  main.appendChild(h("div", { class: "panel" },
    h("h2", null, "2. Import a reviewer's answer"),
    h("div", { class: "row" }, h("label", { class: "f" }, "Reviewer name", iname), h("label", { class: "f" }, "…or choose a .json file", ifile)),
    h("div", { class: "row" }, itext),
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: action(async () => {
        let name = iname.value.trim();
        if (!name) { try { name = JSON.parse(itext.value).reviewer || ""; } catch (_) { /* ignore */ } }
        if (!name) throw new Error("Enter the reviewer name");
        const r = await api("/api/reviews/import", { reviewer_name: name, text: itext.value });
        applyPayload(r);
        state.importWarnings = { reviewer: name, items: r.warnings || [] };
        toast(`Imported ${r.imported} suggestion(s) from ${name}` +
          ((r.warnings || []).length ? ` — ${r.warnings.length} warning(s)` : ""), (r.warnings || []).length ? "warn" : "ok");
        render();
      }) }, "Import"),
      h("span", { class: "muted small" }, "Importing the same reviewer again adds to its suggestions; delete the reviewer first to replace them.")),
    state.importWarnings && state.importWarnings.items.length ? h("div", { class: "import-warnings" },
      h("b", { class: "warn-text" }, `Last import (${state.importWarnings.reviewer}): ${state.importWarnings.items.length} warning(s)`),
      h("ul", null, state.importWarnings.items.map((w) => h("li", null, w))),
      h("button", { class: "btn small", onclick: () => { state.importWarnings = null; render(); } }, "Dismiss")) : null));

  const revs = state.reviewers;
  main.appendChild(h("div", { class: "panel" },
    h("h2", null, "Imported reviewers"),
    revs.length ? h("table", { class: "t" },
      h("thead", null, h("tr", null, ["Reviewer", "Suggestions", "Keep", "Maybe", "Reject", "Unreviewed", "Imported", ""].map((x) => h("th", null, x)))),
      h("tbody", null, revs.map((r) => h("tr", null,
        h("td", null, h("span", { class: "badge rev" }, r.reviewer_name), " ", h("span", { class: "muted mono small" }, r.reviewer)),
        h("td", null, r.count), h("td", { class: "good-text" }, r.keep), h("td", { style: { color: "var(--maybe)" } }, r.maybe),
        h("td", { class: "bad-text" }, r.reject), h("td", { class: "muted" }, r.unreviewed), h("td", { class: "muted small" }, r.imported_at || ""),
        h("td", null, h("button", { class: "btn small danger", onclick: action(async () => {
          if (!confirm(`Delete all ${r.count} suggestions from ${r.reviewer_name} (and their review decisions)?`)) return;
          applyPayload(await api("/api/reviews/delete", { reviewer: r.reviewer })); toast("Reviewer deleted", "ok"); render();
        }) }, "Delete"))))))
      : h("div", { class: "muted" }, "No reviews imported yet."),
    s.events.length ? h("div", { class: "muted small", style: { marginTop: "6px" } }, `${s.events.length} events, ${s.events.filter((e) => new Set(e.suggestion_ids.map(reviewerOfSid)).size > 1).length} found by more than one reviewer.`) : null));
}

function reviewerOfSid(sid) {
  const sg = state.session?.suggestions.find((x) => x.suggestion_id === sid);
  return sg ? sg.reviewer : sid;
}

// ---------------------------------------------------------------- 5. Guide
async function loadGuide() {
  state.guide = await api("/api/guide");
  return state.guide;
}

function renderGuide(main) {
  if (!state.guide) {
    main.appendChild(h("div", { class: "empty" }, "Loading guide…"));
    loadGuide().then(() => { if (state.tab === "guide") render(); }).catch((e) => toast(e.message, "error"));
    return;
  }
  const g = state.guide;
  if (!g.suggestions.length) {
    main.appendChild(h("div", { class: "panel empty" }, "No AI suggestions yet. Import reviews in the AI Reviews tab."));
    return;
  }
  const F = state.filters;
  const sel = (key, label, options) => {
    const el = h("select", { onchange: () => { F[key] = el.value; state.focusIdx = -1; renderGuideList(); } },
      h("option", { value: "" }, label), options.map((o) => Array.isArray(o) ? h("option", { value: o[0] }, o[1]) : h("option", { value: o }, o)));
    el.value = F[key];
    return el;
  };
  const search = h("input", { type: "search", placeholder: "Search titles, text, quotes…", value: F.q });
  search.addEventListener("input", debounce(() => { F.q = search.value; state.focusIdx = -1; renderGuideList(); }, 200));
  const count = h("span", { class: "muted", id: "guide-count" });
  main.appendChild(h("div", { class: "filters" }, search,
    sel("reviewer", "All reviewers", g.reviewers.map((r) => [r.reviewer, r.reviewer_name])),
    sel("status", "Any status", [["unreviewed", "Unreviewed"], ["keep", "Keep"], ["maybe", "Maybe"], ["reject", "Reject"]]),
    sel("pov", "Any POV", g.povs),
    sel("event", "All events", g.events.map((e) => [e.event_id, `${e.event_id} ${e.title || ""}`])),
    sel("priority", "Any priority", ["high", "medium", "low"]),
    sel("kind", "Any kind", [["clip_now", "Clip now"], ["needs_filming", "Needs filming"]]),
    h("button", { class: "btn small", onclick: () => { for (const k in F) F[k] = ""; render(); } }, "Clear"),
    count,
    h("span", { class: "muted small", style: { marginLeft: "auto" } },
      h("span", { class: "kbd" }, "j"), "/", h("span", { class: "kbd" }, "k"), " next/prev  ",
      h("span", { class: "kbd" }, "1"), h("span", { class: "kbd" }, "2"), h("span", { class: "kbd" }, "3"), " keep/maybe/reject")));
  main.appendChild(h("div", { id: "guide-list" }));
  renderGuideList();
}

function suggestionMatches(sg, F) {
  const st = sg.review?.status || "unreviewed";
  if (F.reviewer && sg.reviewer !== F.reviewer) return false;
  if (F.status && st !== F.status) return false;
  if (F.event && sg.event_id !== F.event) return false;
  if (F.priority && sg.priority !== F.priority) return false;
  if (F.kind && sg.kind !== F.kind) return false;
  if (F.pov) {
    const p = F.pov.toLowerCase();
    const inRanges = (sg.ranges || []).some((r) => (r.pov_name || "").toLowerCase() === p);
    const named = (sg.povs || []).some((x) => String(x).toLowerCase() === p);
    if (!inRanges && !named) return false;
  }
  if (F.q) {
    const q = F.q.toLowerCase();
    const hay = [sg.title, sg.event_id, sg.suggestion_id, sg.reviewer_name, sg.what_happens, sg.why, sg.hook, sg.structure,
      sg.pov_notes, sg.confirmed_by_transcript, sg.needs_visual_check, sg.uncertainty, sg.review?.notes,
      ...(sg.quotes || []).map((x) => `${x.speaker || ""} ${x.text || ""}`)].join(" \n ").toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function renderGuideList() {
  const g = state.guide, F = state.filters;
  const list = $("#guide-list");
  if (!list) return;
  list.innerHTML = "";
  const shown = g.suggestions.filter((sg) => suggestionMatches(sg, F));
  $("#guide-count").textContent = `${shown.length} of ${g.suggestions.length} suggestions`;
  const byEvent = new Map();
  for (const sg of shown) {
    const k = sg.event_id || "(no event)";
    if (!byEvent.has(k)) byEvent.set(k, []);
    byEvent.get(k).push(sg);
  }
  const evInfo = new Map(g.events.map((e) => [e.event_id, e]));
  const firstStart = (sgs) => Math.min(...sgs.map((x) => (x.untimed ? Infinity : x.start)));
  const groups = Array.from(byEvent.entries()).sort((a, b) => firstStart(a[1]) - firstStart(b[1]));
  if (!groups.length) list.appendChild(h("div", { class: "empty" }, "Nothing matches these filters."));
  for (const [eid, sgs] of groups) {
    const ev = evInfo.get(eid);
    sgs.sort((a, b) => (a.reviewer_name || "").localeCompare(b.reviewer_name || "") || a.start - b.start);
    const allInEvent = ev ? ev.suggestion_ids.length : sgs.length;
    const reviewersInEvent = new Set((ev ? ev.suggestion_ids.map(reviewerOfSid) : sgs.map((x) => x.reviewer)));
    const timed = sgs.filter((x) => !x.untimed);
    const lo = Math.min(...timed.map((x) => x.start)), hi = Math.max(...timed.map((x) => x.end));
    list.appendChild(h("section", { class: "event" },
      h("div", { class: "event-head" },
        h("span", { class: "eid" }, eid), h("span", { class: "etitle" }, ev?.title || sgs[0].title),
        timed.length ? h("span", { class: "muted mono" }, `${fmt(lo)} – ${fmt(hi)}`) : h("span", { class: "badge warn" }, "no timestamp"),
        reviewersInEvent.size > 1 ? h("span", { class: "badge good" }, `found by ${reviewersInEvent.size} reviewers — compare side by side`) : null,
        allInEvent > sgs.length ? h("span", { class: "muted small" }, `${allInEvent - sgs.length} more hidden by filters`) : null),
      h("div", { class: "cards" }, sgs.map(guideCard))));
  }
  const cards = $$(".card", list);
  if (state.focusIdx >= cards.length) state.focusIdx = cards.length - 1;
  if (state.focusIdx >= 0) cards[state.focusIdx].classList.add("focused");
}

function rangeNotes(r) {
  const notes = [];
  if (r.pieces > 1) notes.push(h("span", { class: "badge warn" }, `piece ${r.piece} of ${r.pieces}`));
  if (r.clipped_start) notes.push(h("span", { class: "badge warn", title: "The requested range starts before this part begins" }, "starts before part"));
  if (r.clipped_end) notes.push(h("span", { class: "badge warn", title: "The requested range continues after this part ends" }, "continues after part"));
  if (r.duration_unknown) notes.push(h("span", { class: "badge bad", title: "Video duration unknown; part end estimated from the SRT" }, "duration unknown"));
  if (r.offset_source === "default" && (r.part_index || 1) > 1) notes.push(h("span", { class: "badge bad", title: "This part's offset was never aligned" }, "unaligned"));
  else if (r.offset_source === "transcript" && r.offset_confidence != null && r.offset_confidence < 0.6)
    notes.push(h("span", { class: "badge warn" }, `offset conf ${Math.round(r.offset_confidence * 100)}%`));
  return notes;
}

function rangesTable(sg, ranges) {
  const povs = state.guide.povs;
  const covered = new Set(ranges.map((r) => r.pov_name));
  const missing = povs.filter((p) => !covered.has(p));
  const rows = ranges.slice().sort((a, b) => (a.pov_name || "").localeCompare(b.pov_name || "") || a.part_index - b.part_index).map((r) =>
    h("tr", null, h("td", null, r.pov_name), h("td", null, `part ${r.part_index}`),
      h("td", { class: "mono" }, `${fmt(r.local_start)} → ${fmt(r.local_end)}`), h("td", null, rangeNotes(r))));
  for (const p of missing) rows.push(h("tr", null, h("td", { class: "muted" }, p), h("td", null, "—"),
    h("td", { class: "muted" }, "no coverage of this moment"), h("td", null, h("span", { class: "badge bad", title: "This POV has recordings, but none covers this time (gap or not recording)" }, "no footage"))));
  return h("table", { class: "t ranges" },
    h("thead", null, h("tr", null, ["POV", "Part", "Local range (padded)", "Notes"].map((x) => h("th", null, x)))), h("tbody", null, rows));
}

function guideCard(sg) {
  const rev = sg.review || { status: null, notes: "", approved_start: null, approved_end: null };
  const card = h("div", { class: "card" + (rev.status ? " st-" + rev.status : ""), "data-sid": sg.suggestion_id });
  const savedEl = h("span", { class: "saved" }, rev.updated_at ? "saved " + rev.updated_at.replace("T", " ").slice(0, 19) : "");
  const notes = h("textarea", { rows: 2, placeholder: "Your notes (why keep / reject, preferred POV, cut ideas)…" }, rev.notes || "");
  const aStart = h("input", { class: "time", placeholder: "start", value: rev.approved_start == null ? "" : fmt(rev.approved_start) });
  const aEnd = h("input", { class: "time", placeholder: "end", value: rev.approved_end == null ? "" : fmt(rev.approved_end) });
  let status = rev.status || null;
  const btns = {};

  const save = async () => {
    savedEl.textContent = "saving…";
    try {
      const r = await api("/api/review", { suggestion_id: sg.suggestion_id, status, notes: notes.value,
        approved_start: aStart.value.trim() || null, approved_end: aEnd.value.trim() || null });
      sg.review = r.review;
      savedEl.textContent = "saved ✓";
      savedEl.className = "saved good-text";
      card.className = "card" + (status ? " st-" + status : "") + (card.classList.contains("focused") ? " focused" : "");
      for (const k in btns) btns[k].classList.toggle("on", k === status);
      bumpReviewerCounts();
    } catch (e) { savedEl.textContent = "not saved"; savedEl.className = "saved bad-text"; toast(e.message, "error"); }
  };
  const saveSoon = debounce(save, 600);
  card._setStatus = (st) => { status = status === st ? null : st; save(); };
  for (const st of ["keep", "maybe", "reject"])
    btns[st] = h("button", { class: `st-btn ${st}` + (status === st ? " on" : ""), onclick: () => card._setStatus(st) }, st[0].toUpperCase() + st.slice(1));
  notes.addEventListener("input", () => { savedEl.textContent = "…"; saveSoon(); });
  aStart.addEventListener("change", save); aEnd.addEventListener("change", save);

  const evInput = h("input", { list: "event-ids", value: sg.event_id || "", style: { width: "100px" }, class: "mono" });
  const reassign = (eventId) => action(async () => {
    const r = await api("/api/events/reassign", { suggestion_id: sg.suggestion_id, event_id: eventId() });
    toast(`${sg.suggestion_id} → ${r.event_id}`, "ok");
    await loadGuide(); state.session && api("/api/session").then(applyPayloadKeepGuide);
    render();
  });

  const sec = (label, text, cls) => text ? h("div", { class: "sec" + (cls ? " " + cls : "") }, h("b", null, label), text) : null;
  const quotes = (sg.quotes || []).length ? h("div", { class: "sec" }, h("b", null, "Quotes"),
    h("ul", { class: "quotes" }, sg.quotes.map((q) => h("li", null,
      q.speaker ? h("strong", null, q.speaker + ": ") : null, `“${q.text || ""}”`,
      h("div", { class: "prov" }, [q.time != null ? fmt(q.time) : null, q.source].filter(Boolean).join(" · ")))))) : null;
  const evidence = (sg.confirmed_by_transcript || sg.needs_visual_check || sg.uncertainty) ? h("div", { class: "evidence" },
    sg.confirmed_by_transcript ? h("div", { class: "c" }, h("b", null, "Confirmed by transcript: "), sg.confirmed_by_transcript) : null,
    sg.needs_visual_check ? h("div", { class: "v" }, h("b", null, "Needs visual check: "), sg.needs_visual_check) : null,
    sg.uncertainty ? h("div", { class: "u" }, h("b", null, "Uncertainty: "), sg.uncertainty) : null) : null;

  appendAll(card,
    h("div", { class: "card-top" },
      h("span", { class: "mono", style: { color: "var(--accent)" } }, sg.event_id || "—"),
      h("span", { class: "mono muted" }, sg.suggestion_id),
      h("span", { class: "badge rev" }, sg.reviewer_name || sg.reviewer),
      h("span", { class: "badge " + (sg.priority || "") }, sg.priority || "?"),
      sg.kind === "needs_filming" ? h("span", { class: "badge warn" }, "needs filming") : h("span", { class: "badge" }, "clip now"),
      savedEl),
    h("div", { class: "card-title" }, sg.title || "(untitled)"),
    sg.untimed
      ? h("div", { class: "small" }, h("span", { class: "badge warn" }, "Idea – no timestamp"),
          h("span", { class: "muted" }, " Not tied to a moment in the footage; not exported to LosslessCut."))
      : h("div", { class: "mono small" }, `Session ${fmt(sg.start)} – ${fmt(sg.end)}`, h("span", { class: "muted" }, ` (${fmtDur(sg.end - sg.start)}; ±${state.guide.pad_before}/${state.guide.pad_after}s padding in imports)`)),
    rev.approved_start != null && rev.approved_end != null ? h("div", { class: "mono small good-text" }, `Approved ${fmt(rev.approved_start)} – ${fmt(rev.approved_end)}`) : null,
    sg.range_error ? h("div", { class: "bad-text small" }, "Mapping failed: " + sg.range_error) : null,
    sg.untimed || (sg.kind === "needs_filming" && !(sg.ranges || []).length) ? null : rangesTable(sg, sg.ranges || []),
    sec("What happens", sg.what_happens), sec("Why it works", sg.why), sec("Hook", sg.hook), sec("Structure / edit", sg.structure),
    quotes,
    (sg.povs || []).length || sg.pov_notes ? h("div", { class: "sec" }, h("b", null, "POVs"),
      (sg.povs || []).map((p) => h("span", { class: "badge", style: { marginRight: "3px" } }, p)), sg.pov_notes ? h("div", null, sg.pov_notes) : null) : null,
    evidence,
    h("div", { class: "review-box" },
      h("div", { class: "row" }, btns.keep, btns.maybe, btns.reject,
        h("span", { class: "muted small", style: { marginLeft: "8px" } }, "Approved range:"), aStart, "–", aEnd,
        h("button", { class: "btn small", title: "Fill with the AI's suggested range", onclick: () => { aStart.value = fmt(sg.start); aEnd.value = fmt(sg.end); save(); } }, "use AI range")),
      h("div", { class: "row" }, notes),
      h("div", { class: "row small" }, h("span", { class: "muted" }, "Event:"), evInput,
        h("button", { class: "btn small", onclick: reassign(() => evInput.value.trim() || null) }, "Move to event"),
        h("button", { class: "btn small", onclick: reassign(() => null) }, "Split into new event"))));
  if (!$("#event-ids")) document.body.appendChild(h("datalist", { id: "event-ids" }));
  const dl = $("#event-ids"); dl.innerHTML = "";
  for (const e of state.guide.events) dl.appendChild(h("option", { value: e.event_id }, e.title || ""));
  card.addEventListener("click", () => {
    const cards = $$("#guide-list .card");
    state.focusIdx = cards.indexOf(card);
    cards.forEach((c) => c.classList.toggle("focused", c === card));
  });
  return card;
}

function applyPayloadKeepGuide(p) {
  const g = state.guide; applyPayload(p); state.guide = g;
}

function bumpReviewerCounts() {
  // Keep reviewer counters in the other tabs fresh without re-rendering the guide.
  api("/api/session").then(applyPayloadKeepGuide).catch(() => {});
}

document.addEventListener("keydown", (e) => {
  if (state.tab !== "guide") return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag)) return;
  const cards = $$("#guide-list .card");
  if (!cards.length) return;
  const focus = (i) => {
    state.focusIdx = Math.max(0, Math.min(cards.length - 1, i));
    cards.forEach((c, j) => c.classList.toggle("focused", j === state.focusIdx));
    cards[state.focusIdx].scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  if (e.key === "j") { focus(state.focusIdx + 1); e.preventDefault(); }
  else if (e.key === "k") { focus(state.focusIdx - 1); e.preventDefault(); }
  else if (["1", "2", "3"].includes(e.key) && state.focusIdx >= 0) {
    cards[state.focusIdx]._setStatus(["keep", "maybe", "reject"][Number(e.key) - 1]);
    e.preventDefault();
  }
});

// ---------------------------------------------------------------- 6. Export
const EXPORT_MODES = {
  all: { label: "All suggestions (incl. rejected)", statuses: ["keep", "maybe", "reject", null] },
  exclude_rejected: { label: "Exclude rejected", statuses: ["keep", "maybe", null] },
  keep: { label: "Keep only", statuses: ["keep"] },
};

// ---------------------------------------------------------------- Export to Resolve
const RESOLVE_REASONS = {
  no_coverage: "not recording", recording_gap: "gap between parts", mp4_missing: "MP4 missing",
  mic_blocked: "mic not confirmed", fps_mismatch: "frame rate", duration_unverified: "duration unknown",
  source_frame_range: "source too short", mic_unverified: "mic not confirmed", mic_unmapped: "mic stream not found",
};
let resolvePoll = null;

function openBtn(label, path) {
  return path ? h("button", { class: "btn small", onclick: action(() => api("/api/open-path", { path })) }, label) : null;
}

function issueList(items, cls) {
  return h("ul", { class: "issues " + cls }, items.map((b) => h("li", null,
    [b.event_id, b.pov_name, b.part_id].filter(Boolean).length
      ? h("b", null, [b.event_id, b.pov_name, b.part_id].filter(Boolean).join(" / ") + ": ") : null,
    b.message, " ", h("span", { class: "mono small muted" }, b.code))));
}

function micOptionLabel(o, m) {
  return `Track ${o.track} · MP4 audio ${o.mp4_audio_ordinal}${o.mp4_audio_title ? " “" + o.mp4_audio_title + "”" : ""}` +
    ` → Resolve ch ${o.resolve_channels.join("-")}${o.track === m.expected_track ? " (metadata expects this)" : ""}`;
}

// Pick one of the MP4's real audio tracks and confirm it in-page (no native dialogs): the
// button stays disabled until the owner ticks "I listened", so a stray click or double-click
// can never record a confirmation. Re-confirming replaces the earlier choice for the part.
function micConfirmControl(m, box) {
  const options = m.options || [];
  if (!m.mp4_found) return h("div", { class: "bad-text small" }, "No OBS-remuxed MP4 found for this part.");
  if (!options.length) {
    return h("div", { class: "bad-text small" },
      `None of the MP4's ${m.audio_streams} audio stream(s) can be matched to a track number; check the MP4's audio track titles.`);
  }
  const pick = h("select", null,
    m.default_track ? null : h("option", { value: "", selected: true }, "Choose the mic track…"),
    options.map((o) => h("option", { value: o.track, selected: o.track === m.default_track ? true : null }, micOptionLabel(o, m))));
  const listened = h("input", { type: "checkbox" });
  const button = h("button", { class: "btn small primary", disabled: true, onclick: action(async () => {
    const track = parseInt(pick.value, 10);
    if (!listened.checked) throw new Error("Tick “I listened” first");
    if (!(track > 0)) throw new Error("Choose the mic track first");
    await api("/api/resolve/confirm-mic", { part_id: m.part_id, transcriber_track: track, speaker: m.pov_name });
    toast(`Mic confirmed for ${m.pov_name} part ${m.part_index}: track ${track}`, "ok");
    await loadResolvePreview(box);
  }) }, "Confirm mic");
  const sync = () => { button.disabled = !listened.checked || !pick.value; };
  listened.addEventListener("change", sync);
  pick.addEventListener("change", () => { listened.checked = false; sync(); });
  return h("div", { class: "row" }, pick,
    h("label", { class: "small" }, listened, " I listened: this is ", m.pov_name, "'s own mic"), button);
}

function resolveMicRow(m, box) {
  const verified = m.status === "verified" || m.status === "owner_confirmed";
  let control = null;
  if (m.status === "unverified" || m.status === "unmapped") {
    control = micConfirmControl(m, box);
  } else if (m.status === "owner_confirmed") {
    // Let the owner correct a wrong pick without a dialog: reveal the same control on demand.
    control = h("div", null);
    control.appendChild(h("button", { class: "btn small", onclick: () => control.replaceChildren(micConfirmControl(m, box)) }, "Change…"));
  }
  const expected = m.expected_track
    ? ` · metadata expects track ${m.expected_track}` +
      ((m.options || []).some((o) => o.track === m.expected_track) ? "" : ", which the MP4 cannot supply")
    : "";
  const says = verified
    ? `Track ${m.transcriber_track} · ${m.speaker}${m.mp4_audio_title ? " · MP4 “" + m.mp4_audio_title + "”" : ""}` +
      (m.resolve_channels ? ` → Resolve ch ${m.resolve_channels.join("-")}` : "")
    : (m.metadata_found
      ? ((m.metadata_tracks || []).map((t) => `track ${t.track} = ${t.speaker || "?"}${t.title ? " (" + t.title + ")" : ""}`).join("; ") || "no speaker tracks") +
        ((m.relabel_provenance || []).length ? ` · labels ${m.relabel_provenance.join(", ")}` : "")
      : "no transcriber JSON found");
  return h("tr", null,
    h("td", null, m.pov_name), h("td", null, m.part_index ?? m.part_id),
    h("td", null, h("span", { class: "badge " + (verified ? "good" : "bad") }, m.status.replace("_", " "))),
    h("td", { class: "small" }, says, expected && h("span", { class: "muted" }, expected),
      verified ? h("div", { class: "muted small" }, m.evidence || "") : h("div", { class: "bad-text small" }, m.message || "")),
    h("td", null, control));
}

function resolveSegmentsTable(man) {
  const warn = {};
  for (const w of man.warnings) if (w.event_id) (warn[w.event_id] = warn[w.event_id] || []).push(w);
  return h("table", { class: "t resolve-segs" },
    h("thead", null, h("tr", null, ["#", "Event", "Title", "Session range", "Angles", "Group reference audio", "Unavailable", "Notes"].map((x) => h("th", null, x)))),
    h("tbody", null, man.segments.map((sg, i) => {
      const notes = [];
      if (sg.pieces > 1) notes.push(h("span", { class: "badge warn" }, `piece ${sg.piece}/${sg.pieces}`));
      if (sg.duration_frames < man.fps) notes.push(h("span", { class: "badge warn" }, `short: ${sg.duration_frames} frames`));
      for (const w of (sg.piece === 1 ? warn[sg.event_id] || [] : [])) notes.push(h("div", { class: "warn-text small" }, w.message));
      return h("tr", null,
        h("td", { class: "muted" }, i + 1),
        h("td", { class: "mono" }, sg.event_id),
        h("td", null, sg.title, " ", h("span", { class: "muted small" }, `(${sg.range_source})`)),
        h("td", { class: "mono small" }, `${fmt(sg.session_start)} – ${fmt(sg.session_end)}`, h("div", { class: "muted" }, fmtDur(sg.session_end - sg.session_start))),
        h("td", null, sg.angles.length ? sg.angles.map((a) => h("span", { class: "badge good" }, `${a.pov_name} P${a.part_index}`)) : h("span", { class: "bad-text" }, "none")),
        h("td", null, sg.reference_audio ? `${sg.reference_audio.pov_name} / ${sg.reference_audio.part_id}` : h("span", { class: "warn-text" }, "gap")),
        h("td", null, sg.unavailable.map((u) => h("span", { class: "badge bad", title: u.reason }, `${u.pov_name}: ${RESOLVE_REASONS[u.reason] || u.reason}`))),
        h("td", null, notes));
    })));
}

function resolveJobBox(job, box) {
  if (!job || job.state === "idle") return null;
  const links = h("div", { class: "row" }, openBtn("Open report.md", job.report_path), openBtn("Open export folder", job.out_dir),
    job.readback_path ? openBtn("Open readback.json", job.readback_path) : null);
  if (job.state === "running") {
    return h("div", { class: "resolve-job" }, h("b", null, "Exporting to Resolve… "),
      `creating project “${job.project}”. Do not touch Resolve until this finishes.`, links);
  }
  if (job.state === "done") {
    const rb = job.readback || {};
    return h("div", { class: "resolve-job ok" },
      h("b", { class: "good-text" }, "Export complete. "),
      `Project “${rb.project || job.project}”, timeline “${rb.timeline || ""}”, ${(rb.segments || []).length} multicam segment(s), ` +
      `${(rb.sources || []).length} source MP4(s)${rb.reopened ? ", verified after save and reopen" : ""}.`,
      (rb.limitations || []).length ? h("ul", { class: "small muted" }, rb.limitations.map((l) => h("li", null, l))) : null,
      links);
  }
  return h("div", { class: "resolve-job bad" }, h("b", { class: "bad-text" }, "Resolve export stopped."),
    h("pre", { class: "setup bad-text" }, job.error || "Unknown error"), links);
}

function pollResolve(box) {
  if (resolvePoll) return;
  resolvePoll = setInterval(async () => {
    try {
      const job = await api("/api/resolve/status");
      if (job.state !== "running") { clearInterval(resolvePoll); resolvePoll = null; }
      if (state.resolve) state.resolve.job = job;
      if (box.isConnected) fillResolve(box);
      if (job.state === "done") toast("Resolve export complete", "ok");
      if (job.state === "failed") toast("Resolve export stopped — see the Export tab", "error");
    } catch (e) { clearInterval(resolvePoll); resolvePoll = null; toast(e.message, "error"); }
  }, 1500);
}

async function loadResolvePreview(box) {
  state.resolve = await api("/api/resolve/preview");
  fillResolve(box);
}

function fillResolve(box) {
  while (box.childNodes.length > 2) box.lastChild.remove();
  const r = state.resolve;
  if (!r) return;
  const man = r.manifest;
  const running = r.job && r.job.state === "running";
  if (running) pollResolve(box);
  const selections = new Set(man.segments.map((s) => s.suggestion_id)).size;
  appendAll(box,
    h("div", { class: "muted small" }, `${selections} Keep selection(s) → ${man.segments.length} multicam segment(s) at ${man.fps} fps, in session order. `,
      "Multicam audio uses Resolve Source mode with each player's own mic."),
    man.blockers.length ? h("div", { class: "resolve-blockers" }, h("b", null, `Blocked (${man.blockers.length}) — fix these before exporting:`), issueList(man.blockers, "bad-text")) : null,
    r.mics.length ? h("div", null, h("h3", null, "Microphones"),
      h("table", { class: "t" }, h("thead", null, h("tr", null, ["Player", "Part", "Status", "Metadata says", ""].map((x) => h("th", null, x)))),
        h("tbody", null, r.mics.map((m) => resolveMicRow(m, box))))) : null,
    man.segments.length ? h("div", null, h("h3", null, "Segments"), resolveSegmentsTable(man)) : null,
    man.warnings.filter((w) => !w.event_id).length ? h("div", null, h("h3", null, "Warnings"), issueList(man.warnings.filter((w) => !w.event_id), "warn-text")) : null,
    h("div", { class: "row resolve-go" },
      h("button", { class: "btn primary", disabled: man.blockers.length || running ? true : null, onclick: action(async () => {
        const job = await api("/api/resolve/export", {});
        state.resolve.job = job; fillResolve(box);
      }) }, "Export to Resolve"),
      h("span", { class: "muted small" }, "Before exporting: save and close your Resolve project, leave the Project Manager open, and set Preferences → General → External scripting using = Local.")),
    resolveJobBox(r.job, box));
}

function resolvePanel() {
  const box = h("div", { class: "panel" },
    h("h2", null, "Export to Resolve"),
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: action(() => loadResolvePreview(box)) }, state.resolve ? "Refresh preview" : "Preview"),
      h("span", { class: "muted small" }, "Builds one new DaVinci Resolve project with a multicam clip per Keep selection. Nothing is written until you export.")));
  if (state.resolve) fillResolve(box);
  else api("/api/resolve/status").then((job) => {
    if (job.state !== "idle") { state.resolve = null; loadResolvePreview(box).catch((e) => toast(e.message, "error")); }
  }).catch(() => {});
  return box;
}

function renderExport(main) {
  const s = state.session;
  main.appendChild(resolvePanel());
  const modes = h("div", { class: "row" }, Object.entries(EXPORT_MODES).map(([k, m]) =>
    h("label", null, h("input", { type: "radio", name: "xmode", value: k, checked: state.exportMode === k,
      onchange: () => { state.exportMode = k; } }), " ", m.label)));
  main.appendChild(h("div", { class: "panel" },
    h("h2", null, "Generate LosslessCut imports"),
    h("div", { class: "muted small" }, "One CSV per reviewer × recording part × pass. Overlapping suggestions from one reviewer go into separate passes — nothing is trimmed or dropped. ",
      "Files are written to ", h("span", { class: "mono" }, (s.folder || "") + "\\autoclipper_exports\\<reviewer>\\"), " (regenerating overwrites them)."),
    modes,
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: action(async () => {
        const r = await api("/api/export", { statuses: EXPORT_MODES[state.exportMode].statuses });
        state.exportResult = r; state.exportInfo = r;
        toast(`Wrote ${r.files.length} CSV file(s)`, "ok"); render();
      }) }, "Generate CSVs"),
      h("button", { class: "btn", onclick: action(() => api("/api/open-path", { path: "autoclipper_exports" })) }, "Open exports folder"))));

  const r = state.exportResult;
  if (r) {
    main.appendChild(h("div", { class: "panel" },
      h("h2", null, `Generated files (${r.files.length})`),
      r.files.length ? h("table", { class: "t" },
        h("thead", null, h("tr", null, ["Reviewer", "POV", "Part", "Pass", "Rows", "CSV file", "Load into", ""].map((x) => h("th", null, x)))),
        h("tbody", null, r.files.map((f) => h("tr", null,
          h("td", null, h("span", { class: "badge rev" }, f.reviewer)), h("td", null, f.pov_name), h("td", null, f.part_index ?? f.part_id),
          h("td", null, f.pass_no), h("td", null, f.rows), h("td", { class: "mono small", title: f.csv_path }, f.csv_path.startsWith(r.out_dir) ? f.csv_path.slice(r.out_dir.length + 1) : f.csv_path),
          h("td", { class: "mono small muted" }, basename(f.video_path) || "(no video)"),
          h("td", null, h("button", { class: "btn small", onclick: action(() => api("/api/open-path", { path: f.folder })) }, "Open folder"))))))
        : h("div", { class: "muted" }, "No rows matched the selected statuses.")));
  }

  const infoBox = h("div", { class: "panel" }, h("h2", null, "Expected export filenames"), h("div", { class: "muted" }, "Loading…"));
  const setupBox = h("div", { class: "panel" }, h("h2", null, "LosslessCut one-time setup"), h("div", { class: "muted" }, "Loading…"));
  main.appendChild(h("div", { class: "grid2" }, infoBox, setupBox));
  const fill = (info) => {
    infoBox.lastChild.remove();
    const entries = Object.entries(info.expected || {});
    infoBox.appendChild(entries.length ? h("div", null, entries.map(([rv, names]) => h("div", null,
      h("h3", null, rv, h("span", { class: "muted small" }, ` · ${names.length} clip(s)`)),
      h("div", { class: "namelist" }, names.map((n) => h("div", null, n))))))
      : h("div", { class: "muted" }, "Import AI reviews first."));
    setupBox.lastChild.remove();
    setupBox.appendChild(h("pre", { class: "setup" }, info.setup || ""));
  };
  if (state.exportInfo) fill(state.exportInfo);
  else api("/api/export/info").then((i) => { state.exportInfo = i; fill(i); }).catch((e) => { infoBox.lastChild.textContent = e.message; setupBox.lastChild.textContent = e.message; });
}

// ---------------------------------------------------------------- boot
$$("#tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
(async function boot() {
  try {
    const p = await api("/api/session");
    applyPayload(p);
    const want = location.hash.slice(1);
    state.tab = want && $(`#tabs button[data-tab="${want}"]`) ? want : "recordings";
  } catch (_) { state.tab = "session"; }
  updateChrome();
  render();
})();
