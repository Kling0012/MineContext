// Hibi の備忘録（ブログ閲覧）。sync-server の /v1/blog API だけを使う。
import { renderMarkdown, esc } from "../ui/md.js";

const $ = (id) => document.getElementById(id);
const KIND = { post: "記事", summary: "1日のまとめ", learning: "技術的な学び" };
const EMBED = document.documentElement.classList.contains("embed");
const fmtDate = (d) => {
  const [y, m, day] = d.split("-").map(Number);
  const w = "日月火水木金土"[new Date(Date.UTC(y, m - 1, day)).getUTCDay()];
  return `${y}年${m}月${day}日（${w}）`;
};

const list = { type: "", tag: "", q: "", cursor: null };

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

function tagLink(t, n) {
  return `<a class="tag" href="#/tag/${encodeURIComponent(t)}">#${esc(t)}${n != null ? `<span class="n">${n}</span>` : ""}</a>`;
}

function card(p) {
  return `<li><a class="post-card" href="#/post/${encodeURIComponent(p.slug)}">
    <div class="meta"><span class="kind ${esc(p.type)}">${esc(KIND[p.type] || p.type)}</span><span>${esc(fmtDate(p.date))}</span></div>
    <h3>${esc(p.title)}</h3>
    <p>${esc(p.excerpt || "")}</p>
  </a></li>`;
}

async function loadList(append = false) {
  const params = new URLSearchParams({ limit: "20" });
  if (list.type) params.set("type", list.type);
  if (list.tag) params.set("tag", list.tag);
  if (list.q) params.set("q", list.q);
  if (append && list.cursor) params.set("cursor", list.cursor);
  let j;
  try {
    j = await api(`/v1/blog/posts?${params}`);
  } catch {
    $("posts").innerHTML = '<li class="empty">記事を読み込めませんでした。</li>';
    return;
  }
  const html = j.posts.map(card).join("");
  if (append) $("posts").insertAdjacentHTML("beforeend", html);
  else $("posts").innerHTML = html;
  list.cursor = j.next_cursor || null;
  $("more").hidden = !list.cursor;
  $("posts-empty").hidden = append || j.posts.length > 0;
  $("active-tag").hidden = !list.tag;
  if (list.tag) $("active-tag").innerHTML = `タグ <b>#${esc(list.tag)}</b> の記事 <button id="clear-tag">解除</button>`;
  for (const c of $("type-filter").children) c.classList.toggle("on", c.dataset.type === list.type);
}

async function loadTags() {
  try {
    const tags = await api("/v1/blog/tags");
    $("tags").innerHTML = tags.map((t) => tagLink(t.tag, t.count)).join("");
  } catch {
    $("tags").innerHTML = "";
  }
}

async function showPost(slug) {
  $("list-view").hidden = true;
  $("post-view").hidden = false;
  $("post-body").innerHTML = '<p class="empty">読み込み中…</p>';
  let p;
  try {
    p = await api(`/v1/blog/posts/${encodeURIComponent(slug)}`);
  } catch {
    $("post-title").textContent = "記事が見つかりません";
    $("post-body").innerHTML = "";
    return;
  }
  document.title = `${p.title} | Hibi の備忘録`;
  $("post-meta").innerHTML = `<span class="kind ${esc(p.type)}">${esc(KIND[p.type] || p.type)}</span><span>${esc(fmtDate(p.date))}</span>`;
  $("post-title").textContent = p.title;
  $("post-tags").innerHTML = (p.tags || []).map((t) => tagLink(t)).join("");
  // 本文先頭の見出しがタイトルと同じなら重複させない
  const body = p.markdown.replace(/^#\s+.*\n+/, (h) => (h.replace(/^#\s+/, "").trim() === p.title.trim() ? "" : h));
  $("post-body").innerHTML = renderMarkdown(body);
  $("post-foot").innerHTML = `この日の記録：<a href="../ui/${EMBED ? "?embed=1" : ""}#report" data-day="${esc(p.date)}" id="to-report">日報を見る</a>${p.model ? ` · 執筆 ${esc(p.model)}` : ""}`;
  $("to-report").addEventListener("click", () => localStorage.setItem("mc.reportDay", p.date));
  const nav = [];
  if (p.prev) nav.push(`<a class="prev" href="#/post/${encodeURIComponent(p.prev.slug)}"><span class="lbl">‹ 前の記事</span>${esc(p.prev.title)}</a>`);
  if (p.next) nav.push(`<a class="next" href="#/post/${encodeURIComponent(p.next.slug)}"><span class="lbl">次の記事 ›</span>${esc(p.next.title)}</a>`);
  $("prevnext").innerHTML = nav.join("");
  window.scrollTo(0, 0);
}

function route() {
  const h = decodeURIComponent(location.hash.slice(1) || "/");
  const post = h.match(/^\/post\/(.+)$/);
  if (post) { showPost(post[1]); return; }
  document.title = "Hibi の備忘録";
  $("post-view").hidden = true;
  $("list-view").hidden = false;
  const tag = h.match(/^\/tag\/(.+)$/);
  list.tag = tag ? tag[1] : "";
  list.cursor = null;
  loadList();
}

$("type-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  list.type = b.dataset.type;
  list.cursor = null;
  loadList();
});
$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  list.q = $("q").value.trim();
  list.cursor = null;
  loadList();
});
$("active-tag").addEventListener("click", (e) => {
  if (e.target.id === "clear-tag") location.hash = "#/";
});
$("more").addEventListener("click", () => loadList(true));
window.addEventListener("hashchange", route);

loadTags();
route();
