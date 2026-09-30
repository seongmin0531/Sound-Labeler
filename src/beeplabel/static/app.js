"use strict";
// Beep Labeler front-end: one beep region per file, snapped to the 100 ms frame grid.
// Region = [s, e) in frame indices, or null (= no beep). Every change is saved under the
// logged-in user id and recorded in the server-side audit log.

const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
async function api(url, opts = {}) {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opts });
  if (r.status === 401 && !url.startsWith("/api/auth")) { showLogin(); throw new Error("로그인이 필요합니다"); }
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

const S = {
  me: null, settings: null, files: [], idx: -1, view: null,
  region: null, reviewed: false, memo: "", hasLabel: false,
  saved: null,              // JSON of last saved state
  saveTimer: null, saving: null,
  v0: 0, v1: 1, cursor: 0, hoverX: -1,
  undo: [], redo: [], specImg: null, gain: 1, big: false, playEnd: null,
};

// ================= theme =================
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("bl-theme", t); } catch (e) {}
  readColors(); draw();
}
function toggleTheme() {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(t);
  if (S.me) api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ theme: t }) }).catch(() => {});
}
document.querySelectorAll(".theme-btn").forEach((b) => (b.onclick = toggleTheme));
let C = {};
function readColors() {
  const cs = getComputedStyle(document.documentElement);
  for (const k of ["bg", "track", "wave", "grid", "text", "frame", "frame-on", "region", "region-line", "auto", "cursor", "play"])
    C[k] = cs.getPropertyValue("--c-" + k).trim();
}
readColors();

// ================= login =================
let registerMode = false;
async function showLogin() {
  $("app-view").hidden = true; $("login-view").hidden = false;
  $("audio").pause();
  const p = await fetch("/api/public").then((r) => r.json()).catch(() => ({}));
  $("login-version").textContent = p.version ? "v" + p.version : "";
  $("tab-register").hidden = p.has_users && !p.allow_register;
  setRegisterMode(!p.has_users);
  $("lg-hint").textContent = p.has_users ? "" : "첫 번째로 가입하는 계정이 관리자가 됩니다.";
  $("lg-id").focus();
}
function setRegisterMode(on) {
  registerMode = on;
  $("tab-login").classList.toggle("active", !on); $("tab-register").classList.toggle("active", on);
  $("lg-name-row").hidden = !on;
  $("lg-submit").textContent = on ? "가입하고 시작" : "로그인";
  $("lg-pw").autocomplete = on ? "new-password" : "current-password";
  $("lg-error").textContent = "";
}
$("tab-login").onclick = () => setRegisterMode(false);
$("tab-register").onclick = () => setRegisterMode(true);
$("login-form").onsubmit = async (ev) => {
  ev.preventDefault();
  $("lg-error").textContent = "";
  try {
    await api(registerMode ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ user_id: $("lg-id").value.trim(), password: $("lg-pw").value, display_name: $("lg-name").value.trim() }),
    });
    $("lg-pw").value = "";
    await startApp();
  } catch (e) { $("lg-error").textContent = e.message; }
};

async function logout() {
  await flushSave();
  await api("/api/auth/logout", { method: "POST", body: JSON.stringify({ saved: true }) }).catch(() => {});
  S.me = null; S.view = null; S.idx = -1;
  showLogin();
}
$("btn-logout").onclick = logout;

// ================= boot =================
async function startApp() {
  const m = await api("/api/me");
  S.me = m.user; S.settings = m.settings;
  $("login-view").hidden = true; $("app-view").hidden = false;
  applyTheme(S.me.theme || "dark");
  $("version").textContent = "v" + m.version;
  $("who").textContent = `${S.me.display_name} (${S.me.id})${S.me.is_admin ? " · 관리자" : ""}`;
  renderDataset();
  resize();
  await loadFiles();
  if (!S.settings.dataset_path) { openSettings(); return; }
  const last = S.files.find((f) => f.file === S.me.last_file);
  const first = last || S.files.find((f) => f.status !== "reviewed") || S.files[0];
  if (first) openFile(first.idx);
}
function renderDataset() {
  const s = S.settings;
  $("dataset-label").textContent = s.dataset_path ? `📁 ${s.dataset_path} (${s.n_files}개)` : "📁 WAV 폴더 미지정";
  $("dataset-label").title = s.dataset_path || "";
}
async function loadFiles() {
  S.files = await api("/api/files");
  renderFiles();
  if (!S.files.length) {
    S.view = null; draw();
    $("file-title").textContent = S.settings.dataset_path ? "폴더에 WAV 파일이 없습니다" : "설정에서 WAV 데이터 폴더를 지정하세요";
  }
}

