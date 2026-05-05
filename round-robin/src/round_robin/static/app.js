// Round Robin frontend. Tiny event-handler registry + REST/WebSocket plumbing.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  models: [],
  link: null,
  presets: [],
  sessions: [],
  errors: [],
  errorStats: { total: 0, by_category: {} },
  unseenErrors: 0,
  runStatus: "idle",
  currentRunId: null,
  cardsByTurn: new Map(), // key: `${turn}-${agentName}` -> { card, contentEl }
  totalTurns: 1,
  alphaUp: false,
  bravoUp: false,
  userScrolledUp: false,
};

// ── tiny event handler registry ─────────────────────────────────────────────

const handlers = {};
const on = (type, fn) => { handlers[type] = fn; };
const dispatch = (msg) => {
  const fn = handlers[msg.type];
  if (fn) {
    try { fn(msg); }
    catch (e) { console.error(`Handler ${msg.type} crashed:`, e); }
  } else {
    console.debug("No handler for", msg.type, msg);
  }
};

// ── REST helpers ───────────────────────────────────────────────────────────

function formatDetail(detail, fallback) {
  // FastAPI returns `detail` as a string for HTTPException, but as an array of
  // {loc, msg, type} for Pydantic 422, and the global exception handler sends
  // an object. Render each shape readably so toasts never say "[object Object]".
  if (detail == null) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item.msg === "string") {
        const where = Array.isArray(item.loc) ? item.loc.join(".") : "";
        return where ? `${where}: ${item.msg}` : item.msg;
      }
      return JSON.stringify(item);
    }).join("; ");
  }
  if (typeof detail === "object") return JSON.stringify(detail);
  return String(detail);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text();
    let msg = text;
    try {
      const parsed = JSON.parse(text);
      msg = formatDetail(parsed.detail, text);
    } catch {}
    throw new Error(`${r.status}: ${msg}`);
  }
  return r.json();
}

function toast(message, kind = "info", ms = 3500) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = message;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

// ── Health & models ────────────────────────────────────────────────────────

