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
  chosenBackend: null,   // user's engine choice (null = default Qdrant)
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
/* error cards: Persian summary + exact technical reason (collapsible) */
function errorCard(title, faMsg, technical) {
  const c = el("div", "wcard error-card");
  c.appendChild(el("h3", null, title));
  c.appendChild(el("p", null, faMsg || ""));
  if (technical) {
    const det = el("details", "hit-details");
    det.appendChild(el("summary", null, "جزئیات فنی خطا"));
    det.appendChild(el("div", "hit-full", esc(String(technical))));
    c.appendChild(det);
  }
  return c;
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
  milvus: "Milvus (داکر) — حالت پیشرفته",
  milvus_lite: "Milvus Lite — گزینهٔ اختیاری (Ubuntu)",
  qdrant_native: "Qdrant — موتور پیش‌فرض داخلی (بدون داکر)",
  qdrant_local: "Qdrant — حالت تعبیه‌شده (بدون فرایند جدا)",
  milvus_pending_consent: "Milvus (در گام بعد پرسیده می‌شود)",
  impossible: "غیرقابل نصب روی این سامانه",
};
const REASON_FA = {
  docker_available: "داکر فعال است (حالت پیشرفتهٔ Milvus در دسترس)",
  docker_installable: "داکر قابل نصب است (فقط برای حالت پیشرفته)",
  docker_declined: "نصب داکر را رد کردید",
  docker_unavailable: "داکر در دسترس نیست — موتور داخلی استفاده می‌شود",
  qdrant_default_selfcontained: "موتور پیش‌فرض Qdrant — بدون نیاز به داکر یا نصب چیزی",
  milvus_lite_available: "نسخهٔ سبک Milvus در دسترس است",
  milvus_lite_unavailable: "نسخهٔ سبک Milvus در دسترس نیست",
  qdrant_native_fallback: "استفاده از موتور جایگزین Qdrant",
  qdrant_native_fallback_offer: "پیشنهاد موتور جایگزین Qdrant",
  musl_not_supported: "توزیع‌های musl پشتیبانی نمی‌شوند",
  no_qdrant_windows_arm64: "برای ویندوز ARM64 موتور جایگزینی موجود نیست",
};

/* ---------------- Persian quantity helpers ---------------- */
function faMB(bytes) { return faNum(Math.round(bytes / 1e6)); }
function faMB1(bytes) { return faNum(Math.round(bytes / 1e5) / 10); }
function faSpeed(bps) {
  if (!bps || bps < 1) return "—";
  if (bps >= 1048576) return faNum(Math.round(bps / 104857) / 10) + " مگابایت/ثانیه";
  return faNum(Math.round(bps / 1024)) + " کیلوبایت/ثانیه";
}
function faEta(sec) {
  if (!sec || sec < 1 || !isFinite(sec)) return "—";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  if (m <= 0) return faNum(s) + " ثانیه";
  if (m < 60) return faNum(m) + " دقیقه" + (s ? " و " + faNum(s) + " ثانیه" : "");
  return faNum(Math.floor(m / 60)) + " ساعت و " + faNum(m % 60) + " دقیقه";
}