// ================= layout / geometry =================
const canvas = $("view"), ctx = canvas.getContext("2d");
let W = 0, H = 0, DPR = 1, P = {};
function layoutPanels() {
  const axis = 20, frames = 26, wave = S.big ? 110 : 56;
  const spec = Math.max(60, H - axis - frames - wave - 6);
  P = { wave: [0, wave], spec: [wave + 2, wave + 2 + spec], frames: [wave + 4 + spec, wave + 4 + spec + frames] };
  P.axis = [P.frames[1] + 2, P.frames[1] + 2 + axis];
}
function resize() {
  const r = canvas.getBoundingClientRect();
  DPR = window.devicePixelRatio || 1; W = r.width; H = r.height;
  canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
  layoutPanels(); draw();
}
window.addEventListener("resize", resize);
new ResizeObserver(resize).observe($("canvas-wrap"));

const xOf = (s) => ((s - S.v0) / (S.v1 - S.v0)) * W;
const sOf = (x) => Math.round(S.v0 + (x / W) * (S.v1 - S.v0));
const nSamp = () => (S.view ? S.view.num_samples : 0);
const clampS = (s) => Math.max(0, Math.min(nSamp(), s));
const nFrames = () => S.view.frame_starts.length;
const fmtS = (s) => (s / S.view.sr).toFixed(3) + "s";
const region = (y) => Object.keys(P).find((k) => y >= P[k][0] && y < P[k][1]) || null;
function lowerBound(a, v) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < v) lo = m + 1; else hi = m; } return lo; }
function frameAt(s) { const i = lowerBound(S.view.frame_ends, s + 1); return Math.min(i, nFrames() - 1); }
function boundary(k) { return k < nFrames() ? S.view.frame_starts[k] : S.view.frame_ends[nFrames() - 1]; }
function nearestBoundary(s) {
  const i = frameAt(s);
  return s - S.view.frame_starts[i] < S.view.frame_ends[i] - s ? i : i + 1;
}
function secOfFrame(k) { return boundary(k) / S.view.sr; }
function regionText(r) {
  if (!r) return S.hasLabel || S.reviewed ? "비프 없음" : "지정 안 됨";
  return `${secOfFrame(r[0]).toFixed(1)} ~ ${secOfFrame(r[1]).toFixed(1)}초`;
}