async function refreshHealth(opts = {}) {
  const interactive = !!opts.interactive;
  const btn = interactive ? $("#btn-test") : null;
  const originalLabel = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
  try {
    const h = await api("/api/health");
    state.models = h.models || [];
    state.link = h.link;
    populateModelDropdowns();
    paintHealthPills(h);
    if (interactive) {
      flashHealthPills();
      const localCount = (h.models || []).filter(m => m.is_local).length;
      const remoteCount = (h.models || []).filter(m => !m.is_local && m.device).length;
      const link = h.link?.enabled ? "LM Link active"
                 : h.link ? "LM Link disabled"
                 : "LM Link not detected";
      const summary = h.reachable
        ? `LM Studio reachable · ${localCount} local model${localCount === 1 ? "" : "s"}` +
          (remoteCount ? ` · ${remoteCount} remote` : "") + ` · ${link}`
        : "LM Studio not reachable on localhost:1234";
      toast(summary, h.reachable ? "success" : "error", 5000);
    }
  } catch (e) {
    toast(`Health check failed: ${e.message}`, "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = originalLabel || "Test connection"; }
  }
}

function flashHealthPills() {
  for (const sel of ['[data-peer="alpha"]', '[data-peer="bravo"]', '[data-bind="linkPill"]']) {
    const el = document.querySelector(sel);
    if (!el) continue;
    el.classList.remove("just-tested");
    // Force reflow so the animation re-triggers even on consecutive clicks
    void el.offsetWidth;
    el.classList.add("just-tested");
    setTimeout(() => el.classList.remove("just-tested"), 900);
  }
}

function paintHealthPills(h) {
  const alphaPill = $('[data-peer="alpha"]');
  const bravoPill = $('[data-peer="bravo"]');
  const linkPill = $('[data-bind="linkPill"]');

  state.alphaUp = !!h.reachable;
  alphaPill.classList.toggle("up", h.reachable);
  alphaPill.classList.toggle("down", !h.reachable);
  $('[data-bind="alphaModel"]').textContent = $("#alpha-model")?.value || "—";

  // Bravo presence is inferred from LM Link reporting >1 device
  const bravoVisible = !!(h.link && (h.link.devices || []).length >= 2);
  state.bravoUp = bravoVisible;
  bravoPill.classList.toggle("up", bravoVisible);
  bravoPill.classList.toggle("down", !bravoVisible);
  $('[data-bind="bravoModel"]').textContent = $("#bravo-model")?.value || "—";

  if (!h.link) {
    linkPill.textContent = "LM Link: not detected";
    linkPill.title = "lms CLI not found or returned no link data";
    linkPill.classList.remove("ok"); linkPill.classList.add("preview-missing");
  } else if (h.link.enabled) {
    linkPill.textContent = `LM Link: ${(h.link.devices || []).join(", ") || "enabled"}`;
    linkPill.title = buildLinkTooltip(h.link);
    linkPill.classList.add("ok"); linkPill.classList.remove("preview-missing");
  } else {
    linkPill.textContent = "LM Link: disabled";
    linkPill.title = "LM Link reported disabled";
    linkPill.classList.remove("ok"); linkPill.classList.add("preview-missing");
  }

  $("#btn-start").disabled = !h.reachable;
}

function buildLinkTooltip(link) {
  const lines = [];
  if (link.this_device) lines.push(`This device: ${link.this_device}`);
  for (const d of link.remote_devices || []) {
    const label = `${d.name}` + (d.status ? ` (${d.status})` : "");
    const id = d.identifier ? `\n  identifier: ${d.identifier}` : "";
    const models = (d.loaded_models || []).length
      ? `\n  loaded: ${d.loaded_models.join(", ")}`
      : "\n  no models loaded";
    lines.push(`${label}${id}${models}`);
  }
  return lines.join("\n\n") || "LM Link enabled";
}

function populateModelDropdowns() {
  const targets = ["alpha-model", "bravo-model", "charlie-model"];
  // Group models: locals first (Alpha), then by remote device, then unknown.
  const locals = [];
  const remotesByDevice = new Map();
  const unknowns = [];
  for (const m of state.models) {
    if (m.is_local) locals.push(m);
    else if (m.device) {
      if (!remotesByDevice.has(m.device)) remotesByDevice.set(m.device, []);
      remotesByDevice.get(m.device).push(m);
    } else {
      unknowns.push(m);
    }
  }
  for (const id of targets) {
    const el = $("#" + id);
    if (!el) continue;
    const prev = el.value;
    el.innerHTML = "";
    if (state.models.length === 0) {
      const opt = document.createElement("option");
      opt.value = ""; opt.textContent = "(no models loaded)";
      el.appendChild(opt);
      updateHostBadge(id, null);
      continue;
    }
    if (locals.length) {
      const g = document.createElement("optgroup");
      g.label = "● Alpha (this machine)";
      for (const m of locals) g.appendChild(makeModelOption(m));
      el.appendChild(g);
    }
    for (const [device, list] of remotesByDevice) {
      const g = document.createElement("optgroup");
      g.label = `● Bravo — ${device}`;
      const remoteMeta = (state.link?.remote_devices || []).find(d => d.name === device);
      if (remoteMeta?.identifier) g.title = `Identifier: ${remoteMeta.identifier}`;
      for (const m of list) g.appendChild(makeModelOption(m));
      el.appendChild(g);
    }
    if (unknowns.length) {
      const g = document.createElement("optgroup");
      g.label = "● Unknown host";
      for (const m of unknowns) g.appendChild(makeModelOption(m));
      el.appendChild(g);
    }
    if (prev && el.querySelector(`option[value="${cssEscape(prev)}"]`)) el.value = prev;
    updateHostBadge(id, modelById(el.value));
  }
  applyPendingModelSelection();
  validateModelClash();
}

function makeModelOption(m) {
  const opt = document.createElement("option");
  opt.value = m.id || "";
  opt.textContent = m.id;
  opt.dataset.device = m.device || "";
  opt.dataset.isLocal = m.is_local ? "1" : "0";
  // Hover tooltip: device + identifier so the user can verify which peer hosts it
  const id = (state.link?.remote_devices || []).find(d => d.name === m.device)?.identifier;
  const lines = [];
  if (m.device) lines.push(`Hosted on: ${m.device}${m.is_local ? " (this machine)" : " (remote)"}`);
  if (id) lines.push(`Identifier: ${id}`);
  if (lines.length) opt.title = lines.join("\n");
  return opt;
}

function modelById(id) {
  return state.models.find((m) => m.id === id) || null;
}

function cssEscape(s) {
  return (s || "").replace(/["\\]/g, "\\$&");
}

function updateHostBadge(selectId, m) {
  const badge = $("#" + selectId.replace(/-model$/, "-host-badge"));
  if (!badge) return;
  badge.classList.remove("alpha", "bravo", "unknown");
  badge.title = "";
  if (!m) { badge.classList.add("unknown"); badge.textContent = "—"; return; }
  if (m.is_local) {
    badge.classList.add("alpha");
    badge.textContent = "Alpha";
    if (state.link?.this_device) badge.title = `This machine: ${state.link.this_device}`;
  } else if (m.device) {
    badge.classList.add("bravo");
    badge.textContent = `Bravo · ${m.device}`;
    const id = (state.link?.remote_devices || []).find(d => d.name === m.device)?.identifier;
    if (id) badge.title = `Remote device: ${m.device}\nIdentifier: ${id}`;
  } else {
    badge.classList.add("unknown"); badge.textContent = "Unknown";
  }
}

function validateModelClash() {
  const a = $("#alpha-model").value;
  const b = $("#bravo-model").value;
  const banner = ensureBanner();
  // No real choice if only one model is loaded -> no real warning.
  if (state.models.length <= 1) { banner.hide(); return; }
  if (a && b && a === b) {
    banner.show("Both agents are pointing at the same model. LM Link will route both turns to the same device.", "warn");
  } else {
    banner.hide();
  }
}

function ensureBanner() {
  let el = $("#model-clash-banner");
  if (!el) {
    el = document.createElement("div");
    el.id = "model-clash-banner";
    el.style.cssText = "background:#3a2c0a;border:1px solid #6b5012;color:#fbbf24;padding:8px 12px;border-radius:6px;margin-bottom:10px;display:none;";
    $(".main").prepend(el);
  }
  return {
    show: (msg) => { el.textContent = msg; el.style.display = "block"; },
    hide: () => { el.style.display = "none"; },
  };
}

// ── Tabs ───────────────────────────────────────────────────────────────────

function setupTabs() {
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`.tab-panel[data-panel="${btn.dataset.tab}"]`).classList.add("active");
      if (btn.dataset.tab === "presets") refreshPresets();
      if (btn.dataset.tab === "history") refreshSessions();
      if (btn.dataset.tab === "errors") {
        refreshErrors();
        clearErrorBadge();
      }
    });
  });
}

// ── Run controls ───────────────────────────────────────────────────────────

