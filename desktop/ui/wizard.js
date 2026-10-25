/* Dehkhoda Setup Wizard — state machine (vanilla JS, RTL).
 * 7 locked steps: خوش آمدید / بررسی سیستم / نصب پیش‌نیازها /
 * آماده‌سازی دیتابیس برداری / دانلود مدل زبانی / آماده‌سازی لغت‌نامه / آماده است.
 * Idempotent: completed steps are skipped on re-runs; a healthy install
 * lands directly on «آماده است» in well under 10 seconds.
 */
"use strict";

const B = window.WizardBridge;
const $ = (id) => document.getElementById(id);

const $panelTitle = $("panel-title");
const $panelDesc = $("panel-desc");
const $panelBody = $("panel-body");
const $panelActions = $("panel-actions");
const $log = $("log-output");
const $statusText = $("status-text");
const $statusDot = $("status-dot");
const $foot = $("foot-note");

const STEP_IDS = ["welcome", "system", "prereqs", "database", "model", "dictionary", "ready"];

let S = {
  state: { complete: false, steps: {}, backend: null, embedder: "e5-base" },
  report: null,          // latest detector report
  consent: null,         // docker consent decision (null = not asked)
  modelSkipped: false,
  busy: false,
};

/* ---------------- small helpers ---------------- */
function faNum(n) { return Number(n).toLocaleString("fa-IR"); }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function log(line) {
  $log.textContent += line + "\n";
  const pre = $log;
  pre.scrollTop = pre.scrollHeight;
}
function setStatus(kind, text) {
  $statusDot.className = "dot " + (kind === "on" ? "dot-on" : kind === "err" ? "dot-err" : "dot-off");
  $statusText.textContent = text;
}
function markStep(n, status) {
  const node = document.querySelector(`.step[data-step="${n}"]`);
  if (!node) return;
  node.classList.remove("active", "done", "skipped");
  if (status) node.classList.add(status);
}
function markAllFromState() {
  const done = S.state.steps || {};
  STEP_IDS.forEach((id, i) => {
    if (done[id] === "done") markStep(i + 1, "done");
    else if (done[id] === "skipped") markStep(i + 1, "skipped");
  });
}
function setHead(title, desc) { $panelTitle.textContent = title; $panelDesc.textContent = desc || ""; }
function clearPanel() { $panelBody.replaceChildren(); $panelActions.replaceChildren(); }
function button(label, cls, onclick) {
  const b = el("button", cls, esc(label));
  b.type = "button";
  b.addEventListener("click", onclick);
  return b;
}
function currentStepIndex() { return STEP_IDS.indexOf(S.currentStep); }

/* ---------------- backend decision -> Persian ---------------- */
const BACKEND_FA = {
  milvus: "Milvus (داکر) — موتور اصلی",
  milvus_lite: "Milvus Lite — حالت سبک بدون داکر",
  qdrant_native: "Qdrant — موتور جایگزین داخلی",
  milvus_pending_consent: "Milvus (در گام بعد پرسیده می‌شود)",
  impossible: "غیرقابل نصب روی این سامانه",
};
const REASON_FA = {
  docker_available: "داکر فعال است",
  docker_installable: "داکر قابل نصب است (با تأیید شما)",
  docker_declined: "نصب داکر را رد کردید",
  docker_unavailable: "داکر در دسترس نیست",
  milvus_lite_available: "نسخهٔ سبک Milvus در دسترس است",
  milvus_lite_unavailable: "نسخهٔ سبک Milvus در دسترس نیست",
  qdrant_native_fallback: "استفاده از موتور جایگزین Qdrant",
  qdrant_native_fallback_offer: "پیشنهاد موتور جایگزین Qdrant",
  musl_not_supported: "توزیع‌های musl پشتیبانی نمی‌شوند",
  no_qdrant_windows_arm64: "برای ویندوز ARM64 موتور جایگزینی موجود نیست",
};

/* ---------------- wizard-event plumbing ---------------- */
let liveProgress = null; // {fill, task, pct}