// ================= spectrogram image =================
const LUT = (() => {
  const st = [[0, [8, 8, 20]], [0.25, [40, 20, 90]], [0.5, [150, 40, 110]], [0.75, [245, 120, 40]], [1, [252, 250, 180]]];
  const lut = new Uint8ClampedArray(768);
  for (let i = 0; i < 256; i++) {
    const t = i / 255; let k = 0; while (k < st.length - 2 && t > st[k + 1][0]) k++;
    const [t0, c0] = st[k], [t1, c1] = st[k + 1], u = (t - t0) / (t1 - t0);
    for (let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + u * (c1[j] - c0[j]);
  }
  return lut;
})();
function buildSpec() {
  const sp = S.view.spec, raw = Uint8Array.from(atob(sp.data), (c) => c.charCodeAt(0));
  const off = document.createElement("canvas"); off.width = sp.n_time; off.height = sp.n_freq;
  const oc = off.getContext("2d"), img = oc.createImageData(sp.n_time, sp.n_freq);
  for (let t = 0; t < sp.n_time; t++) for (let f = 0; f < sp.n_freq; f++) {
    const v = raw[t * sp.n_freq + f], p = ((sp.n_freq - 1 - f) * sp.n_time + t) * 4;
    img.data[p] = LUT[v * 3]; img.data[p + 1] = LUT[v * 3 + 1]; img.data[p + 2] = LUT[v * 3 + 2]; img.data[p + 3] = 255;
  }
  oc.putImageData(img, 0, 0); S.specImg = off;
}

// ================= drawing =================
function draw() {
  if (!W) return;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
  if (!S.view) return;
  const [wy0, wy1] = P.wave, [sy0, sy1] = P.spec, [fy0, fy1] = P.frames, [ay0] = P.axis;
  const sp = S.view.spec;

  // spectrogram
  if (S.specImg) {
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(S.specImg, S.v0 / sp.hop, 0, (S.v1 - S.v0) / sp.hop, sp.n_freq, 0, sy0, W, sy1 - sy0);
    ctx.font = "10px system-ui"; ctx.fillStyle = "rgba(255,255,255,.7)";
    const step = (sy1 - sy0) > 200 ? 1000 : 2000;
    for (let f = step; f < sp.fmax; f += step) ctx.fillText(`${f / 1000}k`, 3, sy1 - (f / sp.fmax) * (sy1 - sy0) + 3);
  }

  // waveform
  ctx.fillStyle = C.track; ctx.fillRect(0, wy0, W, wy1 - wy0);
  const wv = S.view.wave, mid = (wy0 + wy1) / 2, amp = (wy1 - wy0) / 2 * S.gain;
  ctx.fillStyle = C.wave;
  for (let x = 0; x < W; x++) {
    const a = Math.floor(sOf(x) / wv.step), b = Math.max(a + 1, Math.floor(sOf(x + 1) / wv.step));
    if (b <= 0 || a >= wv.min.length) continue;
    let lo = 1, hi = -1;
    for (let i = Math.max(0, a); i < Math.min(b, wv.min.length); i++) { if (wv.min[i] < lo) lo = wv.min[i]; if (wv.max[i] > hi) hi = wv.max[i]; }
    const y0 = Math.max(wy0, mid - hi * amp), y1 = Math.min(wy1, mid - lo * amp);
    ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
  }

  // frame grid
  const st = S.view.frame_starts, en = S.view.frame_ends;
  const f0 = frameAt(S.v0), f1 = frameAt(Math.max(0, S.v1 - 1));
  const fw = xOf(en[0]) - xOf(st[0]);
  if (fw > 5) {
    ctx.strokeStyle = C.grid;
    for (let i = f0; i <= f1; i++) { const x = Math.round(xOf(st[i])) + 0.5; ctx.beginPath(); ctx.moveTo(x, wy0); ctx.lineTo(x, fy1); ctx.stroke(); }
  }

  // auto suggestion (dashed)
  const au = S.view.auto;
  if (au && au.start_frame != null) {
    const x0 = xOf(boundary(au.start_frame)), x1 = xOf(boundary(au.end_frame));
    ctx.strokeStyle = C.auto; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5;
    ctx.strokeRect(x0 + 1, wy0 + 1, x1 - x0 - 2, sy1 - wy0 - 2);
    ctx.setLineDash([]); ctx.lineWidth = 1;
    ctx.fillStyle = C.auto; ctx.font = "10px system-ui"; ctx.fillText("자동 제안", x0 + 4, sy1 - 5);
  }

  // user region
  if (S.region) {
    const x0 = xOf(boundary(S.region[0])), x1 = xOf(boundary(S.region[1]));
    ctx.fillStyle = C.region; ctx.fillRect(x0, wy0, x1 - x0, fy1 - wy0);
    ctx.strokeStyle = C["region-line"]; ctx.lineWidth = 2;
    for (const x of [x0, x1]) { ctx.beginPath(); ctx.moveTo(x, wy0); ctx.lineTo(x, fy1); ctx.stroke(); }
    ctx.lineWidth = 1;
    ctx.fillStyle = C["region-line"]; ctx.font = "bold 11px system-ui";
    ctx.fillText(regionText(S.region), Math.max(2, x0 + 4), wy0 + 12);
  }

  // frame strip
  ctx.fillStyle = C.track; ctx.fillRect(0, fy0, W, fy1 - fy0);
  for (let i = f0; i <= f1; i++) {
    const x0 = xOf(st[i]), w = Math.max(1, xOf(en[i]) - x0 - (fw > 4 ? 1 : 0));
    const on = S.region && i >= S.region[0] && i < S.region[1];
    ctx.fillStyle = on ? C["frame-on"] : C.frame; ctx.fillRect(x0, fy0 + 2, w, fy1 - fy0 - 4);
    if (fw > 22) { ctx.fillStyle = on ? "#fff" : C.text; ctx.font = "9px system-ui"; ctx.fillText(i, x0 + 3, fy1 - 8); }
  }

  // time axis
  const span = (S.v1 - S.v0) / S.view.sr;
  const tick = [0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60].find((s) => s * W / span >= 60) || 60;
  ctx.fillStyle = C.text; ctx.strokeStyle = C.grid; ctx.font = "10px system-ui";
  for (let t = Math.ceil(S.v0 / S.view.sr / tick - 1e-9) * tick; t <= S.v1 / S.view.sr + 1e-9; t += tick) {
    const x = xOf(t * S.view.sr);
    ctx.beginPath(); ctx.moveTo(x, ay0); ctx.lineTo(x, ay0 + 5); ctx.stroke();
    ctx.fillText(tick < 1 ? t.toFixed(1) + "s" : t.toFixed(0) + "s", x + 2, ay0 + 15);
  }

  // cursor / playhead / hover
  const vline = (x, c, w = 1) => { ctx.strokeStyle = c; ctx.lineWidth = w; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, fy1); ctx.stroke(); ctx.lineWidth = 1; };
  vline(xOf(S.cursor) + 0.5, C.cursor);
  if (!audio.paused) vline(xOf(audio.currentTime * S.view.sr), C.play, 2);
  if (S.hoverX >= 0) vline(S.hoverX + 0.5, C.auto);
}

