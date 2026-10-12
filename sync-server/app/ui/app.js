// MineContext 管理画面（スマホ・iPad 向け）。sync-server の /v1 API だけを使う。
const TZ = "Asia/Tokyo";
const DEVICE_ID = "web-ui";
const $ = (id) => document.getElementById(id);

const state = {
  day: todayStr(),
  filter: "all",
  timeline: null,
  active: null,
};

// ---------- 共通 ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { const j = await res.json(); msg = j.detail ? JSON.stringify(j.detail) : msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function todayStr() {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: TZ }).format(new Date());
}
function shiftDay(day, n) {
  const d = new Date(`${day}T12:00:00+09:00`);
  d.setUTCDate(d.getUTCDate() + n);
  return new Intl.DateTimeFormat("sv-SE", { timeZone: TZ }).format(d);
}
const fmtTime = (iso) => new Intl.DateTimeFormat("ja-JP", { timeZone: TZ, hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
const hourOf = (iso) => Number(new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", hourCycle: "h23" }).format(new Date(iso)));
function fmtDayLabel(day) {
  const d = new Date(`${day}T12:00:00+09:00`);
  const s = new Intl.DateTimeFormat("ja-JP", { timeZone: TZ, month: "long", day: "numeric", weekday: "short" }).format(d);
  return day === todayStr() ? `今日 · ${s}` : s;
}
function fmtMinutes(m) {
  m = Math.round(m || 0);
  if (m < 60) return `${m}分`;
  return `${Math.floor(m / 60)}時間${m % 60 ? `${m % 60}分` : ""}`;
}
function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
// 日本時間の「その日の HH:MM」を ISO に
function isoFromDayTime(day, hhmm) {
  return new Date(`${day}T${hhmm}:00+09:00`).toISOString();
}
function hhmm(iso) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso));
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k);
  return e;
}
let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2600);
}

const END_REASON = {
  manual: "手動で終了",
  ai_detected: "AI が終了と判断",
  idle: "操作なしで終了",
  superseded: "次の作業へ",
};
const KIND_LABEL = {
  activity: "活動",
  screen_summary: "画面の要約",
  note: "メモ",
  todo: "Todo",
  ai_work: "AI の作業",
  report: "レポート",
  session: "作業",
};
const itemGroup = (it) => (it.type === "session" ? "session" : it.type === "ai_work" || it.event?.kind === "ai_work" ? "ai_work" : "mine");

// ---------- 状態 ----------
async function loadHealth() {
  const h = $("health");
  try {
    const j = await api("/healthz");
    h.className = "health " + (j.ok ? "ok" : "bad");
    h.querySelector(".label").textContent = j.ok ? (j.embed_pending ? `接続中 · 埋め込み待ち ${j.embed_pending}` : "接続中") : "DB に問題";
    return j;
  } catch {
    h.className = "health bad";
    h.querySelector(".label").textContent = "接続できません";
    return null;
  }
}

// ---------- 作業宣言 ----------
let elapsedTimer;
async function loadActive() {
  try {
    const j = await api("/v1/sessions/active");
    state.active = j.session;
  } catch {
    state.active = null;
  }
  renderActive();
}
function renderActive() {
  const s = state.active;
  $("active").hidden = !s;
  $("declare-label").textContent = s ? "次の作業を始める（今の作業はここで終了）" : "これから何をする？";
  clearInterval(elapsedTimer);
  if (!s) return;
  $("active-title").textContent = s.title;
  $("active-meta").textContent = `${fmtTime(s.started_at)} から${s.device_id ? ` · ${s.device_id}` : ""}${s.note ? ` · ${s.note}` : ""}`;
  const tick = () => ($("elapsed").textContent = fmtElapsed(Date.now() - new Date(s.started_at).getTime()));
  tick();
  elapsedTimer = setInterval(tick, 1000);
}
$("declare-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("declare-input");
  const title = input.value.trim();
  if (!title) return;
  try {
    const j = await api("/v1/sessions", { method: "POST", body: { title, device_id: DEVICE_ID } });
    input.value = "";
    toast(j.superseded ? `「${j.superseded.title}」を終了して開始しました` : "開始しました");
    await refreshToday();
  } catch (err) {
    toast(`開始できませんでした（${err.message}）`);
  }
});
$("btn-end").addEventListener("click", async () => {
  if (!state.active) return;
  try {
    await api(`/v1/sessions/${state.active.id}/end`, { method: "POST", body: {} });
    toast("終了しました");
    await refreshToday();
  } catch (err) {
    toast(`終了できませんでした（${err.message}）`);
  }
});
$("btn-cancel").addEventListener("click", async () => {
  if (!state.active || !confirm(`「${state.active.title}」を取り消しますか？記録には残りません。`)) return;
  try {
    await api(`/v1/sessions/${state.active.id}/cancel`, { method: "POST" });
    toast("取り消しました");
    await refreshToday();
  } catch (err) {
    toast(`取り消せませんでした（${err.message}）`);
  }
});