function readConfig() {
  return {
    theme: $("#cfg-theme").value.trim(),
    agents: [
      { name: $("#alpha-name").value.trim() || "Alpha", model: $("#alpha-model").value, persona: $("#alpha-persona").value.trim() },
      { name: $("#bravo-name").value.trim() || "Bravo", model: $("#bravo-model").value, persona: $("#bravo-persona").value.trim() },
    ],
    loop_limit: parseInt($("#loop-limit").value, 10) || 3,
    pause_after_each_turn: $("#pause-each-turn").checked,
    auto_retry: parseInt($("#auto-retry").value, 10) || 0,
    auto_retry_backoff_s: 2.0,
    charlie: {
      enabled: $("#charlie-enabled").checked,
      model: $("#charlie-model").value || "",
    },
    intel_collab_directive: $("#intel-collab").checked,
    intel_anti_rambling: $("#intel-rambling").checked,
    intel_anti_yes_man: $("#intel-yesman").checked,
    intel_agreement_threshold: parseInt($("#intel-agreement-threshold").value, 10) || 2,
  };
}

function applyConfig(cfg) {
  if (!cfg) return;
  $("#cfg-theme").value = cfg.theme || "";
  const [a, b] = cfg.agents || [];
  if (a) {
    $("#alpha-name").value = a.name || "Alpha";
    $("#alpha-persona").value = a.persona || "";
    if (a.model) $("#alpha-model").value = a.model;
  }
  if (b) {
    $("#bravo-name").value = b.name || "Bravo";
    $("#bravo-persona").value = b.persona || "";
    if (b.model) $("#bravo-model").value = b.model;
  }
  $("#loop-limit").value = cfg.loop_limit || 3;
  $("#pause-each-turn").checked = !!cfg.pause_after_each_turn;
  $("#auto-retry").value = cfg.auto_retry || 0;
  if (cfg.charlie) {
    $("#charlie-enabled").checked = !!cfg.charlie.enabled;
    $("#charlie-model").disabled = !cfg.charlie.enabled;
    if (cfg.charlie.model) $("#charlie-model").value = cfg.charlie.model;
  }
  if ("intel_collab_directive" in cfg) $("#intel-collab").checked = !!cfg.intel_collab_directive;
  if ("intel_anti_rambling" in cfg) $("#intel-rambling").checked = !!cfg.intel_anti_rambling;
  if ("intel_anti_yes_man" in cfg) $("#intel-yesman").checked = !!cfg.intel_anti_yes_man;
  if ("intel_agreement_threshold" in cfg) $("#intel-agreement-threshold").value = cfg.intel_agreement_threshold;
  ["alpha-model", "bravo-model", "charlie-model"].forEach((id) => {
    updateHostBadge(id, modelById($("#" + id).value));
  });
  validateModelClash();
}

async function startRun() {
  const body = readConfig();
  if (!body.agents[0].model || !body.agents[1].model) {
    toast("Pick a model for each agent first.", "error");
    return;
  }
  try {
    clearDialogue();
    await api("/api/run/start", { method: "POST", body });
    setRunControls("running");
  } catch (e) {
    toast(`Start failed: ${e.message}`, "error");
  }
}

async function stopRun() {
  try { await api("/api/run/stop", { method: "POST" }); }
  catch (e) { toast(`Stop failed: ${e.message}`, "error"); }
}

async function pauseRun() {
  try { await api("/api/run/pause", { method: "POST" }); }
  catch (e) { toast(`Pause failed: ${e.message}`, "error"); }
}

async function resumeRun(injection) {
  try { await api("/api/run/resume", { method: "POST", body: { injection: injection || null } }); }
  catch (e) { toast(`Resume failed: ${e.message}`, "error"); }
}

async function chooseAction(action) {
  try { await api("/api/run/choose", { method: "POST", body: { action } }); }
  catch (e) { toast(`Action failed: ${e.message}`, "error"); }
}

function setRunControls(status) {
  state.runStatus = status;
  const running = status === "running" || status === "paused" || status === "awaiting_user";
  $("#btn-start").disabled = running;
  $("#btn-pause").disabled = !running || status !== "running";
  $("#btn-stop").disabled = !running;
  $('[data-bind="runStatusBar"]').textContent = `Status: ${status}`;
}

function clearDialogue() {
  $("#dialogue").innerHTML = "";
  state.cardsByTurn.clear();
  state.userScrolledUp = false;
  $("#run-progress").classList.add("hidden");
}

// ── Dialogue rendering ─────────────────────────────────────────────────────

function showProgress(turn, agentName, totalTurns) {
  state.totalTurns = totalTurns || state.totalTurns;
  $("#run-progress").classList.remove("hidden");
  $('[data-bind="progressText"]').textContent =
    `Round ${turn + 1} of ${state.totalTurns} — ${agentName} speaking…`;
  const pct = ((turn + 0.5) / state.totalTurns) * 100;
  $('[data-bind="progressFill"]').style.width = `${Math.min(pct, 100)}%`;
}