// ================= panels =================
function renderFiles() {
  const q = $("filter").value.toLowerCase(), sf = $("status-filter").value;
  const ul = $("files"); ul.innerHTML = "";
  for (const f of S.files) {
    if (q && !f.file.toLowerCase().includes(q)) continue;
    if (sf && f.status !== sf) continue;
    const li = document.createElement("li");
    li.dataset.idx = f.idx;
    li.className = f.idx === S.idx ? "active" : "";
    li.innerHTML = `<span class="dot ${f.status}"></span><span class="name" title="${esc(f.file)}">${esc(f.file)}</span>` +
      `<span class="dur">${f.duration.toFixed(1)}s</span>` +
      (f.region || f.n_users ? `<span class="reg">${esc(f.region)}${f.n_users ? ` · 작업자 ${f.n_users}명` : ""}</span>` : "");
    li.onclick = () => openFile(f.idx);
    ul.appendChild(li);
  }
  const done = S.files.filter((f) => f.status === "reviewed").length;
  $("progress").textContent = `내 검수 ${done} / ${S.files.length}`;
}

function refreshPanels() {
  if (!S.view) return;
  $("region-text").textContent = regionText(S.region);
  $("region-detail").textContent = S.region
    ? `프레임 ${S.region[0]}–${S.region[1] - 1} (${S.region[1] - S.region[0]}개 × ${S.view.frame_ms}ms) · 샘플 [${boundary(S.region[0])}, ${boundary(S.region[1])})`
    : "";
  const st = S.reviewed ? `<span style="color:var(--ok)">● 검수 완료</span>` : S.hasLabel ? `<span style="color:var(--draft)">● 작업 중</span>` : `<span class="muted">○ 미작업</span>`;
  $("file-state").innerHTML = st;
  const au = S.view.auto;
  $("auto-info").innerHTML = au && au.start_frame != null
    ? `${au.start_s.toFixed(1)} ~ ${au.end_s.toFixed(1)}초 <span class="muted small">(파란 점선)</span>`
    : "탐지된 비프 없음";
  $("btn-play-seg").disabled = !S.region;
  updateHover();
}
function updateHover() {
  if (!S.view) return;
  const s = clampS(S.hoverX >= 0 ? sOf(S.hoverX) : S.cursor), i = frameAt(s);
  $("statusbar").textContent = `커서 ${fmtS(s)} · 프레임 #${i} (${secOfFrame(i).toFixed(1)}–${secOfFrame(i + 1).toFixed(1)}s) · 보기 ${fmtS(S.v0)}–${fmtS(S.v1)} · ${S.view.sr} Hz`;
}
function setSaveState(t, warn = false) { $("save-state").textContent = t; $("save-state").style.color = warn ? "var(--warn)" : ""; }