// ---------- タイムライン ----------
async function loadTimeline() {
  $("daylabel").textContent = fmtDayLabel(state.day);
  $("next-day").disabled = state.day >= todayStr();
  try {
    state.timeline = await api(`/v1/timeline?date=${state.day}&tz=${encodeURIComponent(TZ)}`);
  } catch (err) {
    state.timeline = { items: [], session_totals: [], total_minutes: 0 };
    toast(`読み込めませんでした（${err.message}）`);
  }
  renderTimeline();
}

// ---------- 横スクロールのタイムライン ----------
const ZOOMS = [30, 45, 60, 90, 120, 180, 240, 360]; // 1 時間あたりの px
let zoomIdx = Number(localStorage.getItem("mc.zoom") ?? 3);
let lanesScrolledFor = null;

function laneKey(it) {
  if (it.type === "session") return { key: "session", label: "作業", color: "var(--k-session)", order: 0 };
  const ev = it.event;
  const name = ev.device_name || ev.device_id;
  if (ev.kind === "ai_work") return { key: `ai:${ev.device_id}`, label: `AI ${name}`, color: "var(--k-ai)", order: 2 };
  return { key: `dev:${ev.device_id}`, label: name, color: "var(--k-mine)", order: 1 };
}

function renderLanes(items) {
  const pxh = ZOOMS[zoomIdx];
  const width = pxh * 24;
  const dayStart = new Date(`${state.day}T00:00:00+09:00`).getTime();
  const x = (iso) => Math.min(width, Math.max(0, ((new Date(iso).getTime() - dayStart) / 3600000) * pxh));

  const lanes = new Map([["session", { label: "作業", color: "var(--k-session)", order: 0, items: [] }]]);
  for (const it of items) {
    const k = laneKey(it);
    if (!lanes.has(k.key)) lanes.set(k.key, { ...k, items: [] });
    lanes.get(k.key).items.push(it);
  }
  const ordered = [...lanes.values()].sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));

  const labels = $("lane-labels");
  labels.replaceChildren(el("div", { class: "axis-gap" }),
    ...ordered.map((l) => el("div", { class: "lab", title: l.label }, el("span", { class: "sw", style: `background:${l.color}` }), l.label)));

  const canvas = $("lane-canvas");
  canvas.style.width = `${width}px`;
  const axis = el("div", { class: "axis" });
  const step = pxh >= 60 ? 1 : 2;
  for (let h = 0; h <= 24; h += step) axis.append(el("span", { style: `left:${h * pxh}px` }, `${h}:00`));
  const rows = ordered.map((l) => {
    const row = el("div", { class: "row-bg" });
    for (const it of l.items) row.append(laneMark(it, x));
    return row;
  });
  const grid = [];
  for (let h = 0; h <= 24; h++) grid.push(el("div", { class: "grid", style: `left:${h * pxh}px` }));
  if (pxh >= 120) for (let h = 0; h < 24; h++) grid.push(el("div", { class: "grid half", style: `left:${(h + 0.5) * pxh}px` }));
  const nowEl = state.day === todayStr() ? el("div", { class: "now", style: `left:${x(new Date().toISOString())}px` }) : null;
  canvas.replaceChildren(...grid, axis, ...rows, nowEl);

  // 初回だけ、今（今日）か最初の記録の位置へスクロール
  if (lanesScrolledFor !== state.day) {
    lanesScrolledFor = state.day;
    scrollLanesTo(state.day === todayStr() ? new Date().toISOString() : items[0]?.t, x);
  }
}