B.onWizardEvent((obj) => {
  if (!obj) return;
  if (obj.t === "progress" || obj.t === "log") {
    if (obj.log) log(obj.log);
    if (liveProgress) {
      if (obj.task_fa) liveProgress.task.textContent = obj.task_fa;
      if (obj.indet) { liveProgress.fill.classList.add("indet"); liveProgress.pct.textContent = ""; }
      else if (typeof obj.pct === "number" && obj.pct > 0) {
        liveProgress.fill.classList.remove("indet");
        liveProgress.fill.style.width = obj.pct + "%";
        liveProgress.pct.textContent = faNum(obj.pct) + "٪";
      }
    }
  }
});

/* ---------------- step runner with live UI ---------------- */
async function runStep(stepName, args, panel) {
  S.busy = true;
  if (panel) {
    const track = el("div", "progress-track");
    const fill = el("div", "progress-fill");
    track.appendChild(fill);
    const taskLine = el("div", "task-line");
    const task = el("span", null, "در حال اجرا…");
    const pct = el("span", "pct", "");
    taskLine.append(task, pct);
    panel.prepend(taskLine, track);
    liveProgress = { fill, task, pct };
  }
  try {
    const res = await B.runStep(stepName, args);
    if (res && res.ok && res.data) log("RESULT " + JSON.stringify(res.data).slice(0, 4000));
    if (res && !res.ok) {
      (res.log_tail || []).forEach(log);
      if (res.error_fa) log("ERROR " + res.error_fa);
    }
    return res;
  } finally {
    S.busy = false;
    liveProgress = null;
  }
}

/* =========================================================================
   STEP 1 — خوش آمدید
   ========================================================================= */
function renderWelcome() {
  S.currentStep = "welcome";
  markStep(1, S.state.steps.welcome === "done" ? "done" : "active");
  setStatus("off", "آمادهٔ شروع");
  setHead("به فرهنگ دهخدا خوش آمدید",
    "چند دقیقه وقت می‌گذاریم تا همه‌چیز برای جست‌وجوی معنایی آماده شود.");
  clearPanel();
  const c = el("div", "wcard");
  c.innerHTML = `
    <h3>این برنامه چه کاری برای شما انجام می‌دهد؟</h3>
    <p>کافی است «معنا» یا «توصیف» یک واژه را بنویسید؛ سرواژهٔ مربوط را از میان
    ۳۱۲٬۵۰۷ مدخل دهخدا می‌یابیم. همهٔ مراحل راه‌اندازی به‌صورت خودکار و بدون
    خط فرمان انجام می‌شود.</p>`;
  const c2 = el("div", "wcard");
  c2.innerHTML = `
    <h3>چه چیزی نصب یا دریافت می‌شود؟</h3>
    <p>موتور جست‌وجوی برداری (در صورت نیاز با پنجرهٔ رمز سیستم‌عامل)،
    مدل زبانی حدود ۱٫۱ گیگابایت و داده‌های لغت‌نامه حدود ۲۹۰ مگابایت —
    همه در پوشهٔ کاربری شما.</p>`;
  $panelBody.append(c, c2);
  $panelActions.append(
    button("ادامه", "btn-primary", () => {
      markStep(1, "done");
      const done = S.state.steps || {};
      if (done.system === "done" || S.report) renderSystem();
      else renderSystem();
    }),
    button("انصراف", "btn-ghost", cancelWizard)
  );
}

function cancelWizard() {
  if (B.isTauri && window.__TAURI__.window) {
    window.__TAURI__.window.getCurrentWindow().close();
  } else {
    setStatus("off", "لغو شد (نمونهٔ نمایشی)");
  }
}

/* =========================================================================
   STEP 2 — بررسی سیستم
   ========================================================================= */
function checkRow(icon, ok, label, value) {
  const cls = ok === true ? "c-ok" : ok === false ? "c-bad" : "c-warn";
  const glyph = ok === true ? "✓" : ok === false ? "✕" : "!";
  return el("div", "check-row",
    `<span class="c-icon ${cls}">${glyph}</span>
     <span class="c-label">${esc(label)}</span>
     <span class="c-value">${esc(value || "")}</span>`);
}