async function loadHistory() {
  if (S.idx < 0) return;
  const h = await api(`/api/files/${S.idx}/history`).catch(() => null);
  if (!h) return;
  const others = h.labels.filter((l) => l.user_id !== S.me.id);
  $("others").innerHTML = others.length
    ? others.map((l) => `<div class="others-row"><b>${esc(l.user_id)}</b><span>${esc(l.region)}${l.reviewed ? " ✓" : ""}</span></div>` +
      (l.memo ? `<div class="muted small">“${esc(l.memo)}”</div>` : "")).join("")
    : "없음";
  $("history").innerHTML = h.history.length ? h.history.map((a) =>
    `<li><span class="t">${esc(a.ts)} · <b>${esc(a.user_id)}</b></span>${esc(a.action)}` +
    (a.before || a.after ? `: ${a.before ? esc(a.before) + " → " : ""}${esc(a.after || "")}` : "") + `</li>`).join("")
    : `<li class="muted">아직 없음</li>`;
}

// ================= open / save =================
function stateJSON() { return JSON.stringify({ r: S.region, v: S.reviewed, m: S.memo, h: S.hasLabel }); }
function isDirty() { return !!S.view && S.saved !== null && stateJSON() !== S.saved; }

async function openFile(idx) {
  if (idx < 0 || idx >= S.files.length) return;
  await flushSave();
  audio.pause();
  S.idx = idx; renderFiles();
  const f = S.files[idx];
  $("file-title").textContent = f.file;
  setSaveState("불러오는 중…");
  let view, lab;
  try {
    [view, lab] = await Promise.all([api(`/api/files/${idx}/view`), api(`/api/files/${idx}/label`)]);
  } catch (e) { setSaveState("불러오기 실패: " + e.message, true); return; }
  if (S.idx !== idx) return;  // user moved on meanwhile
  S.view = view; S.undo = []; S.redo = [];
  const l = lab.label;
  S.hasLabel = !!l;
  S.region = l && l.start_frame != null ? [l.start_frame, l.end_frame] : null;
  S.reviewed = !!(l && l.reviewed); S.memo = l ? l.memo : "";
  $("memo").value = S.memo;
  S.saved = stateJSON();
  S.v0 = 0; S.v1 = view.num_samples; S.cursor = 0;
  audio.src = `/api/files/${idx}/audio`;
  buildSpec(); refreshPanels(); draw();
  setSaveState(l ? `마지막 저장 ${l.updated_at}` : "아직 저장된 라벨 없음");
  const li = $("files").querySelector("li.active"); if (li) li.scrollIntoView({ block: "nearest" });
  api("/api/me/prefs", { method: "PUT", body: JSON.stringify({ last_file: f.file }) }).catch(() => {});
  loadHistory();
}

function scheduleSave() {
  clearTimeout(S.saveTimer);
  if (!isDirty()) return;
  setSaveState("변경됨 · 자동 저장 대기…");
  S.saveTimer = setTimeout(() => save(), 800);
}
async function flushSave() {
  clearTimeout(S.saveTimer);
  if (S.saving) await S.saving;
  if (isDirty()) await save();
}
function payload() {
  return JSON.stringify({ file: S.files[S.idx].file, start_frame: S.region ? S.region[0] : null,
    end_frame: S.region ? S.region[1] : null, reviewed: S.reviewed, memo: S.memo });
}
async function save() {
  if (S.idx < 0 || !S.view) return;
  clearTimeout(S.saveTimer);
  const snap = stateJSON(), idx = S.idx;
  setSaveState("저장 중…");
  S.saving = api(`/api/files/${idx}/label`, { method: "PUT", body: payload() })
    .then((r) => {
      if (S.idx === idx) { S.hasLabel = true; S.saved = snap; setSaveState(`저장됨 ${r.label.updated_at} (${S.me.id})`); }
      const f = S.files[idx];
      f.status = r.label.reviewed ? "reviewed" : "draft";
      f.region = r.label.start_s == null ? "비프 없음" : `${r.label.start_s.toFixed(1)}~${r.label.end_s.toFixed(1)}초`;
      renderFiles(); refreshPanels();
      if (r.actions.length) loadHistory();
    })
    .catch((e) => setSaveState("저장 실패: " + e.message, true))
    .finally(() => { S.saving = null; if (isDirty()) scheduleSave(); });
  return S.saving;
}
window.addEventListener("beforeunload", (e) => {
  if (isDirty()) {
    fetch(`/api/files/${S.idx}/label`, { method: "PUT", body: payload(), keepalive: true, headers: { "Content-Type": "application/json" } });
    e.preventDefault(); e.returnValue = "";
  }
});