function ensureCard(turn, agentName, model) {
  const key = `${turn}-${agentName}`;
  let entry = state.cardsByTurn.get(key);
  if (entry) return entry;
  // Mark previous active card inactive
  $$(".turn-card.active").forEach((c) => c.classList.remove("active"));

  const card = document.createElement("div");
  card.className = "turn-card active";
  // Color the left border by host machine for at-a-glance Alpha/Bravo distinction
  const m = modelById(model);
  if (m) card.classList.add(m.is_local ? "alpha-host" : "bravo-host");

  const header = document.createElement("div");
  header.className = "turn-header";
  const left = document.createElement("span");
  left.innerHTML = `<span class="turn-agent">${escapeHtml(agentName)}</span>`;
  const right = document.createElement("span");
  right.textContent = `Round ${turn + 1} · ${model || ""}`;
  const copyBtn = document.createElement("button");
  copyBtn.className = "copy-btn";
  copyBtn.type = "button";
  copyBtn.textContent = "Copy";
  copyBtn.addEventListener("click", () => copyCardContent(copyBtn, content));
  right.appendChild(copyBtn);
  header.appendChild(left); header.appendChild(right);

  const content = document.createElement("div");
  content.className = "turn-content";
  card.appendChild(header);
  card.appendChild(content);
  $("#dialogue").appendChild(card);
  scrollDialogueIfAtBottom(true);   // first paint: force-scroll
  entry = { card, contentEl: content };
  state.cardsByTurn.set(key, entry);
  return entry;
}

async function copyCardContent(btn, contentEl) {
  try {
    await navigator.clipboard.writeText(contentEl.textContent || "");
    btn.classList.add("copied");
    const original = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.classList.remove("copied"); btn.textContent = original; }, 1200);
  } catch (e) {
    toast(`Copy failed: ${e.message}`, "error");
  }
}

function appendToken(turn, agentName, token) {
  const entry = state.cardsByTurn.get(`${turn}-${agentName}`);
  if (!entry) return;
  entry.contentEl.textContent += token;
  scrollDialogueIfAtBottom(false);
}

function scrollDialogueIfAtBottom(force) {
  const dlg = $("#dialogue");
  if (!dlg) return;
  // Don't fight the user: skip auto-scroll if they scrolled up OR have a selection.
  if (!force) {
    if (state.userScrolledUp) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
  }
  dlg.scrollTop = dlg.scrollHeight;
}

function setupDialogueScrollTracking() {
  const dlg = $("#dialogue");
  if (!dlg) return;
  dlg.addEventListener("scroll", () => {
    const distanceFromBottom = dlg.scrollHeight - (dlg.scrollTop + dlg.clientHeight);
    state.userScrolledUp = distanceFromBottom > 40;
  });
}

function finalizeCard(turn, agentName, latencyMs, tokenCount) {
  const entry = state.cardsByTurn.get(`${turn}-${agentName}`);
  if (!entry) return;
  // Drop the streaming pulse — this card is settled now.
  entry.card.classList.remove("active");
  const stats = document.createElement("div");
  stats.className = "turn-stats";
  stats.textContent = `${tokenCount || 0} tokens · ${(latencyMs / 1000).toFixed(1)}s`;
  entry.card.appendChild(stats);
}

function showAgentError(turn, agentName, errorClass, message) {
  const entry = state.cardsByTurn.get(`${turn}-${agentName}`)
    || ensureCard(turn, agentName, "");
  entry.card.classList.add("error");
  const err = document.createElement("div");
  err.className = "turn-stats";
  err.style.color = "var(--red)";
  err.textContent = `${errorClass}: ${message}`;
  entry.card.appendChild(err);
  const actions = document.createElement("div");
  actions.className = "error-actions";
  for (const action of ["retry", "skip", "use_other", "stop"]) {
    const btn = document.createElement("button");
    btn.className = "ghost";
    btn.textContent = action.replace("_", " ");
    btn.addEventListener("click", () => {
      actions.remove();
      chooseAction(action);
    });
    actions.appendChild(btn);
  }
  entry.card.appendChild(actions);
}

function showPauseBanner() {
  const banner = document.createElement("div");
  banner.className = "pause-banner";
  banner.innerHTML = `<textarea placeholder="Optional nudge to inject…"></textarea>`;
  const cont = document.createElement("button");
  cont.className = "primary";
  cont.textContent = "Continue";
  cont.addEventListener("click", () => {
    const text = banner.querySelector("textarea").value.trim();
    banner.remove();
    resumeRun(text || null);
  });
  const stop = document.createElement("button");
  stop.className = "danger";
  stop.textContent = "Stop";
  stop.addEventListener("click", () => { banner.remove(); stopRun(); });
  banner.appendChild(cont);
  banner.appendChild(stop);
  $("#dialogue").appendChild(banner);
  scrollDialogueIfAtBottom(true);
}

function appendNudgeCard(reason, content, turn) {
  const card = document.createElement("div");
  card.className = "nudge-card";
  const tag = document.createElement("span");
  tag.className = "nudge-tag";
  tag.textContent = reason || "nudge";
  const body = document.createElement("span");
  body.textContent = content || "";
  card.appendChild(tag); card.appendChild(body);
  $("#dialogue").appendChild(card);
  scrollDialogueIfAtBottom(false);
}

// ── Persona handoff (from prompt-enhancer) ─────────────────────────────────

async function checkPersonaHandoff() {
  // Fetch any staged handoff (POSTed by prompt-enhancer's "Send to Round Robin"
  // button). One-shot: prefill empty fields only, then DELETE so a refresh
  // doesn't re-stamp the same handoff over later user edits.
  let payload;
  try {
    const r = await fetch("/api/persona-handoff");
    if (r.status === 204) return;
    if (!r.ok) return;
    payload = await r.json();
  } catch { return; }
  if (!payload) return;

  const themeEl = $("#cfg-theme");
  const alphaEl = $("#alpha-persona");
  const bravoEl = $("#bravo-persona");
  let prefilled = false;

  if (payload.theme && themeEl && !themeEl.value.trim()) {
    themeEl.value = payload.theme;
    prefilled = true;
  }
  if (payload.alpha_persona && alphaEl && !alphaEl.value.trim()) {
    alphaEl.value = payload.alpha_persona;
    prefilled = true;
  }
  if (payload.bravo_persona && bravoEl && !bravoEl.value.trim()) {
    bravoEl.value = payload.bravo_persona;
    prefilled = true;
  }

  if (prefilled) {
    showHandoffBanner();
    // Persist the prefilled values to user-config so they survive a reload.
    scheduleConfigSave();
  }

  // One-shot: clear the staged payload regardless of whether we actually
  // prefilled (handoff was consumed; refreshing the page should not re-trigger).
  try { await fetch("/api/persona-handoff", { method: "DELETE" }); }
  catch { /* silent */ }
}