function scrollLanesTo(iso, x) {
  const sc = $("lane-scroll");
  if (!iso) { sc.scrollLeft = 0; return; }
  sc.scrollLeft = Math.max(0, x(iso) - sc.clientWidth * 0.7);
}

function laneMark(it, x) {
  if (it.type === "session") {
    const s = it.session;
    const end = s.ended_at || (s.status === "active" ? new Date().toISOString() : s.started_at);
    const left = x(s.started_at);
    const cls = ["blk", s.status === "active" ? "live" : "", !s.confirmed && s.status === "ended" ? "unconfirmed" : ""].join(" ");
    return el("div", { class: cls, style: `left:${left}px;width:${Math.max(4, x(end) - left)}px`, onclick: () => showPop(it) }, s.title);
  }
  const ev = it.event;
  if (ev.ended_at && new Date(ev.ended_at) > new Date(ev.started_at)) {
    const left = x(ev.started_at);
    return el("div", { class: "bar", style: `left:${left}px;width:${Math.max(6, x(ev.ended_at) - left)}px`, onclick: () => showPop(it) });
  }
  return el("div", { class: `dot ${ev.kind === "ai_work" ? "ai_work" : ""}`, style: `left:${x(ev.started_at)}px`, onclick: () => showPop(it) });
}

function showPop(it) {
  const pop = $("lane-pop");
  if (it.type === "session") {
    const s = it.session;
    pop.replaceChildren(
      el("div", { class: "t" }, s.title),
      el("div", { class: "s" }, `${fmtTime(s.started_at)}〜${s.ended_at ? fmtTime(s.ended_at) : "作業中"}${s.duration_minutes != null ? ` · ${fmtMinutes(s.duration_minutes)}` : ""}`),
      s.note ? el("div", { class: "b" }, s.note) : null,
    );
  } else {
    const ev = it.event;
    pop.replaceChildren(
      el("div", { class: "t" }, ev.title || ev.app || KIND_LABEL[ev.kind] || ev.kind),
      el("div", { class: "s" }, `${fmtTime(ev.started_at)} · ${ev.device_name || ev.device_id} · ${KIND_LABEL[ev.kind] || ev.kind}`),
      el("div", { class: "b" }, ev.text),
    );
  }
  pop.hidden = false;
}

function setZoom(delta) {
  const sc = $("lane-scroll");
  const center = (sc.scrollLeft + sc.clientWidth / 2) / ZOOMS[zoomIdx]; // 中央の時刻（時間）
  zoomIdx = Math.min(ZOOMS.length - 1, Math.max(0, zoomIdx + delta));
  localStorage.setItem("mc.zoom", String(zoomIdx));
  renderLanes(state.timeline?.items || []);
  sc.scrollLeft = Math.max(0, center * ZOOMS[zoomIdx] - sc.clientWidth / 2);
}
$("zoom-in").addEventListener("click", () => setZoom(1));
$("zoom-out").addEventListener("click", () => setZoom(-1));
$("zoom-now").addEventListener("click", () => {
  if (state.day !== todayStr()) { state.day = todayStr(); loadTimeline(); return; }
  const pxh = ZOOMS[zoomIdx];
  const dayStart = new Date(`${state.day}T00:00:00+09:00`).getTime();
  scrollLanesTo(new Date().toISOString(), (iso) => ((new Date(iso).getTime() - dayStart) / 3600000) * pxh);
});

// ---------- 1か月のヒートマップ ----------
let heatMetric = localStorage.getItem("mc.heat") || "active_minutes";
let heatData = null;
const HEAT_UNIT = { active_minutes: "分動いた", ai_active_minutes: "分 AI が動いた", session_minutes: "分の作業" };

async function loadHeat() {
  const to = todayStr();
  const from = shiftDay(to, -34);
  try {
    const j = await api(`/v1/stats/daily?from=${from}&to=${to}&tz=${encodeURIComponent(TZ)}`);
    heatData = j.days;
    $("heat-note").textContent = "";
  } catch {
    heatData = null;
    $("heat-note").textContent = "集計を読み込めませんでした。";
  }
  renderHeat();
}