// ================= editing =================
function snapshot() { return JSON.stringify(S.region); }
function pushUndo() { S.undo.push(snapshot()); if (S.undo.length > 200) S.undo.shift(); S.redo = []; }
function changed() { S.hasLabel = true; refreshPanels(); draw(); scheduleSave(); }
function undo() { if (S.undo.length) { S.redo.push(snapshot()); S.region = JSON.parse(S.undo.pop()); changed(); } }
function redo() { if (S.redo.length) { S.undo.push(snapshot()); S.region = JSON.parse(S.redo.pop()); changed(); } }
function applyAuto() {
  const a = S.view.auto;
  pushUndo(); S.region = a && a.start_frame != null ? [a.start_frame, a.end_frame] : null; changed();
}
function noBeep() { pushUndo(); S.region = null; changed(); }
function shift(which, d) {
  if (!S.region) return;
  pushUndo();
  const r = [...S.region];
  if (which === 0) r[0] = Math.max(0, Math.min(r[1] - 1, r[0] + d)); else r[1] = Math.max(r[0] + 1, Math.min(nFrames(), r[1] + d));
  S.region = r; changed();
}

// ================= mouse =================
let drag = null;
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener("mousedown", (ev) => {
  if (!S.view || ev.button !== 0) return;
  const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top, rg = region(y);
  if (rg === "axis" || !rg) { S.cursor = clampS(sOf(x)); seekIfPlaying(); draw(); updateHover(); return; }
  const snap = snapshot();
  if (S.region) {
    const xa = xOf(boundary(S.region[0])), xb = xOf(boundary(S.region[1]));
    if (Math.abs(x - xa) <= 6 || Math.abs(x - xb) <= 6) {
      drag = { mode: "edge", x0: x, side: Math.abs(x - xa) <= Math.abs(x - xb) ? 0 : 1, snap, moved: false }; return;
    }
    if (x > xa && x < xb) { drag = { mode: "move", x0: x, f0: frameAt(sOf(x)), r0: [...S.region], snap, moved: false }; return; }
  }
  drag = { mode: "create", x0: x, anchor: frameAt(clampS(sOf(x))), snap, moved: false };
});
window.addEventListener("mousemove", (ev) => {
  if (!S.view || $("app-view").hidden) return;
  const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  S.hoverX = x >= 0 && x <= W && y >= 0 && y <= H ? x : -1;
  if (drag) {
    const s = clampS(sOf(Math.max(0, Math.min(W, x))));
    if (Math.abs(x - drag.x0) > 3) drag.moved = true;
    if (drag.mode === "edge" && drag.moved) {
      const b = nearestBoundary(s), rr = [...S.region];
      if (drag.side === 0) rr[0] = Math.min(b, rr[1] - 1); else rr[1] = Math.max(b, rr[0] + 1);
      S.region = rr;
    } else if (drag.mode === "move" && drag.moved) {
      const len = drag.r0[1] - drag.r0[0];
      const st = Math.max(0, Math.min(nFrames() - len, drag.r0[0] + frameAt(s) - drag.f0));
      S.region = [st, st + len];
    } else if (drag.mode === "create" && drag.moved) {
      const c = frameAt(s);
      S.region = [Math.min(drag.anchor, c), Math.max(drag.anchor, c) + 1];
    }
    refreshPanels();
  } else {
    const rg = region(y);
    let cur = "crosshair";
    if (S.region && rg && rg !== "axis") {
      const xa = xOf(boundary(S.region[0])), xb = xOf(boundary(S.region[1]));
      if (Math.abs(x - xa) <= 6 || Math.abs(x - xb) <= 6) cur = "ew-resize"; else if (x > xa && x < xb) cur = "grab";
    }
    canvas.style.cursor = cur;
    updateHover();
  }
  draw();
});
window.addEventListener("mouseup", () => {
  if (!drag) return;
  if (!drag.moved) { S.cursor = clampS(sOf(drag.x0)); seekIfPlaying(); }
  else if (snapshot() !== drag.snap) { S.undo.push(drag.snap); S.redo = []; changed(); }
  drag = null; refreshPanels(); draw();
});
canvas.addEventListener("wheel", (ev) => {
  if (!S.view) return;
  ev.preventDefault();
  const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left;
  if (ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
    const d = (ev.shiftKey ? ev.deltaY : ev.deltaX) / W * (S.v1 - S.v0); setView(S.v0 + d, S.v1 + d);
  } else zoom(Math.exp(ev.deltaY * 0.0015), sOf(x));
}, { passive: false });
function setView(a, b) {
  const n = nSamp(), minSpan = Math.min(n, S.view.sr * 0.3);
  const span = Math.max(minSpan, Math.min(n, b - a));
  a = Math.max(0, Math.min(n - span, a));
  S.v0 = a; S.v1 = a + span; draw(); updateHover();
}
function zoom(f, c) { c = c ?? (S.v0 + S.v1) / 2; setView(c - (c - S.v0) * f, c + (S.v1 - c) * f); }