async function renderSystem() {
  S.currentStep = "system";
  markStep(2, "active");
  setStatus("off", "در حال بررسی سیستم…");
  setHead("بررسی سیستم", "سخت‌افزار و نرم‌افزار شما برای بهترین گزینه بررسی می‌شود.");
  clearPanel();
  const card = el("div", "wcard");
  for (let i = 0; i < 4; i++) card.appendChild(el("div", "skeleton-line"));
  $panelBody.appendChild(card);

  const res = await runStep("report", S.consent === null ? [] : ["--consent", S.consent ? "1" : "0"], null);
  card.replaceChildren();
  if (!res.ok) {
    card.className = "wcard error-card";
    card.appendChild(el("h3", null, "بررسی سیستم ناموفق بود"));
    card.appendChild(el("p", null, esc(res.error_fa || "خطای نامشخص")));
    $panelActions.append(button("تلاش مجدد", "btn-primary", renderSystem));
    setStatus("err", "بررسی ناموفق");
    return;
  }
  S.report = res.data;
  const r = S.report;
  const be = r.backend || {};
  S.backendPath = be.path;

  card.appendChild(checkRow(true, true, "سیستم‌عامل", `${r.os.pretty} • ${r.os.arch === "x86_64" ? "۶۴بیتی" : r.os.arch}`));
  if (r.os.type === "linux") {
    card.appendChild(checkRow(true, !!r.linux.pkexec, "مجوز ارتقای سیستمی (pkexec)", r.linux.pkexec ? "موجود" : "یافت نشد"));
  }
  if (r.windows) {
    card.appendChild(checkRow(r.windows.winget, r.windows.winget, "winget (نصب خودکار)", r.windows.winget ? "موجود" : "یافت نشد"));
    card.appendChild(checkRow(true, r.windows.wsl2, "WSL2", r.windows.wsl2 ? "فعال" : "غیرفعال (در صورت نیاز نصب می‌شود)"));
  }
  card.appendChild(checkRow(true, r.docker.installed, "داکر",
    r.docker.installed ? (r.docker.running ? "نصب و فعال" : "نصب شده، غیرفعال") : "نصب نیست"));
  card.appendChild(checkRow(r.resources.disk_ok, r.resources.disk_ok,
    "فضای دیسک", `${faNum(r.resources.disk_free_gb)} گیگابایت آزاد`));
  card.appendChild(checkRow(true, r.resources.ram_ok, "حافظهٔ رم",
    `${faNum(r.resources.ram_gb)} گیگابایت` + (r.resources.ram_recommended ? "" : " (کمتر از حد توصیه‌شده)")));
  const beRow = checkRow(be.path !== "impossible", be.path !== "impossible",
    "گزینهٔ انتخابی موتور", BACKEND_FA[be.path] || be.path);
  card.appendChild(beRow);
  (be.reasons || []).forEach((reason) => {
    card.appendChild(el("div", "check-row",
      `<span class="c-icon"></span><span class="c-label" style="color:var(--muted);font-size:12.5px">
       ${esc(REASON_FA[reason] || reason)}</span>`));
  });

  $panelActions.append(
    button("ادامه", "btn-primary", () => renderPrereqs()),
    button("انصراف", "btn-ghost", cancelWizard)
  );
  setStatus("on", "بررسی کامل شد");
  markStep(2, "done");
}

/* =========================================================================
   STEP 3 — نصب پیش‌نیازها
   ========================================================================= */