function renderHeat() {
  for (const b of $("heat-metric").children) b.classList.toggle("on", b.dataset.m === heatMetric);
  const box = $("heat");
  box.replaceChildren();
  if (!heatData) return;
  const today = todayStr();
  // 直近 5 週（月曜はじまり）
  const dow = (day) => (new Date(`${day}T12:00:00+09:00`).getUTCDay() + 6) % 7;
  const end = shiftDay(today, 6 - dow(today));
  const start = shiftDay(end, -34);
  const byDate = new Map(heatData.map((d) => [d.date, d]));
  const vals = heatData.map((d) => d[heatMetric] || 0).filter((v) => v > 0).sort((a, b) => a - b);
  const q = (p) => vals.length ? vals[Math.min(vals.length - 1, Math.floor(p * vals.length))] : Infinity;
  const th = [q(0.25), q(0.5), q(0.75)];
  const level = (v) => (!v ? 0 : v <= th[0] ? 1 : v <= th[1] ? 2 : v <= th[2] ? 3 : 4);
  let total = 0;
  for (let i = 0; i < 35; i++) {
    const day = shiftDay(start, i);
    const d = byDate.get(day);
    const v = d ? Math.round(d[heatMetric] || 0) : 0;
    if (day <= today) total += v;
    const future = day > today;
    const lv = future ? 0 : level(v);
    const cls = ["c", `l${lv}`, day === today ? "today" : "", day === state.day ? "sel" : "", future ? "future" : ""].join(" ");
    const label = `${Number(day.slice(5, 7))}/${Number(day.slice(8))}`;
    box.append(el("button", {
      class: cls,
      title: `${label}：${v}${HEAT_UNIT[heatMetric]}`,
      "aria-label": `${label} ${v}${HEAT_UNIT[heatMetric]}`,
      disabled: future,
      onclick: () => { state.day = day; loadTimeline(); renderHeat(); window.scrollTo({ top: 0, behavior: "smooth" }); },
    }, el("span", { class: "n" }, String(Number(day.slice(8))))));
  }
  $("heat-title").textContent = `この1か月 · ${fmtMinutes(total)}`;
}
$("heat-metric").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  heatMetric = b.dataset.m;
  localStorage.setItem("mc.heat", heatMetric);
  renderHeat();
});

function sessionItem(s) {
  const reason = s.status === "active" ? el("span", { class: "badge live" }, "作業中")
    : s.status === "cancelled" ? el("span", { class: "badge" }, "取り消し")
    : el("span", { class: `badge ${s.confirmed ? "ok" : "warn"}` }, s.confirmed ? "確定" : `${END_REASON[s.end_reason] || "終了"}・未確定`);
  const range = `${fmtTime(s.started_at)}〜${s.ended_at ? fmtTime(s.ended_at) : ""}`;
  const actions = el("div", { class: "actions" });
  if (s.status === "ended") {
    if (!s.confirmed) actions.append(el("button", { class: "btn small", onclick: () => confirmSession(s) }, "このまま確定"));
    actions.append(el("button", { class: "btn small ghost", onclick: () => openEdit(s) }, "修正"));
  }
  const evidence = (s.end_evidence || []).map((e) => e.reason).filter(Boolean);
  return el("li", { class: "item session" },
    el("div", { class: "time" }, fmtTime(s.started_at)),
    el("div", {},
      el("div", { class: "head" }, el("span", { class: "title" }, s.title), reason),
      el("div", { class: "sub" }, `${range}${s.duration_minutes != null ? ` · ${fmtMinutes(s.duration_minutes)}` : ""}${s.end_confidence != null ? ` · 確信度 ${Math.round(s.end_confidence * 100)}%` : ""}`),
      s.note ? el("div", { class: "body" }, s.note) : null,
      evidence.length ? el("div", { class: "sub" }, `判断の根拠：${evidence.join(" / ")}`) : null,
      actions.childElementCount ? actions : null,
    ),
  );
}

function eventItem(ev, extra) {
  const g = ev.kind === "ai_work" ? "ai_work" : "mine";
  const body = el("div", { class: "body clamp", onclick: (e) => e.currentTarget.classList.toggle("clamp") }, ev.text);
  const who = ev.device_name || ev.device_id;
  return el("li", { class: `item ${g}` },
    el("div", { class: "time" }, fmtTime(ev.started_at)),
    el("div", {},
      el("div", { class: "head" },
        el("span", { class: "title" }, ev.title || ev.app || KIND_LABEL[ev.kind] || ev.kind),
        el("span", { class: "badge" }, KIND_LABEL[ev.kind] || ev.kind),
        extra,
      ),
      el("div", { class: "sub" }, [who, ev.app && ev.title ? ev.app : null].filter(Boolean).join(" · ")),
      body,
    ),
  );
}