// ================= playback =================
const audio = $("audio");
function play(from, to = null) {
  if (!S.view) return;
  audio.currentTime = from / S.view.sr; S.playEnd = to;
  audio.play().then(tick).catch(() => {});
}
function playAll() {
  if (!S.view) return;
  if (!audio.paused && S.playEnd === null) { stop(); return; }
  play(S.cursor >= nSamp() - 100 ? 0 : S.cursor);
}
function playSeg() { if (S.region) play(boundary(S.region[0]), boundary(S.region[1])); }
function stop() { audio.pause(); S.playEnd = null; tick(); }
function seekIfPlaying() { if (!audio.paused && S.playEnd === null) audio.currentTime = S.cursor / S.view.sr; }
function tick() {
  const playing = !audio.paused;
  $("btn-play-all").classList.toggle("active", playing && S.playEnd === null);
  $("btn-play-seg").classList.toggle("active", playing && S.playEnd !== null);
  if (!playing || !S.view) { draw(); return; }
  const s = audio.currentTime * S.view.sr;
  if (S.playEnd !== null && s >= S.playEnd) { stop(); return; }
  if (s > S.v1 || s < S.v0) { const span = S.v1 - S.v0; setView(s - span * 0.1, s + span * 0.9); }
  draw(); requestAnimationFrame(tick);
}
audio.addEventListener("ended", () => { S.playEnd = null; tick(); });

