/* Dehkhoda RAG — UI logic (vanilla JS, RTL) */
"use strict";

let BASE = null; // http://127.0.0.1:<port>

const $chat = document.getElementById("chat");
const $q = document.getElementById("q");
const $go = document.getElementById("go");
const $dot = document.getElementById("status-dot");
const $status = document.getElementById("status-text");

function setStatus(kind, text) {
  $dot.className = "dot " + (kind === "on" ? "dot-on" : kind === "err" ? "dot-err" : "dot-off");
  $status.textContent = text;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function resolvePort() {
  // Tauri v2 exposes invoke on window.__TAURI__; plain-browser dev falls back
  // to the default 8300.
  try {
    const inv = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    if (inv) return await inv("backend_port");
  } catch (e) { /* fall through */ }
  return 8300;
}

async function waitBackend() {
  let tries = 0;
  while (tries < 600) { // up to 5 min
    try {
      const r = await fetch(BASE + "/health", { method: "GET" });
      if (r.ok) {
        const h = await r.json();
        const n = (h.count ?? 0).toLocaleString("fa-IR");
        const countTxt = h.count ? ` • ${n} مدخل بارگذاری شده` : "";
        const bm = h.bm25_fitted ? "" : " • نمایه BM25 آماده نیست (در حال ساخت)";
        setStatus("on", `متصل به موتور (${h.backend.backend})${countTxt}${bm}`);
        return true;
      }
    } catch (e) { /* backend still starting */ }
    tries += 1;
    await new Promise((r) => setTimeout(r, 500));
  }
  setStatus("err", "اتصال به موتور ناموفق بود");
  return false;
}

function addUserBubble(text) {
  $chat.appendChild(el("div", "bubble-user", esc(text)));
  $chat.scrollTop = $chat.scrollHeight;
}

function addTyping() {
  const t = el("div", "card system-note");
  t.innerHTML = '<span class="typing"><span></span><span></span><span></span></span> در حال جست‌وجو…';
  $chat.appendChild(t);
  $chat.scrollTop = $chat.scrollHeight;
  return t;
}

function renderResults(data) {
  const items = data.results || [];
  const card = el("div", "card hit");
  if (!items.length) {
    card.innerHTML = "<div class='hit-def'>نتیجه‌ای یافت نشد. عبارت دیگری را امتحان کنید.</div>";
  } else {
    const head = el("div", "hit-head");
    head.appendChild(el("div", "hit-word", esc(items[0].word)));
    head.appendChild(el("div", "hit-score", `RRF ${items[0].score.toFixed(4)}`));
    card.appendChild(head);
    card.appendChild(el("div", "hit-def", esc(items[0].definition)));
    const foot = el("div", "hit-foot");
    foot.textContent = `${data.backend} • ${data.timing_ms.total} ms`;
    card.appendChild(foot);

    if (items.length > 1) {
      const more = el("details", "more");
      const sum = el("summary", null, `${items.length - 1} نتیجهٔ دیگر…`);
      more.appendChild(sum);
      for (const it of items.slice(1)) {
        const row = el("div", "more-row");
        row.appendChild(el("div", "hit-word small", esc(it.word)));
        row.appendChild(el("div", "hit-def small", esc(it.definition)));
        more.appendChild(row);
      }
      card.appendChild(more);
    }
  }
  $chat.appendChild(card);
  $chat.scrollTop = $chat.scrollHeight;
}

async function search(query) {
  if (!query.trim()) return;
  addUserBubble(query);
  const typing = addTyping();
  $go.disabled = true;
  try {
    const r = await fetch(BASE + "/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, top_k: 8 }),
    });
    typing.remove();
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      setStatus("err", "خطا در جست‌وجو: " + (err.detail || r.status));
      return;
    }
    renderResults(await r.json());
  } catch (e) {
    typing.remove();
    setStatus("err", "ارتباط با موتور برقرار نشد");
  } finally {
    $go.disabled = false;
    $q.focus();
  }
}

document.querySelectorAll(".chip").forEach((c) =>
  c.addEventListener("click", () => search(c.dataset.q))
);
$go.addEventListener("click", () => search($q.value));
$q.addEventListener("keydown", (e) => {
  if (e.key === "Enter") search($q.value);
});

(async function init() {
  const port = await resolvePort();
  BASE = `http://127.0.0.1:${port}`;
  setStatus("off", "در حال راه‌اندازی موتور…");
  const ok = await waitBackend();
  if (ok) {
    $chat.appendChild(
      el("div", "card system-note", "موتور آماده است — سؤال خود را بنویسید یا یکی از نمونه‌ها را انتخاب کنید.")
    );
  }
  // keep status fresh
  setInterval(async () => {
    try {
      const r = await fetch(BASE + "/health");
      const h = await r.json();
      if (r.ok) setStatus("on", `متصل به موتور (${h.backend.backend}) • ${(h.count ?? 0).toLocaleString("fa-IR")} مدخل`);
    } catch (e) { /* keep last status */ }
  }, 15000);
})();