function renderTimeline() {
  const tl = state.timeline;
  const items = tl.items || [];
  renderLanes(items);
  $("lane-pop").hidden = true;
  if (heatData) renderHeat();
  const shown = items.filter((it) => state.filter === "all" || itemGroup(it) === state.filter);
  const list = $("timeline");
  list.replaceChildren();
  let lastHour = -1;
  for (const it of shown) {
    const h = hourOf(it.t);
    if (h !== lastHour) {
      list.append(el("li", { class: "hour" }, `${h}時`));
      lastHour = h;
    }
    list.append(it.type === "session" ? sessionItem(it.session) : eventItem(it.event));
  }
  $("timeline-empty").hidden = shown.length > 0;

  $("total").textContent = fmtMinutes(tl.total_minutes);
  $("totals").replaceChildren(...(tl.session_totals || []).map((t) =>
    el("li", {}, el("span", { class: "t" }, t.title), el("span", { class: "m" }, fmtMinutes(t.minutes)))));
  if (!(tl.session_totals || []).length) $("totals").append(el("li", {}, el("span", { class: "t" }, "宣言した作業はまだありません")));

  const counts = {};
  for (const it of items) {
    const k = it.type === "session" ? "session" : it.event?.kind || it.type;
    counts[k] = (counts[k] || 0) + 1;
  }
  $("counts").replaceChildren(...Object.entries(counts).map(([k, n]) =>
    el("li", {}, el("span", {}, KIND_LABEL[k] || k), el("span", { class: "m" }, `${n}件`))));

  renderDevices(items);
}

async function confirmSession(s) {
  try {
    await api(`/v1/sessions/${s.id}`, { method: "PATCH", body: { confirmed: true } });
    toast("確定しました");
    await loadTimeline();
  } catch (err) {
    toast(`確定できませんでした（${err.message}）`);
  }
}

let editing = null;
function openEdit(s) {
  editing = s;
  $("edit-title").value = s.title;
  $("edit-note").value = s.note || "";
  $("edit-start").value = hhmm(s.started_at);
  $("edit-end").value = s.ended_at ? hhmm(s.ended_at) : "";
  $("edit-reason").textContent = s.end_reason ? `終了理由：${END_REASON[s.end_reason] || s.end_reason}` : "";
  $("edit-dialog").showModal();
}
$("edit-dialog").addEventListener("close", async () => {
  if ($("edit-dialog").returnValue !== "save" || !editing) return;
  const s = editing;
  editing = null;
  const day = new Intl.DateTimeFormat("sv-SE", { timeZone: TZ }).format(new Date(s.started_at));
  const body = {
    title: $("edit-title").value.trim(),
    note: $("edit-note").value.trim() || null,
    started_at: isoFromDayTime(day, $("edit-start").value),
    confirmed: true,
  };
  if ($("edit-end").value) {
    let end = isoFromDayTime(day, $("edit-end").value);
    if (end <= body.started_at) end = isoFromDayTime(shiftDay(day, 1), $("edit-end").value);
    body.ended_at = end;
  }
  try {
    await api(`/v1/sessions/${s.id}`, { method: "PATCH", body });
    toast("保存しました");
    await loadTimeline();
  } catch (err) {
    toast(`保存できませんでした（${err.message}）`);
  }
});

// ---------- 絞り込み・日付 ----------
$("filters").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  state.filter = b.dataset.kind;
  for (const c of $("filters").children) c.classList.toggle("on", c === b);
  renderTimeline();
});
$("prev-day").addEventListener("click", () => { state.day = shiftDay(state.day, -1); loadTimeline(); });
$("next-day").addEventListener("click", () => { if (state.day < todayStr()) { state.day = shiftDay(state.day, 1); loadTimeline(); } });
$("daylabel").addEventListener("click", () => { state.day = todayStr(); loadTimeline(); });