async function renderPrereqs() {
  S.currentStep = "prereqs";
  markStep(3, "active");
  setHead("نصب پیش‌نیازها", "در صورت نیاز، اجزای گم‌شده با اجازهٔ شما نصب می‌شوند.");
  clearPanel();

  const be = (S.report && S.report.backend) || { path: "unknown" };

  if (be.path === "impossible") {
    const c = el("div", "wcard error-card");
    c.appendChild(el("h3", null, "این سامانه پشتیبانی نمی‌شود"));
    c.appendChild(el("p", null,
      "متأسفانه برای این توزیع/معماری امکان نصب خودکار وجود ندارد. توزیع‌های پشتیبانی‌شده: Ubuntu/Debian، Arch، Fedora، openSUSE و ویندوز ۱۰/۱۱."));
    $panelActions.append(button("تلاش مجدد", "btn-primary", renderSystem));
    setStatus("err", "سامانه پشتیبانی نمی‌شود");
    return;
  }

  if (be.path === "milvus" && S.report.docker && S.report.docker.running) {
    const c = el("div", "wcard");
    c.appendChild(el("h3", null, "پیش‌نیازها آماده است"));
    c.appendChild(el("p", null, "داکر فعال است؛ نیازی به نصب چیزی نیست."));
    $panelActions.append(button("ادامه", "btn-primary", renderDatabase));
    markStep(3, "done");
    setStatus("on", "پیش‌نیازها آماده");
    return;
  }

  if (be.path === "milvus_pending_consent") {
    const c = el("div", "wcard decision");
    c.appendChild(el("h3", null, "نصب موتور داکر"));
    c.appendChild(el("p", null,
      "برای اجرای Milvus (موتور اصلی) به داکر نیاز است. با زدن «ادامه»، به‌صورت خودکار نصب می‌شود؛ تنها پنجرهٔ رمز سیستم‌عامل (UAC/polkit) باز خواهد شد."));
    const actions = el("div", "d-actions");
    actions.append(
      button("ادامه (نصب داکر)", "btn-primary", () => decideDocker(true)),
      button("رد کردن", "btn-ghost", () => decideDocker(false))
    );
    c.appendChild(actions);
    $panelBody.appendChild(c);
    $panelActions.append(button("انصراف", "btn-ghost", cancelWizard));
    setStatus("off", "در انتظار تصمیم شما");
    return;
  }

  if (be.path === "qdrant_native") {
    const needConfirm = (be.needs || []).includes("confirm_qdrant_fallback");
    const c = el("div", "wcard" + (needConfirm ? " decision" : ""));
    c.appendChild(el("h3", null, "موتور جایگزین: Qdrant"));
    c.appendChild(el("p", null,
      needConfirm
        ? "داکر در دسترس نیست (یا رد شد) و نسخهٔ سبک Milvus نیز روی این سامانه موجود نیست. موتور داخلی Qdrant — بدون نیاز به هیچ پیش‌نیازی — استفاده می‌شود."
        : "از موتور داخلی Qdrant استفاده می‌شود؛ هیچ پیش‌نیاز سیستمی لازم نیست."));
    $panelActions.append(button("ادامه", "btn-primary", renderDatabase));
    setStatus("on", "پیش‌نیاز لازم ندارد");
    markStep(3, needConfirm ? "skipped" : "done");
    return;
  }

  // docker installed but not running -> prereqs step starts it
  await doPrereqsRun();
}

async function decideDocker(consent) {
  S.consent = consent;
  // persist the decision so Python + Rust stay in sync
  await B.runStep("state", ["--set", "decisions=" + JSON.stringify({ docker_consent: consent })]);
  const res = await runStep("report", ["--consent", consent ? "1" : "0"], null);
  if (res.ok) {
    S.report = res.data;
    S.backendPath = res.data.backend.path;
  }
  if (consent) {
    markStep(3, "active");
    await doPrereqsRun();
  } else {
    // declined -> fallback path chosen by decision table
    markStep(3, "skipped");
    renderDatabase();
  }
}

async function doPrereqsRun() {
  setStatus("off", "در حال نصب پیش‌نیازها…");
  clearPanel();
  const c = el("div", "wcard");
  c.appendChild(el("h3", null, "در حال نصب…"));
  c.appendChild(el("p", null, "اگر پنجرهٔ رمز سیستم‌عامل باز شد، رمز خود را وارد کنید."));
  $panelBody.appendChild(c);
  const res = await runStep("prereqs", [], c);
  if (!res.ok) {
    clearPanel();
    const err = el("div", "wcard error-card");
    err.appendChild(el("h3", null, "نصب پیش‌نیاز ناموفق بود"));
    err.appendChild(el("p", null, esc(res.error_fa || "")));
    $panelBody.appendChild(err);
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", renderPrereqs),
      button("ادامه بدون داکر (موتور جایگزین)", "btn-ghost", async () => {
        await B.runStep("state", ["--set", "backend=qdrant_native"]);
        await B.runStep("state", ["--set", "decisions=" + JSON.stringify({ docker_consent: false })]);
        renderDatabase();
      }),
      button("انصراف", "btn-ghost", cancelWizard)
    );
    setStatus("err", "نصب ناموفق");
    return;
  }
  if (res.data && res.data.skipped) {
    markStep(3, "skipped");
  } else {
    markStep(3, "done");
    // refresh report after installs
    const rep = await runStep("report", [], null);
    if (rep.ok) { S.report = rep.data; S.backendPath = rep.data.backend.path; }
  }
  setStatus("on", "پیش‌نیازها آماده");
  renderDatabase();
}