function showHandoffBanner() {
  const banner = $("#handoff-banner");
  if (!banner) return;
  banner.hidden = false;
}

// ── Crash recovery banner ──────────────────────────────────────────────────

async function checkRecoverableState() {
  try {
    const data = await api("/api/state");
    if (data.resumable && data.saved) showRecoveryBanner(data.saved);
  } catch { /* silent — no banner if endpoint fails */ }
}

function showRecoveryBanner(saved) {
  // Avoid duplicate banners
  $("#recovery-banner")?.remove();
  const cfg = saved.config || {};
  const turn = (saved.current_turn ?? 0) + 1;
  const total = cfg.loop_limit || "?";
  const banner = document.createElement("div");
  banner.id = "recovery-banner";
  banner.className = "recovery-banner";
  banner.innerHTML = `
    <span class="meta">Previous run was interrupted at <strong>round ${turn}/${total}</strong> · status <strong>${escapeHtml(saved.status || "unknown")}</strong>. The transcript is preserved — LM Studio context is gone, so it can't be auto-resumed.</span>`;
  const viewBtn = document.createElement("button");
  viewBtn.className = "ghost"; viewBtn.textContent = "View transcript";
  viewBtn.addEventListener("click", () => {
    replaySession({ ...saved, id: saved.run_id || "interrupted" });
    banner.remove();
  });
  const discardBtn = document.createElement("button");
  discardBtn.className = "danger"; discardBtn.textContent = "Discard";
  discardBtn.addEventListener("click", async () => {
    try { await api("/api/state", { method: "DELETE" }); }
    catch (e) { toast(`Discard failed: ${e.message}`, "error"); return; }
    banner.remove();
    toast("Discarded.", "success");
  });
  banner.appendChild(viewBtn); banner.appendChild(discardBtn);
  $(".main").prepend(banner);
}

// ── User-config persistence (debounced save) ───────────────────────────────

let _saveConfigTimer = null;
function scheduleConfigSave() {
  clearTimeout(_saveConfigTimer);
  _saveConfigTimer = setTimeout(saveUserConfig, 500);
}
async function saveUserConfig() {
  const updates = {
    theme: $("#cfg-theme").value,
    alpha_name: $("#alpha-name").value,
    alpha_model: $("#alpha-model").value,
    alpha_persona: $("#alpha-persona").value,
    bravo_name: $("#bravo-name").value,
    bravo_model: $("#bravo-model").value,
    bravo_persona: $("#bravo-persona").value,
    loop_limit: parseInt($("#loop-limit").value, 10) || 3,
    pause_after_each_turn: $("#pause-each-turn").checked,
    auto_retry: parseInt($("#auto-retry").value, 10) || 0,
    charlie_enabled: $("#charlie-enabled").checked,
    charlie_model: $("#charlie-model").value,
    intel_collab_directive: $("#intel-collab").checked,
    intel_anti_rambling: $("#intel-rambling").checked,
    intel_anti_yes_man: $("#intel-yesman").checked,
    intel_agreement_threshold: parseInt($("#intel-agreement-threshold").value, 10) || 2,
  };
  try { await api("/api/config", { method: "PATCH", body: updates }); }
  catch (e) { console.warn("config save failed:", e.message); }
}

async function loadUserConfig() {
  try {
    const cfg = await api("/api/config");
    if (!cfg) return;
    if (cfg.theme != null) $("#cfg-theme").value = cfg.theme;
    if (cfg.alpha_name) $("#alpha-name").value = cfg.alpha_name;
    if (cfg.alpha_persona != null) $("#alpha-persona").value = cfg.alpha_persona;
    if (cfg.bravo_name) $("#bravo-name").value = cfg.bravo_name;
    if (cfg.bravo_persona != null) $("#bravo-persona").value = cfg.bravo_persona;
    if (cfg.loop_limit) $("#loop-limit").value = cfg.loop_limit;
    $("#pause-each-turn").checked = !!cfg.pause_after_each_turn;
    if (cfg.auto_retry != null) $("#auto-retry").value = cfg.auto_retry;
    $("#charlie-enabled").checked = !!cfg.charlie_enabled;
    $("#charlie-model").disabled = !cfg.charlie_enabled;
    $("#intel-collab").checked = cfg.intel_collab_directive !== false;
    $("#intel-rambling").checked = cfg.intel_anti_rambling !== false;
    $("#intel-yesman").checked = cfg.intel_anti_yes_man !== false;
    if (cfg.intel_agreement_threshold) $("#intel-agreement-threshold").value = cfg.intel_agreement_threshold;
    // Stash desired model selections to apply once /api/health populates the dropdowns.
    state._pendingAlphaModel = cfg.alpha_model || "";
    state._pendingBravoModel = cfg.bravo_model || "";
    state._pendingCharlieModel = cfg.charlie_model || "";
  } catch (e) { console.warn("config load failed:", e.message); }
}

