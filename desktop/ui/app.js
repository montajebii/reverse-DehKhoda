/* Dehkhoda Reverse Dictionary — UI logic (vanilla JS, RTL).
   Stack note: kept intentionally framework-free ("equally lightweight" per
   spec) — zero build step, tiny bundle, same Tauri v2 shell. */
"use strict";

let BASE = null; // http://127.0.0.1:<port>
let inflight = null; // AbortController for the running search

const $form = document.getElementById("search-form");
const $q = document.getElementById("q");
const $go = document.getElementById("go");
const $formMini = document.getElementById("search-mini");
const $qMini = document.getElementById("q-mini");
const $results = document.getElementById("results");
const $hero = document.getElementById("hero");
const $dot = document.getElementById("status-dot");
const $status = document.getElementById("status-text");
const $foot = document.getElementById("foot-note");
const $themeBtn = document.getElementById("theme-toggle");
const $iconMoon = document.getElementById("icon-moon");
const $iconSun = document.getElementById("icon-sun");

/* ---------------- theme ---------------- */
const THEME_KEY = "dehkhoda-theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const dark = theme === "dark";
  $iconMoon.hidden = dark;
  $iconSun.hidden = !dark;
}

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const prefersDark =
    window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}

$themeBtn.addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const next = cur === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
});

/* ---------------- helpers ---------------- */
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const faNum = (n) => Number(n).toLocaleString("fa-IR");

async function resolvePort() {
  try {
    const inv = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    if (inv) return await inv("backend_port");
  } catch (e) { /* plain-browser dev fallback below */ }
  return 8300;
}

function setStatus(kind, text) {
  $dot.className = "dot " + (kind === "on" ? "dot-on" : kind === "err" ? "dot-err" : "dot-off");
  $status.textContent = text;
}

async function waitBackend() {
  for (let tries = 0; tries < 600; tries++) { // up to 5 min (model warm-up)
    try {
      const r = await fetch(BASE + "/health");
      if (r.ok) {
        const h = await r.json();
        updateStatusFromHealth(h);
        return true;
      }
    } catch (e) { /* backend still starting */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  setStatus("err", "اتصال به موتور ناموفق بود");
  return false;
}

function updateStatusFromHealth(h) {
  const parts = ["موتور آماده است"];
  if (h.count) parts.push(`${faNum(h.count)} مدخل`);
  if (h.bm25_fitted === false) parts.push("نمایه BM25 در حال ساخت");
  if (h.exact_index_ready === false) parts.push("تطبیق دقیق غیرفعال");
  setStatus(h.status === "ok" ? "on" : "err", parts.join(" • "));
}

/* ---------------- rendering ---------------- */
function hideHero() {
  if (!$hero.hidden) {
    $hero.hidden = true;
    $foot.hidden = true;
    $formMini.hidden = false; // persistent compact search after the first query
  }
}

function showHero() {
  $hero.hidden = false;
  $foot.hidden = false;
  $formMini.hidden = true;
  $results.innerHTML = "";
}

function skeletonCards(n = 3) {
  const frag = document.createDocumentFragment();
  const head = el("div", "results-head", "<b>در حال جست‌وجو…</b>");
  frag.appendChild(head);
  for (let i = 0; i < n; i++) frag.appendChild(el("div", "card skeleton"));
  $results.replaceChildren(frag);
}

function stateCard(icon, title, text, withRetry) {
  const c = el("div", "card state-card");
  c.appendChild(el("div", "icon", icon));
  c.appendChild(el("p", "state-title", esc(title)));
  c.appendChild(el("p", "state-text", esc(text)));
  if (withRetry) {
    const b = el("button", "btn-ghost", "تلاش دوباره");
    b.type = "button";
    b.addEventListener("click", () => runSearch($q.value.trim()));
    c.appendChild(b);
  }
  return c;
}

function hitCard(item, rank) {
  const card = el("div", "card hit");
  const head = el("div", "hit-head");
  head.appendChild(el("div", "hit-word", esc(item.headword)));
  if (item.match === "exact") {
    head.appendChild(el("span", "badge badge-exact", "تطبیق دقیق سرواژه"));
  }
  if (item.senses > 1) {
    head.appendChild(el("span", "badge badge-senses", `${faNum(item.senses)} معنا`));
  }
  card.appendChild(head);

  const line = el("div", "score-line");
  const track = el("div", "score-track");
  const fill = el("div", "score-fill");
  requestAnimationFrame(() => { fill.style.width = `${Math.max(2, Math.min(100, item.relevance))}%`; });
  track.appendChild(fill);
  line.appendChild(track);
  line.appendChild(el("div", "score-num", `تطبیق ${faNum(Math.round(item.relevance))}٪`));
  card.appendChild(line);

  card.appendChild(el("p", "hit-snippet", esc(item.snippet)));

  const details = el("details", "hit-details");
  const summary = el("summary", null, "نمایش تعریف کامل");
  const full = el("div", "hit-full", esc(item.definition));
  details.appendChild(summary);
  details.appendChild(full);
  card.appendChild(details);

  const foot = el("div", "hit-foot");
  foot.appendChild(el("span", null, `رتبهٔ ${faNum(rank)}`));
  if (item.match === "semantic" && item.score != null) {
    foot.appendChild(el("span", null, `امتیاز RRF: ${Number(item.score).toFixed(4)}`));
  }
  if (item.word_id) foot.appendChild(el("span", null, `شناسهٔ مدخل: ${faNum(item.word_id)}`));
  card.appendChild(foot);
  return card;
}

function renderResults(data) {
  const items = data.results || [];
  const frag = document.createDocumentFragment();

  const head = el("div", "results-head");
  if (!items.length) {
    $results.replaceChildren(
      stateCard("🔍", "نتیجه‌ای یافت نشد",
        "توصیف را ساده‌تر یا با واژگانی دیگر بنویسید؛ مثلاً «احساس بی‌قراری و اضطراب».")
    );
    return;
  }
  const secs = data.timing_ms ? ` • ${faNum(Math.round(data.timing_ms.total))} میلی‌ثانیه` : "";
  head.innerHTML = `<b>${faNum(items.length)} سرواژه مرتبط</b><span>${esc(data.backend || "")}${secs}</span>`;
  frag.appendChild(head);

  items.forEach((item, i) => frag.appendChild(hitCard(item, i + 1)));
  $results.replaceChildren(frag);
}

/* ---------------- search ---------------- */
async function runSearch(query) {
  if (!query) return;
  hideHero();
  if (inflight) inflight.abort();
  inflight = new AbortController();

  skeletonCards(3);
  $go.disabled = true;
  try {
    const r = await fetch(BASE + "/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, top_k: 8, include_exact: true }),
      signal: inflight.signal,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      $results.replaceChildren(
        stateCard("⚠", "خطا در جست‌وجو", err.detail || `کد خطا: ${r.status}`, true)
      );
      return;
    }
    renderResults(await r.json());
  } catch (e) {
    if (e && e.name === "AbortError") return; // superseded by a newer query
    $results.replaceChildren(
      stateCard("⚠", "ارتباط با موتور برقرار نشد", "موتور در دسترس نیست؛ دوباره تلاش کنید.", true)
    );
  } finally {
    $go.disabled = false;
    inflight = null;
    ($formMini.hidden ? $q : $qMini).focus();
  }
}

$form.addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch($q.value.trim());
});

