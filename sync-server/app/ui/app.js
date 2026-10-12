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

function renderStrip(items) {
  const strip = $("strip");
  strip.replaceChildren();
  const dayStart = new Date(`${state.day}T00:00:00+09:00`).getTime();
  const span = 24 * 3600 * 1000;
  const pos = (iso) => Math.min(100, Math.max(0, ((new Date(iso).getTime() - dayStart) / span) * 100));
  for (const it of items) {
    const g = itemGroup(it);
    if (g === "session") {
      const s = it.session;
      const end = s.ended_at || (s.status === "active" ? new Date().toISOString() : s.started_at);
      const left = pos(s.started_at);
      strip.append(el("div", {
        class: `seg${s.confirmed || s.status === "active" ? "" : " unconfirmed"}`,
        style: `left:${left}%;width:${Math.max(0.4, pos(end) - left)}%`,
        title: `${s.title}（${fmtTime(s.started_at)}〜${s.ended_at ? fmtTime(s.ended_at) : "作業中"}）`,
      }));
    } else {
      strip.append(el("div", { class: `tick ${g}`, style: `left:${pos(it.t)}%` }));
    }
  }
  if (state.day === todayStr()) strip.append(el("div", { class: "now", style: `left:${pos(new Date().toISOString())}%` }));
}

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
  renderStrip(items);
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
  await Promise.all([loadActive(), loadTimeline()]);
}

// 起動
loadHealth();
refreshToday();
showTab((location.hash || "#today").slice(1).replace(/[^a-z]/g, "") || "today");
// 画面に戻ってきたら最新にする（スマホでの切り替え向け）
document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadHealth(); refreshToday(); } });
setInterval(() => { if (!document.hidden) refreshToday(); }, 60_000);