/* =========================================================================
   STEP 4 — آماده‌سازی دیتابیس برداری
   ========================================================================= */
async function renderDatabase() {
  S.currentStep = "database";
  markStep(4, "active");
  setHead("آماده‌سازی دیتابیس برداری",
    (BACKEND_FA[S.backendPath || (S.report && S.report.backend.path)] || "") +
    " — این مرحله ممکن است در اولین اجرا چند دقیقه طول بکشد.");
  clearPanel();
  const c = el("div", "wcard");
  $panelBody.appendChild(c);

  const res = await runStep("db", [], c);
  if (!res.ok) {
    clearPanel();
    const err = el("div", "wcard error-card");
    err.appendChild(el("h3", null, "راه‌اندازی موتور برداری ناموفق بود"));
    err.appendChild(el("p", null, esc(res.error_fa || "")));
    $panelBody.appendChild(err);
    const canFallback = S.backendPath !== "qdrant_native";
    if (canFallback) {
      $panelActions.append(button("استفاده از موتور جایگزین (Qdrant)", "btn-primary", async () => {
        await B.runStep("state", ["--set", "backend=qdrant_native"]);
        S.backendPath = "qdrant_native";
        renderDatabase();
      }));
    }
    $panelActions.append(
      button("تلاش مجدد", canFallback ? "btn-ghost" : "btn-primary", renderDatabase),
      button("انصراف", "btn-ghost", cancelWizard)
    );
    setStatus("err", "موتور برداری آماده نشد");
    return;
  }
  S.runtime = res.data;
  markStep(4, "done");
  setStatus("on", "موتور برداری آماده است");
  renderModel();
}

/* =========================================================================
   STEP 5 — دانلود مدل زبانی
   ========================================================================= */
async function renderModel() {
  S.currentStep = "model";
  markStep(5, S.state.steps.model === "done" ? "done" : "active");
  setHead("دانلود مدل زبانی",
    "مدل چندزبانهٔ e5-base (۱٫۱ گیگابایت) برای جست‌وجوی معنایی؛ فقط یک‌بار دانلود می‌شود.");
  clearPanel();

  const c = el("div", "wcard");
  c.appendChild(el("h3", null, "چرا مدل زبانی؟"));
  c.appendChild(el("p", null,
    "این مدل «معنای» توصیف شما را با تعاریف دهخدا مقایسه می‌کند. بدون آن، جست‌وجو فقط کلیدواژه‌ای خواهد بود."));
  $panelBody.appendChild(c);

  $panelActions.append(
    button("ادامه (دانلود)", "btn-primary", () => doModelRun(false)),
    button("رد کردن (در صورت امکان)", "btn-ghost", () => doModelRun(true)),
    button("انصراف", "btn-ghost", cancelWizard)
  );
  setStatus("off", "در انتظار شروع دانلود");
}

async function doModelRun(skip) {
  clearPanel();
  const c = el("div", "wcard");
  $panelBody.appendChild(c);
  const res = await runStep("model", skip ? ["--skip"] : [], c);
  if (!res.ok) {
    clearPanel();
    const err = el("div", "wcard error-card");
    err.appendChild(el("h3", null, "دانلود مدل ناموفق بود"));
    err.appendChild(el("p", null, esc(res.error_fa || "")));
    $panelBody.appendChild(err);
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", () => doModelRun(skip)),
      button("رد کردن و ادامه", "btn-ghost", () => doModelRun(true))
    );
    setStatus("err", "دانلود ناموفق");
    return;
  }
  if (res.data && res.data.skipped) {
    S.modelSkipped = true;
    markStep(5, "skipped");
    const warn = el("div", "wcard");
    warn.appendChild(el("h3", null, "حالت سبک فعال شد"));
    warn.appendChild(el("p", null,
      "مدل دانلود نشد؛ جست‌وجو کیفیت معنایی کمتری خواهد داشت. هر زمان می‌توانید از منوی «تعمیر راه‌اندازی» مدل را اضافه کنید."));
    $panelBody.appendChild(warn);
    setStatus("on", "حالت سبک فعال شد");
  } else {
    markStep(5, "done");
    setStatus("on", "مدل آماده است");
  }
  renderDictionary();
}