function applyPendingModelSelection() {
  for (const [id, key] of [["alpha-model","_pendingAlphaModel"], ["bravo-model","_pendingBravoModel"], ["charlie-model","_pendingCharlieModel"]]) {
    const want = state[key];
    if (!want) continue;
    const el = $("#" + id);
    if (el && el.querySelector(`option[value="${cssEscape(want)}"]`)) {
      el.value = want;
      updateHostBadge(id, modelById(want));
    }
    // One-shot: clear the stash whether or not we found the option, so the next
    // polling tick of refreshHealth() doesn't re-stamp this over the user's pick.
    state[key] = "";
  }
  validateModelClash();
}

// ── Presets ────────────────────────────────────────────────────────────────

async function refreshPresets() {
  try {
    const data = await api("/api/presets");
    state.presets = data.presets || [];
    renderPresets();
  } catch (e) { toast(`Presets: ${e.message}`, "error"); }
}

function renderPresets() {
  const ul = $("#preset-list");
  ul.innerHTML = "";
  if (state.presets.length === 0) {
    ul.innerHTML = '<li><em>No presets saved.</em></li>';
    return;
  }
  for (const p of state.presets) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="row">
        <div><strong>${escapeHtml(p.name)}</strong>
          <div class="meta">${(p.config?.agents || []).map(a => a.name).join(" · ")}</div></div>
        <div class="ops">
          <button class="ghost" data-act="load">Load</button>
          <button class="ghost" data-act="rename">Rename</button>
          <button class="ghost" data-act="dup">Dup</button>
          <button class="ghost" data-act="export">Export</button>
          <button class="ghost" data-act="del">×</button>
        </div>
      </div>`;
    li.querySelector('[data-act="load"]').addEventListener("click", () => {
      applyConfig(p.config); $$(".tab")[0].click();
      toast(`Loaded "${p.name}"`, "success");
    });
    li.querySelector('[data-act="rename"]').addEventListener("click", async () => {
      const name = prompt("Rename preset:", p.name);
      if (!name) return;
      await api(`/api/presets/${p.id}`, { method: "PATCH", body: { name } });
      refreshPresets();
    });
    li.querySelector('[data-act="dup"]').addEventListener("click", async () => {
      await api(`/api/presets/${p.id}/duplicate`, { method: "POST" });
      refreshPresets();
    });
    li.querySelector('[data-act="export"]').addEventListener("click", async () => {
      const data = await api(`/api/presets/${p.id}/export`);
      downloadJson(`${p.name}.preset.json`, data);
    });
    li.querySelector('[data-act="del"]').addEventListener("click", async () => {
      if (!confirm(`Delete preset "${p.name}"?`)) return;
      await api(`/api/presets/${p.id}`, { method: "DELETE" });
      refreshPresets();
    });
    ul.appendChild(li);
  }
}

async function savePreset() {
  const name = prompt("Preset name:");
  if (!name) return;
  await api("/api/presets", { method: "POST", body: { name, config: readConfig() } });
  toast(`Saved "${name}"`, "success");
  refreshPresets();
}

function importPreset() {
  const input = document.createElement("input");
  input.type = "file"; input.accept = "application/json";
  input.addEventListener("change", async () => {
    const f = input.files?.[0]; if (!f) return;
    try {
      const text = await f.text();
      const data = JSON.parse(text);
      await api("/api/presets/import", { method: "POST", body: data });
      toast("Imported.", "success");
      refreshPresets();
    } catch (e) { toast(`Import failed: ${e.message}`, "error"); }
  });
  input.click();
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// ── Sessions ───────────────────────────────────────────────────────────────

async function refreshSessions(query = "") {
  try {
    const data = await api(`/api/sessions${query ? `?q=${encodeURIComponent(query)}` : ""}`);
    state.sessions = data.sessions || [];
    renderSessions();
  } catch (e) { toast(`Sessions: ${e.message}`, "error"); }
}

function renderSessions() {
  const ul = $("#session-list");
  ul.innerHTML = "";
  if (state.sessions.length === 0) {
    ul.innerHTML = '<li><em>No past runs.</em></li>';
    return;
  }
  for (const s of state.sessions) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="row">
        <div><strong>${escapeHtml(s.theme || "(no theme)")}</strong>
          <div class="meta">${s.status} · ${s.turns} turns · ${formatDate(s.ended_at)}</div></div>
        <div class="ops">
          <button class="ghost" data-act="view">View</button>
          <button class="ghost" data-act="export">Export</button>
          <button class="ghost" data-act="del">×</button>
        </div>
      </div>`;
    li.querySelector('[data-act="view"]').addEventListener("click", async () => {
      const full = await api(`/api/sessions/${s.id}`);
      replaySession(full);
    });
    li.querySelector('[data-act="export"]').addEventListener("click", async () => {
      const full = await api(`/api/sessions/${s.id}`);
      const safeTheme = (s.theme || "session").slice(0, 40).replace(/[^a-z0-9-]+/gi, "_");
      downloadJson(`${safeTheme}_${s.id}.json`, full);
    });
    li.querySelector('[data-act="del"]').addEventListener("click", async () => {
      if (!confirm("Delete this session?")) return;
      await api(`/api/sessions/${s.id}`, { method: "DELETE" });
      refreshSessions($("#history-search").value);
    });
    ul.appendChild(li);
  }
}