// ---------- 検索 ----------
$("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("search-input").value.trim();
  if (!query) return;
  const kinds = [];
  if ($("search-mine").checked) kinds.push("activity", "screen_summary", "note", "todo", "report");
  if ($("search-ai").checked) kinds.push("ai_work");
  const list = $("results");
  list.replaceChildren(el("li", { class: "empty" }, "検索中…"));
  try {
    const j = await api("/v1/search", { method: "POST", body: { query, kinds: kinds.length ? kinds : null, k: 30 } });
    list.replaceChildren(...j.hits.map((h) => {
      const li = eventItem(h.event, el("span", { class: "score" }, new Intl.DateTimeFormat("ja-JP", { timeZone: TZ, month: "numeric", day: "numeric" }).format(new Date(h.event.started_at))));
      return li;
    }));
    $("results-empty").hidden = j.hits.length > 0;
  } catch (err) {
    list.replaceChildren();
    toast(`検索できませんでした（${err.message}）`);
  }
});

// ---------- 状態タブ ----------
async function loadStatus() {
  const h = await loadHealth();
  $("server-kv").replaceChildren(
    el("dt", {}, "状態"), el("dd", {}, h ? (h.ok ? "正常" : "問題あり") : "接続できません"),
    el("dt", {}, "データベース"), el("dd", {}, h?.db ?? "-"),
    el("dt", {}, "埋め込み待ち"), el("dd", {}, h ? `${h.embed_pending}件` : "-"),
  );
  try {
    const j = await api("/v1/importers");
    const im = j.importers.find((i) => i.name === "agent_memory") || j.importers[0];
    $("importer-kv").replaceChildren(
      el("dt", {}, "有効"), el("dd", {}, im?.enabled ? "はい" : "いいえ"),
      el("dt", {}, "最後に成功"), el("dd", {}, im?.last_success_at ? new Date(im.last_success_at).toLocaleString("ja-JP", { timeZone: TZ }) : "-"),
      el("dt", {}, "取り込んだ件数"), el("dd", {}, im ? `${im.imported_count}件` : "-"),
      el("dt", {}, "最後のエラー"), el("dd", {}, im?.last_error || "なし"),
    );
  } catch {
    $("importer-kv").replaceChildren(el("dt", {}, "状態"), el("dd", {}, "取得できません"));
  }
  if (!state.timeline) await loadTimeline();
}
function renderDevices(items) {
  const by = new Map();
  for (const it of items) {
    const ev = it.event;
    const id = ev?.device_id || it.session?.device_id;
    if (!id) continue;
    const name = ev?.device_name || id;
    const d = by.get(id) || { name, n: 0, last: null };
    d.n += 1;
    d.last = it.t;
    by.set(id, d);
  }
  $("devices").replaceChildren(...[...by.values()].map((d) =>
    el("li", {}, el("span", {}, d.name), el("span", { class: "m" }, `${d.n}件 · 最後 ${fmtTime(d.last)}`))));
  if (!by.size) $("devices").append(el("li", {}, "この日の記録はありません"));
}
$("btn-import").addEventListener("click", async (e) => {
  e.currentTarget.disabled = true;
  try {
    const j = await api("/v1/importers/agent-memory/run", { method: "POST" });
    toast(`取り込みました（新規・更新 ${j.upserted}件）`);
    await Promise.all([loadStatus(), loadTimeline()]);
  } catch (err) {
    toast(`取り込めませんでした（${err.message}）`);
  } finally {
    e.currentTarget.disabled = false;
  }
});

// ---------- タブ ----------
function showTab(name) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.dataset.view !== name;
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("on", t.dataset.tab === name);
  if (name === "status") loadStatus();
  if (name === "search") $("search-input").focus();
  history.replaceState(null, "", `#${name}`);
}
document.querySelector(".tabbar").addEventListener("click", (e) => {
  const t = e.target.closest(".tab");
  if (t) showTab(t.dataset.tab);
});

async function refreshToday() {
  await Promise.all([loadActive(), loadTimeline(), loadHeat()]);
}

// 起動
loadHealth();
refreshToday();
showTab((location.hash || "#today").slice(1).replace(/[^a-z]/g, "") || "today");
// 画面に戻ってきたら最新にする（スマホでの切り替え向け）
document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadHealth(); refreshToday(); } });
setInterval(() => { if (!document.hidden) refreshToday(); }, 60_000);