$formMini.addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch($qMini.value.trim());
});

document.querySelectorAll(".chip").forEach((c) =>
  c.addEventListener("click", () => {
    $q.value = c.dataset.q;
    runSearch(c.dataset.q);
  })
);
/* ---------------- repair / reset + setup guard ---------------- */
async function setupGuardAndRepair() {
  const inv = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
  if (!inv) return; // plain-browser dev: no Tauri bridge
  try {
    const raw = await inv("setup_state");
    const st = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (st && st.complete === false) {
      location.replace("wizard.html");
      return;
    }
  } catch (e) { /* bridge not ready — continue */ }

  const $repair = document.getElementById("repair-btn");
  if ($repair) {
    $repair.addEventListener("click", async () => {
      const ok = confirm(
        "راه‌اندازی دوباره اجرا شود؟\n\n" +
        "«تعمیر راه‌اندازی» مراحل نصب و آماده‌سازی را از نو بررسی و تکمیل می‌کند " +
        "(داده‌های دانلودشده حفظ می‌شوند)."
      );
      if (!ok) return;
      try {
        await inv("bootstrap_run", { step: "state", args: ["--set", "complete=false"] });
      } catch (e) { /* best effort */ }
      location.replace("wizard.html");
    });
  }
}

/* ---------------- init ---------------- */
(async function init() {
  initTheme();
  setupGuardAndRepair();
  const port = await resolvePort();
  BASE = `http://127.0.0.1:${port}`;
  const ok = await waitBackend();
  if (ok) {
    $q.focus();
    setInterval(async () => {
      try {
        const r = await fetch(BASE + "/health");
        if (r.ok) updateStatusFromHealth(await r.json());
      } catch (e) { setStatus("err", "موتور پاسخ نمی‌دهد"); }
    }, 15000);
  }
})();