// ================= keyboard =================
window.addEventListener("keydown", async (ev) => {
  if ($("app-view").hidden || document.querySelector("dialog[open]")) return;
  if (ev.target.matches("input, textarea, select")) { if (ev.key === "Escape") ev.target.blur(); return; }
  if (!S.view) return;
  const k = ev.code, ctrl = ev.ctrlKey || ev.metaKey;
  if (ctrl && k === "KeyS") { ev.preventDefault(); await save(); return; }
  if (ctrl && k === "KeyZ") { ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
  if (ctrl && k === "KeyY") { ev.preventDefault(); redo(); return; }
  if (ctrl || ev.altKey) return;
  const map = {
    Space: playAll, Enter: playSeg, Escape: stop,
    Delete: noBeep, Backspace: noBeep, KeyA: applyAuto,
    BracketLeft: () => shift(0, -1), BracketRight: () => shift(0, 1),
    Semicolon: () => shift(1, -1), Quote: () => shift(1, 1),
    Equal: () => zoom(0.5), NumpadAdd: () => zoom(0.5), Minus: () => zoom(2), NumpadSubtract: () => zoom(2),
    KeyF: () => setView(0, nSamp()), KeyB: toggleBig, KeyT: toggleTheme,
    KeyR: reviewNext, KeyN: () => nextFile(1), ArrowDown: () => nextFile(1), KeyP: () => nextFile(-1), ArrowUp: () => nextFile(-1),
    ArrowLeft: () => moveCursor(-1), ArrowRight: () => moveCursor(1),
  };
  if (map[k]) { ev.preventDefault(); map[k](); }
  else if (ev.key === "?") $("dlg-help").showModal();
});
function moveCursor(d) {
  const j = Math.max(0, Math.min(nFrames() - 1, frameAt(S.cursor) + d));
  S.cursor = S.view.frame_starts[j];
  if (S.cursor < S.v0 || S.cursor > S.v1) { const span = S.v1 - S.v0; setView(S.cursor - span / 2, S.cursor + span / 2); }
  updateHover(); draw();
}
function nextFile(d) {
  const visible = [...$("files").querySelectorAll("li")].map((li) => +li.dataset.idx);
  const pos = visible.indexOf(S.idx);
  const t = pos < 0 ? visible[0] : visible[pos + d];
  if (t !== undefined) openFile(t);
}
async function reviewNext() {
  if (!S.hasLabel && !S.region && !confirm("구간을 지정하지 않았습니다. '비프 없음'으로 검수 완료할까요?")) return;
  S.hasLabel = true; S.reviewed = true; refreshPanels();
  await save(); nextFile(1);
}
function toggleBig() {
  S.big = !S.big;
  $("canvas-wrap").classList.toggle("big", S.big);
  $("btn-big").textContent = S.big ? "스펙트로그램 작게 보기" : "스펙트로그램 크게 보기";
  $("btn-big").classList.toggle("active", S.big);
}

// ================= toolbar & dialogs =================
$("btn-play-all").onclick = playAll;
$("btn-play-seg").onclick = playSeg;
$("btn-stop").onclick = stop;
$("btn-zoom-in").onclick = () => S.view && zoom(0.5);
$("btn-zoom-out").onclick = () => S.view && zoom(2);
$("btn-fit").onclick = () => S.view && setView(0, nSamp());
$("btn-big").onclick = toggleBig;
$("btn-auto").onclick = () => S.view && applyAuto();
$("btn-nobeep").onclick = () => S.view && noBeep();
$("btn-save").onclick = () => save();
$("btn-review").onclick = () => S.view && reviewNext();
$("gain").onchange = (e) => { S.gain = +e.target.value; draw(); };
$("filter").oninput = renderFiles;
$("status-filter").onchange = renderFiles;
$("memo").oninput = () => { S.memo = $("memo").value; S.hasLabel = true; scheduleSave(); };
$("btn-help").onclick = () => $("dlg-help").showModal();
$("btn-rescan").onclick = async () => { S.settings = await api("/api/rescan", { method: "POST" }); renderDataset(); await loadFiles(); };

function openSettings() {
  const s = S.settings, admin = S.me.is_admin;
  $("set-path").value = s.dataset_path; $("set-path").disabled = !admin;
  $("set-register").checked = s.allow_register; $("set-register").disabled = !admin;
  $("set-save").hidden = !admin;
  $("set-path-hint").textContent = admin
    ? "WAV 파일이 들어 있는 폴더의 전체 경로를 입력하세요 (하위 폴더까지 검색). 저장하면 다음 실행에도 그대로 사용됩니다."
    : "데이터 폴더는 관리자만 바꿀 수 있습니다.";
  $("set-datadir").textContent = s.data_dir;
  $("set-error").textContent = ""; $("pw-msg").textContent = "";
  $("dlg-settings").showModal();
}
$("btn-settings").onclick = openSettings;
$("set-save").onclick = async (ev) => {
  ev.preventDefault();
  try {
    await flushSave();
    S.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify({ dataset_path: $("set-path").value, allow_register: $("set-register").checked }) });
    renderDataset(); $("dlg-settings").close();
    S.idx = -1; S.view = null; await loadFiles();
    if (S.files.length) openFile(0);
  } catch (e) { $("set-error").textContent = e.message; }
};
$("pw-save").onclick = async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/me/password", { method: "PUT", body: JSON.stringify({ old: $("pw-old").value, new: $("pw-new").value }) });
    $("pw-msg").textContent = "변경했습니다. 다시 로그인하세요."; $("pw-msg").style.color = "var(--ok)";
    setTimeout(() => { $("dlg-settings").close(); showLogin(); }, 1200);
  } catch (e) { $("pw-msg").textContent = e.message; $("pw-msg").style.color = "var(--warn)"; }
};

$("btn-export").onclick = () => $("dlg-export").showModal();
for (const [id, kind] of [["exp-xlsx", "xlsx"], ["exp-labels", "labels.csv"], ["exp-frames", "frames.csv"], ["exp-audit", "audit.csv"]]) {
  $(id).onclick = async (ev) => {
    ev.preventDefault();
    await flushSave();
    const a = document.createElement("a");
    a.href = `/api/export/${kind}?reviewed_only=${$("exp-reviewed").checked}`;
    document.body.appendChild(a); a.click(); a.remove();
  };
}

// ================= start =================
startApp().catch(() => showLogin());