function replaySession(data) {
  clearDialogue();
  const transcript = data.transcript || [];
  let turn = 0; let lastAgent = null;
  for (const e of transcript) {
    if (e.agent === "orchestrator") continue;
    if (e.agent === "user_nudge") {
      // Distinguish intel-injected nudges (have intel_reason) from manual user nudges.
      if (e.intel_reason) {
        appendNudgeCard(e.intel_reason, e.content || "", null);
      } else {
        const div = document.createElement("div");
        div.className = "turn-card";
        div.innerHTML = `<div class="turn-header"><span class="turn-agent">User nudge</span></div>
                         <div class="turn-content"></div>`;
        div.querySelector(".turn-content").textContent = e.content || "";
        $("#dialogue").appendChild(div);
      }
      continue;
    }
    if (lastAgent && e.agent === lastAgent) turn += 1;
    lastAgent = e.agent;
    const entry = ensureCard(turn, e.agent, e.model || "");
    entry.contentEl.textContent = e.content || "";
    if (e.latency_ms != null) finalizeCard(turn, e.agent, e.latency_ms, (e.content || "").split(/\s+/).length);
    entry.card.classList.remove("active");
  }
  toast(`Loaded session ${data.id}`, "success");
}

// ── Charlie summary ────────────────────────────────────────────────────────

let _lastSummaryText = "";

async function loadSummary() {
  try {
    const data = await api(`/api/charlie/file?path=summary.md`);
    const v = $("#summary-viewer");
    if (data.content == null) {
      v.innerHTML = `<em>${data.note || "Summary unavailable"}</em>`;
      $("#btn-download-summary").disabled = true;
      _lastSummaryText = "";
      return;
    }
    _lastSummaryText = data.content;
    v.innerHTML = "<pre></pre>";
    v.querySelector("pre").textContent = data.content;
    $("#summary-status").textContent = `${data.size} B`;
    $("#btn-download-summary").disabled = false;
  } catch (e) {
    $("#summary-viewer").innerHTML = `<em>No summary.md yet (${escapeHtml(e.message)}).</em>`;
    $("#btn-download-summary").disabled = true;
    _lastSummaryText = "";
  }
}