/* =========================================================================
   STEP 6 — آماده‌سازی لغت‌نامه دهخدا
   ========================================================================= */
async function renderDictionary() {
  S.currentStep = "dictionary";
  markStep(6, "active");
  setHead("آماده‌سازی لغت‌نامه دهخدا",
    "۳۱۲٬۵۰۶ مدخل پردازش و درج می‌شود؛ در صورت قطع‌شدن، از همان‌جا ادامه می‌دهیم.");
  clearPanel();
  const c = el("div", "wcard");
  $panelBody.appendChild(c);

  const res = await runStep("ingest", [], c);
  if (!res.ok) {
    clearPanel();
    const err = el("div", "wcard error-card");
    err.appendChild(el("h3", null, "آماده‌سازی لغت‌نامه متوقف شد"));
    err.appendChild(el("p", null, esc(res.error_fa || "")));
    $panelBody.appendChild(err);
    $panelActions.append(
      button("تلاش مجدد (ادامه از آخرین نقطه)", "btn-primary", renderDictionary),
      button("انصراف", "btn-ghost", cancelWizard)
    );
    setStatus("err", "آماده‌سازی متوقف شد");
    return;
  }
  S.ingestStats = res.data || {};
  markStep(6, "done");
  setStatus("on", "لغت‌نامه آماده است");
  renderReady();
}

/* =========================================================================
   STEP 7 — آماده است
   ========================================================================= */
async function renderReady() {
  S.currentStep = "ready";
  markStep(7, "done");
  setHead("همه‌چیز آماده است!", "از انتخاب شما سپاسگزاریم؛ حالا واژه‌یاب معنایی در خدمت شماست.");
  clearPanel();
  const be = S.backendPath || (S.report && S.report.backend.path);
  const hero = el("div", "wcard ready-hero");
  hero.appendChild(el("div", "big", "🎉"));
  hero.appendChild(el("h3", null, "فرهنگ دهخدا آمادهٔ جست‌وجو است"));
  const stats = el("div", "ready-stats");
  const stat = (v, k) => el("div", "stat", `<b>${esc(v)}</b>${esc(k)}`);
  stats.append(
    stat(faNum(312506), "مدخل دهخدا"),
    stat(BACKEND_FA[be] ? BACKEND_FA[be].split("—")[0].trim() : be, "موتور جست‌وجو"),
    stat(S.modelSkipped ? "حالت سبک" : "e5-base", "مدل معنایی")
  );
  if (S.ingestStats && S.ingestStats.chunks) {
    stats.append(stat(faNum(S.ingestStats.chunks), "بخش برداری"));
  }
  hero.appendChild(stats);
  $panelBody.appendChild(hero);

  $panelActions.append(
    button("شروع به کار", "btn-primary", async () => {
      await B.runStep("state", ["--set", "complete=true"]);
      try { await B.invoke("start_backend"); } catch (e) { /* browser demo */ }
      location.replace("index.html");
    })
  );
  setStatus("on", "آماده!");
}

/* =========================================================================
   boot
   ========================================================================= */
async function boot() {
  initTheme();
  try {
    const st = await B.invoke("setup_state");
    if (st && typeof st === "object" && st.steps !== undefined) S.state = st;
  } catch (e) {
    log("setup_state unavailable: " + e.message);
  }
  markAllFromState();

  // idempotent fast path: healthy install -> straight to ready (<10s)
  if (S.state.complete) {
    renderReady();
    return;
  }
  const done = S.state.steps || {};
  if (done.dictionary === "done") renderReady();
  else if (done.model === "done") renderDictionary();
  else if (done.database === "done") renderModel();
  else if (done.system === "done") renderPrereqs();
  else renderWelcome();
}

/* theme (same mechanism as the main app) */
const THEME_KEY = "dehkhoda-theme";
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const dark = theme === "dark";
  document.getElementById("icon-moon").hidden = dark;
  document.getElementById("icon-sun").hidden = !dark;
}
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}
document.getElementById("theme-toggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const next = cur === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
});

boot();