/* ---------------- wizard-event plumbing ---------------- */
let liveProgress = null; // {fill, task, pct, dlLine}

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
      // structured download progress (model_download v1.3):
      // {file, bytes_done, bytes_total, speed_bps, eta_seconds, overall_percent}
      // v1.3.1 restore progress: {stage, bytes_done, bytes_total, duration_ms}
      if (obj.dl && liveProgress.dlLine) {
        const d = obj.dl;
        liveProgress.dlLine.hidden = false;
        if (d.stage) {
          liveProgress.dlLine.textContent =
            (RESTORE_STAGE_FA[d.stage] || "در حال بازیابی…") +
            (d.duration_ms ? " — " + faEta(Math.round(d.duration_ms / 1000)) : "");
        } else {
          liveProgress.dlLine.textContent =
            (d.phase === "verified" ? "تأیید شد: " : "در حال دانلود: ") +
            d.file + " — " + faSpeed(d.speed_bps) + " — مانده " + faEta(d.eta_seconds);
        }
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
    const dlLine = el("div", "dl-line");
    dlLine.hidden = true;
    const taskLine = el("div", "task-line");
    const task = el("span", null, "در حال اجرا…");
    const pct = el("span", "pct", "");
    taskLine.append(task, pct);
    panel.prepend(dlLine, taskLine, track);
    liveProgress = { fill, task, pct, dlLine };
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
    <p>موتور جست‌وجوی برداری (در صورت نیاز با پنجرهٔ رمز سیستم‌عامل) و
    مدل زبانی حدود ۱٫۱ گیگابایت و داده‌های لغت‌نامه حدود ۲۹۰ مگابایت —
    همه در پوشهٔ کاربری شما. در «نسخهٔ کامل» مدل زبانی همراه بسته است و
    اینترنت لازم ندارد؛ در «نسخهٔ سبک» فقط در اولین اجرا دانلود می‌شود.</p>`;
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
    "موتور جست‌وجو (پیش‌فرض)", BACKEND_FA[be.path] || be.path);
  card.appendChild(beRow);
  (be.reasons || []).forEach((reason) => {
    card.appendChild(el("div", "check-row",
      `<span class="c-icon"></span><span class="c-label" style="color:var(--muted);font-size:12.5px">
       ${esc(REASON_FA[reason] || reason)}</span>`));
  });

  // Advanced engine chooser — Milvus via Docker is OPTIONAL (spec v1.2.1).
  const advanced = be.advanced || [];
  if (advanced.length) {
    const adv = el("details", "wcard");
    adv.appendChild(el("summary", null, "حالت پیشرفته: موتور Milvus با داکر"));
    adv.appendChild(el("p", null,
      "موتور پیش‌فرض (Qdrant) هیچ پیش‌نیازی ندارد و توصیه می‌شود همان بماند. " +
      "اگر Milvus مستقل را ترجیح می‌دهید، داکر لازم است؛ در گام بعد با پنجرهٔ رمز سیستم‌عامل نصب/راه‌اندازی می‌شود."));
    adv.appendChild(button(
      advanced.includes("milvus") ? "انتخاب Milvus (داکر فعال است)" : "انتخاب Milvus (نصب داکر با اجازهٔ شما)",
      "btn-ghost", async () => {
        S.chosenBackend = "milvus";
        await B.runStep("state", ["--set", "backend=milvus"]);
        await B.runStep("state", ["--set", "decisions=" + JSON.stringify({ docker_consent: true })]);
        markStep(3, "active");
        renderPrereqs();
      }));
    $panelBody.appendChild(adv);
  }

  $panelActions.append(
    button("ادامه", "btn-primary", () => advanceFromSystem()),
    button("انصراف", "btn-ghost", cancelWizard)
  );
  setStatus("on", "بررسی کامل شد");
  markStep(2, "done");
}

/* Default flow: Qdrant needs NO prerequisites — skip step 3 entirely. */
async function advanceFromSystem() {
  if (S.chosenBackend === "milvus") {
    renderPrereqs();
    return;
  }
  await B.runStep("state", ["--set", "backend=qdrant_native"]);
  markStep(3, "skipped");
  renderDatabase();
}

/* =========================================================================
   STEP 3 — نصب پیش‌نیازها (فقط برای حالت پیشرفتهٔ Milvus/داکر)
   ========================================================================= */
async function renderPrereqs() {
  S.currentStep = "prereqs";
  markStep(3, "active");
  setHead("نصب پیش‌نیازها", "در صورت نیاز، اجزای گم‌شده با اجازهٔ شما نصب می‌شوند.");
  clearPanel();

  const be = (S.report && S.report.backend) || { path: "unknown" };
  const wantsDocker = S.chosenBackend === "milvus" || be.path === "milvus" ||
    be.path === "milvus_pending_consent";

  if (be.path === "impossible") {
    const c = el("div", "wcard error-card");
    c.appendChild(el("h3", null, "این سامانه پشتیبانی نمی‌شود"));
    c.appendChild(el("p", null,
      "متأسفانه برای این توزیع/معماری امکان نصب خودکار وجود ندارد. توزیع‌های پشتیبانی‌شده: Ubuntu/Debian، Arch، Fedora، openSUSE و ویندوز ۱۰/۱۱."));
    $panelActions.append(button("تلاش مجدد", "btn-primary", renderSystem));
    setStatus("err", "سامانه پشتیبانی نمی‌شود");
    return;
  }

  if (!wantsDocker) {
    // Default Qdrant path: nothing to install. (Also the resume path when
    // the wizard reopens after step 2 was completed.)
    const c = el("div", "wcard");
    c.appendChild(el("h3", null, "پیش‌نیازی لازم نیست"));
    c.appendChild(el("p", null,
      "موتور پیش‌فرض Qdrant داخل برنامه است؛ بدون داکر و بدون نصب هیچ چیزی اجرا می‌شود."));
    $panelBody.appendChild(c);
    $panelActions.append(button("ادامه", "btn-primary", renderDatabase));
    markStep(3, "skipped");
    setStatus("on", "پیش‌نیاز لازم ندارد");
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
    // Default engine: no prereqs (kept for compatibility with older reports).
    const c = el("div", "wcard");
    c.appendChild(el("h3", null, "موتور داخلی: Qdrant"));
    c.appendChild(el("p", null,
      "از موتور داخلی Qdrant استفاده می‌شود؛ هیچ پیش‌نیاز سیستمی لازم نیست."));
    $panelActions.append(button("ادامه", "btn-primary", renderDatabase));
    setStatus("on", "پیش‌نیاز لازم ندارد");
    markStep(3, "skipped");
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
  const chosen = S.chosenBackend || S.state.backend || "qdrant_native";
  setHead("آماده‌سازی دیتابیس برداری",
    (BACKEND_FA[chosen] || BACKEND_FA.qdrant_native) +
    " — در صورت بروز خطا، موتور جایگزین به‌صورت خودکار امتحان می‌شود.");
  clearPanel();
  const c = el("div", "wcard");
  $panelBody.appendChild(c);

  const res = await runStep("db", [], c);
  if (!res.ok) {
    clearPanel();
    const err = el("div", "wcard error-card");
    err.appendChild(el("h3", null, "راه‌اندازی موتور برداری ناموفق بود"));
    err.appendChild(el("p", null, esc(res.error_fa || "")));
    // Requirement: exact per-engine diagnostics, never a generic message.
    // Each failed attempt's error + missing shared libraries are rendered
    // here AND were already streamed into the «جزئیات فنی» panel below.
    const attempts = (res.data && res.data.attempts) || [];
    const shown = attempts.filter((a) => !a.skipped);
    if (shown.length) {
      const det = el("details", "hit-details");
      det.appendChild(el("summary", null, "خطای دقیق هر موتور"));
      shown.forEach((a) => {
        const line = `[${a.engine}] ${a.error || ""}` +
          (a.missing_libs && a.missing_libs.length
            ? ` — کتابخانهٔ گم‌شده: ${a.missing_libs.join(", ")}` : "");
        det.appendChild(el("div", "hit-full", esc(line)));
      });
      err.appendChild(det);
    }
    $panelBody.appendChild(err);
    const dockerAvailable = !!(res.data && res.data.docker_available);
    if (dockerAvailable && chosen !== "milvus") {
      $panelActions.append(button("امتحان Milvus با داکر (پیشرفته)", "btn-primary", async () => {
        await B.runStep("state", ["--set", "backend=milvus"]);
        await B.runStep("state", ["--set", "decisions=" + JSON.stringify({ docker_consent: true })]);
        S.chosenBackend = "milvus";
        renderPrereqs();
      }));
    }
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", renderDatabase),
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
   STEP 5 — مدل زبانی (v1.3.0: دو نسخهٔ full/lean)
   full: «مدل زبانی همراه بسته نصب شد.» — verify once, warn-only, no download
   lean: pre-flight (disk 2x + HEAD 5s) → resume prompt → progress bar with
         file + speed + ETA + Cancel → sha256 verify → «آماده»
   ========================================================================= */
function modelVariant() {
  return (S.report && S.report.model && S.report.model.variant) || "lean";
}
const MODEL_RELEASE_URL = "https://github.com/montajebii/reverse-DehKhoda/releases/latest";

async function renderModel() {
  S.currentStep = "model";
  markStep(5, S.state.steps.model === "done" ? "done" : "active");
  if (modelVariant() === "full") return renderModelFull();
  return renderModelLean();
}

/* ---------- full variant: bundled weights, no download UI ---------- */
async function renderModelFull() {
  setStatus("off", "در حال بررسی مدل همراه…");
  setHead("مدل زبانی",
    "نسخهٔ کامل: مدل زبانی داخل همین بسته نصب شده و به اینترنت نیازی نیست.");
  clearPanel();

  const m = (S.report && S.report.model) || {};
  if (m.ready) {
    // .ready fast path — nothing to hash, nothing to show but the line
    modelDoneFull(true, true);
    return;
  }
  const c = el("div", "wcard");
  $panelBody.appendChild(c);
  const res = await runStep("model", [], c);
  if (!res.ok) {
    clearPanel();
    $panelBody.appendChild(errorCard("بررسی مدل همراه ناموفق بود",
      res.error_fa || "خطای نامشخص", (res.data && res.data.technical) || ""));
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", renderModelFull),
      button("بازگشت به گام قبل", "btn-ghost", renderDatabase));
    setStatus("err", "بررسی مدل ناموفق");
    return;
  }
  modelDoneFull(res.data && res.data.verified !== false, true);
}

function modelDoneFull(verified, advance) {
  clearPanel();
  const c = el("div", "wcard");
  c.appendChild(el("h3", null, "مدل زبانی همراه بسته نصب شد."));
  c.appendChild(el("p", null, verified
    ? "اثر انگشت همهٔ فایل‌های مدل با pins.json تأیید شد؛ جست‌وجوی معنایی آفلاین کار می‌کند."
    : "هشدار: اثر انگشت برخی فایل‌های مدل مطابقت ندارد (جزئیات در «جزئیات فنی»). ادامه می‌دهیم، اما توصیه می‌کنیم بسته را دوباره دریافت کنید."));
  $panelBody.appendChild(c);
  markStep(5, "done");
  setStatus("on", verified ? "مدل همراه تأیید شد" : "مدل همراه (هشدار تطابق)");
  if (advance) renderDictionary();
}

/* ---------- lean variant: pre-flight → download/resume ---------- */
async function renderModelLean() {
  setHead("مدل زبانی",
    "مدل چندزبانهٔ e5-base برای جست‌وجوی معنایی؛ فقط یک‌بار دانلود می‌شود و بعد از آن همه‌چیز آفلاین است.");
  clearPanel();
  setStatus("off", "در حال بررسی امکان دانلود…");

  const res = await runStep("model", ["--preflight"], null);
  if (!res.ok) {
    leanErrorCard(res.error_fa || "بررسی پیش‌نیازهای دانلود ناموفق بود", res);
    return;
  }
  const pf = (res.data && res.data.preflight) || {};
  S.modelPreflight = pf;

  if (pf.already_ready) { leanReadyCard(null, "مدل از قبل آماده است."); return; }

  if (!pf.network_ok) {
    const c = errorCard("به اینترنت نیاز است",
      "برای دانلود مدل در اولین اجرا، اتصال به huggingface.co لازم است. " +
      "اگر اینترنت ندارید، نسخهٔ کامل (آفلاین) را از صفحهٔ انتشار دریافت کنید.",
      pf.network_error);
    const urlRow = el("div", "model-url");
    urlRow.textContent = MODEL_RELEASE_URL;
    c.appendChild(urlRow);
    $panelBody.appendChild(c);
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", renderModelLean),
      button("راهنمای نسخهٔ آفلاین", "btn-ghost",
        () => { try { window.open(MODEL_RELEASE_URL, "_blank"); } catch (e) { /* show URL text */ } }),
      button("رد کردن (جست‌وجوی کلیدواژه‌ای)", "btn-ghost", () => doModelRun(true)));
    setStatus("err", "شبکه در دسترس نیست");
    return;
  }
  if (!pf.disk_ok) {
    const c = errorCard("فضای دیسک کافی نیست",
      `برای دانلود امن مدل به ${faMB(pf.need_bytes)} مگابایت فضای خالی نیاز است ` +
      `(حجم مدل ${faMB(pf.total_size_bytes)} مگابایت + فضای تأیید). ` +
      `الان ${faMB(pf.disk_free_bytes)} مگابایت خالی دارید.`,
      `need_bytes=${pf.need_bytes} free=${pf.disk_free_bytes} factor=2x`);
    $panelBody.appendChild(c);
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", renderModelLean),
      button("بازگشت به گام قبل", "btn-ghost", renderDatabase));
    setStatus("err", "فضای دیسک ناکافی");
    return;
  }

  if (pf.partial && pf.partial.bytes > 0) {
    leanResumeCard(pf);
    return;
  }

  leanStartCard(pf);
}

function leanStartCard(pf) {
  clearPanel();
  const c = el("div", "wcard");
  c.appendChild(el("h3", null, "آمادهٔ دانلود مدل زبانی"));
  const meta = el("ul", "model-meta");
  meta.innerHTML = `
    <li>حجم دقیق دانلود: <b>${faMB(pf.total_size_bytes)} مگابایت</b> (${faNum(pf.files.length)} فایل)</li>
    <li>محل ذخیره: <span dir="ltr" class="mono">${esc(pf.target_dir)}</span></li>
    <li>پس از دانلود، اثر انگشت همهٔ فایل‌ها با pins.json تأیید می‌شود و نیازی به اینترنت نخواهید داشت.</li>`;
  c.appendChild(meta);
  $panelBody.appendChild(c);
  $panelActions.append(
    button("شروع دانلود", "btn-primary", () => doModelDownload([])),
    button("رد کردن (در صورت امکان)", "btn-ghost", () => doModelRun(true)),
    button("انصراف", "btn-ghost", cancelWizard));
  setStatus("off", "در انتظار شروع دانلود");
}

function leanResumeCard(pf) {
  clearPanel();
  const c = el("div", "wcard decision");
  c.appendChild(el("h3", null, "دانلود مدل ناقص است. ادامه می‌دهید؟"));
  const meta = el("ul", "model-meta");
  meta.innerHTML = `
    <li>از دانلود قبلی <b>${faMB1(pf.partial.bytes)} مگابایت</b> باقی مانده است و دوباره دریافت نمی‌شود.</li>
    <li>حجم کل مدل: ${faMB(pf.total_size_bytes)} مگابایت.</li>`;
  c.appendChild(meta);
  $panelBody.appendChild(c);
  $panelActions.append(
    button("ادامهٔ دانلود", "btn-primary", () => doModelDownload(["--start"])),
    button("از ابتدا", "btn-ghost", () => doModelDownload(["--restart"])),
    button("انصراف", "btn-ghost", cancelWizard));
  setStatus("off", "دانلود ناقص — در انتظار تصمیم شما");
}

async function doModelDownload(extraArgs) {
  clearPanel();
  const c = el("div", "wcard");
  c.appendChild(el("p", null, "دانلود قابل لغو است؛ اگر لغو یا برنامه بسته شود، از همان‌جا ادامه می‌دهیم."));
  $panelBody.appendChild(c);
  $panelActions.append(
    button("لغو دانلود", "btn-ghost", async () => {
      setStatus("off", "در حال لغو…");
      await B.cancelStep();
    }));
  setStatus("off", "در حال دانلود مدل…");

  const res = await runStep("model", ["--start", ...extraArgs], c);
  $panelActions.replaceChildren();
  if (!res.ok) {
    if (res.error_code === "cancelled") {
      leanResumeCard(res.data && res.data.partial
        ? { partial: res.data.partial,
            total_size_bytes: (S.modelPreflight || {}).total_size_bytes || 0 }
        : S.modelPreflight || { partial: { bytes: 0, files: {} } });
      setStatus("off", "دانلود لغو شد");
      return;
    }
    leanErrorCard(res.error_fa || "دانلود مدل ناموفق بود", res);
    return;
  }
  leanReadyCard(res.data, null);
}

function leanReadyCard(data, note) {
  clearPanel();
  const c = el("div", "wcard");
  c.appendChild(el("h3", null, "مدل زبانی آماده است"));
  c.appendChild(el("p", null, note || "اثر انگشت همهٔ فایل‌ها با pins.json تأیید شد؛ از این پس دانلودی انجام نمی‌شود."));
  $panelBody.appendChild(c);
  markStep(5, "done");
  setStatus("on", "مدل آماده است");
  renderDictionary();
}

function leanErrorCard(faMsg, res) {
  clearPanel();
  const technical = res && res.data
    ? [res.data.technical, res.data.url ? "url=" + res.data.url : "",
       res.data.http_status ? "http_status=" + res.data.http_status : ""]
        .filter(Boolean).join(" | ")
    : (res && res.error_fa) || "";
  $panelBody.appendChild(errorCard("دانلود مدل ناموفق بود", faMsg, technical));
  $panelActions.append(
    button("تلاش مجدد", "btn-primary", () => doModelDownload(["--start"])),
    button("بازگشت به گام قبل", "btn-ghost", renderDatabase));
  setStatus("err", "دانلود ناموفق");
}

/* legacy direct runner (skip escape hatch; kept for repair flows) */
async function doModelRun(skip) {
  clearPanel();
  const c = el("div", "wcard");
  $panelBody.appendChild(c);
  const res = await runStep("model", skip ? ["--skip"] : [], c);
  if (!res.ok) {
    leanErrorCard(res.error_fa || "دانلود مدل ناموفق بود", res);
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
   STEP 6 — آماده‌سازی لغت‌نامه دهخدا (v1.3.1: one step, three behaviors)
   full (bundled index):   «بازیابی ایندکس آماده» — progress by restore
                           stage, NO network section, NO download URLs;
                           any network attempt fails with the PART 2 §7
                           Persian message.
   full (bundled dumps):   «ساخت مجدد از داده‌های همراه» (fallback; explicit)
   lean:                   existing download + ingest + resume UX.
   Both: a record-count verification line on success.
   ========================================================================= */
/* PART 2 §7 — shown verbatim whenever a network attempt is detected in the
   full variant (the backend net_guard raises NetworkAttemptError with the
   same text; the wizard's canary is the error_code network_blocked) */
const NETWORK_BLOCKED_FA =
  "این نسخه نباید به اینترنت متصل شود. اگر این پیام را می‌بینید، " +
  "لطفاً بستهٔ نسخهٔ کامل را دوباره دانلود کنید.";
const RESTORE_STAGE_FA = {
  verify: "بررسی اثر انگشت ایندکس همراه…",
  extract: "بازکردن ایندکس فشرده…",
  restore: "بازیابی ایندکس آماده (حدود ۳۰ ثانیه)…",
  count: "شمارش مدخل‌ها…",
  ready: "آماده‌سازی نهایی…",
};

function variantFull() { return modelVariant() === "full"; }

async function renderDictionary() {
  S.currentStep = "dictionary";
  markStep(6, "active");
  if (variantFull()) return renderDictionaryFull();
  return renderDictionaryClassic();
}

async function renderDictionaryFull() {
  setHead("آماده‌سازی لغت‌نامه دهخدا",
    "ایندکس آمادهٔ همراه همین بسته بازیابی می‌شود؛ به اینترنت نیازی نیست و " +
    "هیچ داده‌ای دانلود نمی‌شود.");
  clearPanel();
  setStatus("off", "در حال بازیابی ایندکس آماده…");
  const c = el("div", "wcard");
  c.appendChild(el("p", null,
    "ایندکس برداری آماده داخل بسته است؛ در چند ثانیه بازیابی و تأیید می‌شود."));
  $panelBody.appendChild(c);

  const res = await runStep("ingest", [], c);
  $panelActions.replaceChildren();
  if (!res.ok) {
    clearPanel();
    if (res.error_code === "network_blocked") {
      // PART 2 §7 — explicit Persian failure for ANY network attempt
      const err = errorCard("اتصال شبکه مسدود شد", NETWORK_BLOCKED_FA,
        (res.log_tail || []).join("\n"));
      $panelBody.appendChild(err);
      $panelActions.append(
        button("تلاش مجدد", "btn-primary", renderDictionaryFull),
        button("انصراف", "btn-ghost", cancelWizard));
      setStatus("err", "این نسخه نباید به اینترنت متصل شود");
      return;
    }
    const err = el("div", "wcard error-card");
    err.appendChild(el("h3", null, "بازیابی ایندکس متوقف شد"));
    err.appendChild(el("p", null, esc(res.error_fa || "")));
    // never-silent fallback: the backend may fall back to a rebuild from
    // the bundled SQL — surface that in the retry flow
    $panelBody.appendChild(err);
    $panelActions.append(
      button("تلاش مجدد", "btn-primary", renderDictionaryFull),
      button("انصراف", "btn-ghost", cancelWizard)
    );
    setStatus("err", "بازیابی متوقف شد");
    return;
  }
  dictionaryDoneCard(res.data || {}, true);
}

async function renderDictionaryClassic() {
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
      button("تلاش مجدد (ادامه از آخرین نقطه)", "btn-primary", renderDictionaryClassic),
      button("انصراف", "btn-ghost", cancelWizard)
    );
    setStatus("err", "آماده‌سازی متوقف شد");
    return;
  }
  dictionaryDoneCard(res.data || {}, false);
}

function dictionaryDoneCard(data, restored) {
  clearPanel();
  const c = el("div", "wcard");
  c.appendChild(el("h3", null,
    restored ? "ایندکس آماده بازیابی شد." : "لغت‌نامه دهخدا آماده است."));
  const modeFa = {
    restored: "بازیابی ایندکس همراه بسته",
    rebuilt_from_bundle: "ساخت مجدد از داده‌های همراه بسته",
    downloaded: "دانلود و پردازش داده‌ها",
  }[data.mode] || "";
  const n = data.record_count || data.chunks || 0;
  const ul = el("ul", "model-meta");
  ul.innerHTML = `
    <li>روش آماده‌سازی: <b>${esc(modeFa)}</b></li>
    <li>تعداد بخش‌های برداری: <b>${faNum(n)}</b> — با pins.json تأیید شد</li>
    <li>زمان: ${faEta(Math.round((data.duration_ms || 0) / 1000))}</li>`;
  c.appendChild(ul);
  $panelBody.appendChild(c);
  S.ingestStats = data;
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
  else if (done.system === "done") {
    // resume after the system check: only the advanced Milvus path goes
    // through prerequisites; the default Qdrant path skips straight to db.
    if ((S.state.backend || "") === "milvus") renderPrereqs();
    else { S.chosenBackend = S.state.backend || null; renderDatabase(); }
  }
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