async function regenerateSummary() {
  const btn = $("#btn-regenerate-summary");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Summarizing…";
  try {
    await api("/api/charlie/summarize", { method: "POST", body: {
      model: $("#charlie-model").value || null,
    }});
    toast("Summary regenerated.", "success");
    // charlie_done event will repaint via loadSummary()
  } catch (e) {
    toast(`Summarize failed: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function downloadSummary() {
  if (!_lastSummaryText) return;
  const blob = new Blob([_lastSummaryText], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "summary.md";
  a.click();
  URL.revokeObjectURL(url);
}

function setRegenerateEnabled(enabled) {
  $("#btn-regenerate-summary").disabled = !enabled;
}

// ── Errors ─────────────────────────────────────────────────────────────────

async function refreshErrors() {
  const cat = $("#errors-filter").value;
  const url = `/api/errors${cat ? `?category=${encodeURIComponent(cat)}` : ""}`;
  try {
    const data = await api(url);
    state.errors = data.errors || [];
    state.errorStats = data.stats || { total: 0, by_category: {} };
    renderErrors();
  } catch (e) { toast(`Errors: ${e.message}`, "error"); }
}

function renderErrors() {
  const ul = $("#error-list");
  const stats = $("#errors-stats");
  const totalText = state.errorStats.total === 0 ? "no errors logged"
    : `${state.errorStats.total} total · ` + Object.entries(state.errorStats.by_category)
        .map(([k, v]) => `${k}: ${v}`).join(" · ");
  stats.textContent = totalText;
  ul.innerHTML = "";
  if (state.errors.length === 0) {
    ul.innerHTML = '<li><em>No errors to display.</em></li>';
    return;
  }
  for (const e of state.errors) {
    const li = document.createElement("li");
    const ctxStr = Object.keys(e.context || {}).length === 0
      ? "" : JSON.stringify(e.context, null, 2);
    li.innerHTML = `
      <div class="row">
        <div style="flex:1; min-width:0;">
          <div>
            <span class="err-cat ${escapeHtml(e.category)}">${escapeHtml(e.category)}</span>
            <span class="meta">${formatDate(e.timestamp)} · ${escapeHtml(e.id)}</span>
          </div>
          <div class="err-msg">${escapeHtml(e.message)}</div>
          ${ctxStr ? `<div class="err-ctx">${escapeHtml(ctxStr)}</div>` : ""}
        </div>
      </div>`;
    ul.appendChild(li);
  }
}

async function clearErrors() {
  if (!confirm("Clear all errors from the in-memory ring? (Disk log is preserved.)")) return;
  try {
    await api("/api/errors", { method: "DELETE" });
    toast("Cleared.", "success");
    refreshErrors();
    clearErrorBadge();
  } catch (e) { toast(`Clear failed: ${e.message}`, "error"); }
}

function bumpErrorBadge() {
  state.unseenErrors += 1;
  const badge = $("#errors-badge");
  badge.textContent = state.unseenErrors > 99 ? "99+" : String(state.unseenErrors);
  badge.hidden = false;
}

function clearErrorBadge() {
  state.unseenErrors = 0;
  $("#errors-badge").hidden = true;
}

// ── WebSocket event handlers ───────────────────────────────────────────────

on("hello", (m) => {
  if (m.state?.config?.theme) applyConfig(m.state.config);
});
on("run_started", (m) => {
  state.currentRunId = m.run_id;
  state.totalTurns = m.config?.loop_limit || 1;
  setRunControls("running");
  setRegenerateEnabled(false);
});
on("turn_started", (m) => {
  showProgress(m.turn, m.agent_name, m.total_turns);
  ensureCard(m.turn, m.agent_name, m.model);
});
on("turn_chunk", (m) => appendToken(m.turn, m.agent_name, m.token));
on("turn_done", (m) => finalizeCard(m.turn, m.agent_name, m.latency_ms || 0, m.token_count || 0));
on("agent_error", (m) => {
  if (m.auto_retry) { toast(m.message, "error"); return; }
  setRunControls("awaiting_user");
  showAgentError(m.turn, m.agent_name, m.error_class || "error", m.message || "");
});
on("run_paused", () => { setRunControls("paused"); showPauseBanner(); });
on("run_resumed", () => setRunControls("running"));
on("run_done", (m) => {
  setRunControls("idle");
  $("#run-progress").classList.add("hidden");
  // Defensive: any card still flagged active gets settled on run completion.
  document.querySelectorAll(".turn-card.active").forEach((c) => c.classList.remove("active"));
  toast(`Run finished: ${m.status}`, m.status === "done" ? "success" : "info");
  setRegenerateEnabled(true);
});
on("charlie_started", () => {
  toast("Charlie summarizing…", "info");
  $("#summary-status").textContent = "Summarizing…";
});
on("charlie_progress", (m) => {
  const phaseText = {
    truncated: `Truncated ${m.dropped} turn(s) to fit context (limit ${m.token_limit} tokens)…`,
    calling_llm: `Calling ${m.model || "model"}…`,
    writing: "Writing summary.md…",
  }[m.phase] || `Working: ${m.phase}`;
  $("#summary-status").textContent = phaseText;
});
on("charlie_done", (m) => {
  const note = m.truncated ? ` (${m.dropped_turns} turns truncated)` : "";
  toast(`Summary ready${note}.`, "success");
  $("#summary-status").textContent = `Ready${note}.`;
  loadSummary();
});
on("charlie_error", (m) => {
  toast(`Charlie: ${m.error}`, "error");
  $("#summary-status").textContent = "Failed.";
});
on("dialogue_nudge", (m) => appendNudgeCard(m.reason, m.content, m.turn));
on("error_logged", (m) => {
  // Live-update the errors list if it's currently visible; always bump the badge.
  const onErrorsTab = document.querySelector('.tab.active')?.dataset.tab === "errors";
  if (onErrorsTab) refreshErrors();
  else bumpErrorBadge();
});

// ── Setup ──────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function formatDate(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = (e) => {
    try { dispatch(JSON.parse(e.data)); }
    catch (err) { console.error("Bad WS msg", err); }
  };
  ws.onclose = () => setTimeout(connectWebSocket, 1000);
  ws.onerror = () => ws.close();
  return ws;
}

document.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  $("#btn-test").addEventListener("click", () => refreshHealth({interactive: true}));
  $("#btn-start").addEventListener("click", startRun);
  $("#btn-stop").addEventListener("click", stopRun);
  $("#btn-pause").addEventListener("click", pauseRun);
  $("#btn-save-preset").addEventListener("click", savePreset);
  $("#btn-import-preset").addEventListener("click", importPreset);
  $("#charlie-enabled").addEventListener("change", (e) => {
    $("#charlie-model").disabled = !e.target.checked;
  });
  $("#btn-regenerate-summary").addEventListener("click", regenerateSummary);
  $("#btn-download-summary").addEventListener("click", downloadSummary);
  // Persist UI prefs as the user changes anything.
  const persistedIds = [
    "cfg-theme", "alpha-name", "alpha-model", "alpha-persona",
    "bravo-name", "bravo-model", "bravo-persona",
    "loop-limit", "pause-each-turn", "auto-retry",
    "charlie-enabled", "charlie-model",
    "intel-collab", "intel-rambling", "intel-yesman", "intel-agreement-threshold",
  ];
  for (const id of persistedIds) {
    const el = $("#" + id);
    if (!el) continue;
    el.addEventListener("change", scheduleConfigSave);
    if (el.tagName === "TEXTAREA" || el.type === "text" || el.type === "number") {
      el.addEventListener("input", scheduleConfigSave);
    }
  }
  ["alpha-model", "bravo-model", "charlie-model"].forEach((id) =>
    $("#" + id).addEventListener("change", () => {
      // Belt-and-suspenders: drop any pending stash so the polling tick can never
      // override the user's manual pick.
      const stashKey = "_pending" + id.split("-")[0].replace(/^./, c => c.toUpperCase()) + "Model";
      state[stashKey] = "";
      validateModelClash();
      updateHostBadge(id, modelById($("#" + id).value));
      $('[data-bind="alphaModel"]').textContent = $("#alpha-model").value || "—";
      $('[data-bind="bravoModel"]').textContent = $("#bravo-model").value || "—";
    }));
  $("#history-search").addEventListener("input", (e) => refreshSessions(e.target.value));
  $("#btn-refresh-errors").addEventListener("click", refreshErrors);
  $("#btn-clear-errors").addEventListener("click", clearErrors);
  $("#errors-filter").addEventListener("change", refreshErrors);

  setupDialogueScrollTracking();
  setRunControls("idle");
  $("#handoff-banner-dismiss")?.addEventListener("click", () => {
    const b = $("#handoff-banner");
    if (b) b.hidden = true;
  });
  await loadUserConfig();
  await refreshHealth();          // populates dropdowns + applies pending model selection
  await checkRecoverableState();  // surface banner if previous run was interrupted
  await checkPersonaHandoff();    // prefill from prompt-enhancer if a handoff is staged
  setInterval(refreshHealth, 10000);
  connectWebSocket();
});
