// dhub-runner UI: hash routes over the /api endpoints.
"use strict";

const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");
let refreshTimer = null;

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.body = JSON.stringify(body);
    options.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(`/api${path}`, options);
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!response.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), isError ? 8000 : 3000);
}

// -- icons (Tabler, MIT) and entity marks ------------------------------------

const ICONS = {
  home: '<path d="M5 12l-2 0l9 -9l9 9l-2 0"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-7"/><path d="M9 21v-6a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v6"/>',
  upload: '<path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2 -2v-2"/><path d="M7 9l5 -5l5 5"/><path d="M12 4l0 12"/>',
  key: '<path d="M16.555 3.843l3.602 3.602a2.877 2.877 0 0 1 0 4.069l-2.643 2.643a2.877 2.877 0 0 1 -4.069 0l-.301 -.301l-6.558 6.558a2 2 0 0 1 -1.239 .578l-.175 .008h-1.172a1 1 0 0 1 -.993 -.883l-.007 -.117v-1.172a2 2 0 0 1 .467 -1.284l.119 -.13l.414 -.414h2v-2h2v-2l2.144 -2.144l-.301 -.301a2.877 2.877 0 0 1 0 -4.069l2.643 -2.643a2.877 2.877 0 0 1 4.069 0z"/><path d="M15 9h.01"/>',
  collection: '<path d="M12 3l8 4.5l0 9l-8 4.5l-8 -4.5l0 -9l8 -4.5"/><path d="M12 12l8 -4.5"/><path d="M12 12l0 9"/><path d="M12 12l-8 -4.5"/>',
  pipeline: '<path d="M3 19a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/><path d="M19 7a2 2 0 1 0 0 -4a2 2 0 0 0 0 4z"/><path d="M11 19h5.5a3.5 3.5 0 0 0 0 -7h-8a3.5 3.5 0 0 1 0 -7h4.5"/>',
  dataset: '<path d="M12 6m-8 0a8 3 0 1 0 16 0a8 3 0 1 0 -16 0"/><path d="M4 6v6a8 3 0 0 0 16 0v-6"/><path d="M4 12v6a8 3 0 0 0 16 0v-6"/>',
  agent: '<path d="M6 4m0 2a2 2 0 0 1 2 -2h8a2 2 0 0 1 2 2v4a2 2 0 0 1 -2 2h-8a2 2 0 0 1 -2 -2z"/><path d="M12 2v2"/><path d="M9 12v9"/><path d="M15 12v9"/><path d="M5 16l4 -2"/><path d="M15 14l4 2"/><path d="M9 18h6"/><path d="M10 8v.01"/><path d="M14 8v.01"/>',
  code: '<path d="M7 8l-4 4l4 4"/><path d="M17 8l4 4l-4 4"/><path d="M14 4l-4 16"/>',
  file: '<path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2z"/>',
  play: '<path d="M7 4v16l13 -8z"/>',
  stop: '<path d="M5 5m0 2a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v10a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2z"/>',
  refresh: '<path d="M20 11a8.1 8.1 0 0 0 -15.5 -2m-.5 -4v4h4"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4"/>',
  message: '<path d="M8 9h8"/><path d="M8 13h6"/><path d="M18 4a3 3 0 0 1 3 3v8a3 3 0 0 1 -3 3h-5l-5 3v-3h-2a3 3 0 0 1 -3 -3v-8a3 3 0 0 1 3 -3h12z"/>',
  plus: '<path d="M12 5l0 14"/><path d="M5 12l14 0"/>',
  trash: '<path d="M4 7l16 0"/><path d="M10 11l0 6"/><path d="M14 11l0 6"/><path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2 -2l1 -12"/><path d="M9 7v-3a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v3"/>',
  send: '<path d="M10 14l11 -11"/><path d="M21 3l-6.5 18a.55 .55 0 0 1 -1 0l-3.5 -7l-7 -3.5a.55 .55 0 0 1 0 -1l18 -6.5"/>',
  tool: '<path d="M7 10h3v-3l-3.5 -3.5a6 6 0 0 1 8 8l6 6a2 2 0 0 1 -3 3l-6 -6a6 6 0 0 1 -8 -8l3.5 3.5"/>',
  alert: '<path d="M12 9v4"/><path d="M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0z"/><path d="M12 16h.01"/>',
  sun: '<path d="M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"/><path d="M3 12h1m8 -9v1m8 8h1m-9 8v1m-6.4 -15.4l.7 .7m12.1 -.7l-.7 .7m0 11.4l.7 .7m-12.1 -.7l-.7 .7"/>',
  moon: '<path d="M12 3c.132 0 .263 0 .393 0a7.5 7.5 0 0 0 7.92 12.446a9 9 0 1 1 -8.313 -12.454z"/>',
  pause: '<path d="M6 5m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v12a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z"/><path d="M14 5m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v12a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z"/>',
  activity: '<path d="M3 12h4l3 8l4 -16l3 8h4"/>',
  inbox: '<path d="M4 4m0 2a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2z"/><path d="M4 13h3l3 3h4l3 -3h3"/>',
  catalog: '<path d="M5 4m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v14a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z"/><path d="M9 4m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v14a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z"/><path d="M5 8h4"/><path d="M9 16h4"/><path d="M13.803 4.56l2.184 -.53c.562 -.135 1.133 .19 1.282 .732l3.695 13.418a1.02 1.02 0 0 1 -.634 1.219l-.133 .041l-2.184 .53c-.562 .135 -1.133 -.19 -1.282 -.732l-3.695 -13.418a1.02 1.02 0 0 1 .634 -1.219l.133 -.041z"/><path d="M14 9l4 -1"/><path d="M16 16l3.923 -.98"/>',
  search: '<path d="M10 10m-7 0a7 7 0 1 0 14 0a7 7 0 1 0 -14 0"/><path d="M21 21l-6 -6"/>',
  download: '<path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2 -2v-2"/><path d="M7 11l5 5l5 -5"/><path d="M12 4l0 12"/>',
  tag: '<path d="M7.5 7.5m-1 0a1 1 0 1 0 2 0a1 1 0 1 0 -2 0"/><path d="M3 6v5.172a2 2 0 0 0 .586 1.414l7.71 7.71a2.41 2.41 0 0 0 3.408 0l5.592 -5.592a2.41 2.41 0 0 0 0 -3.408l-7.71 -7.71a2 2 0 0 0 -1.414 -.586h-5.172a3 3 0 0 0 -3 3z"/>',
  history: '<path d="M12 8l0 4l2 2"/><path d="M3.05 11a9 9 0 1 1 .5 4m-.5 5v-5h5"/>',
  lineage: '<path d="M6 5m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/><path d="M6 19m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/><path d="M18 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/><path d="M6 7v10"/><path d="M8 5h3a3 3 0 0 1 3 3v1a3 3 0 0 0 3 3"/>',
  table: '<path d="M3 5a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-14z"/><path d="M3 10h18"/><path d="M10 3v18"/>',
  eye: '<path d="M10 12a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/><path d="M21 12c-2.4 4 -5.4 6 -9 6c-3.6 0 -6.6 -2 -9 -6c2.4 -4 5.4 -6 9 -6c3.6 0 6.6 2 9 6"/>',
  arrow: '<path d="M5 12l14 0"/><path d="M13 18l6 -6"/><path d="M13 6l6 6"/>',
};
const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.file}</svg>`;

// Resource kind -> entity color and icon, as in the portal's entity-color system.
const ENTITY = {
  collection: ["collection", "collection", "컬렉션"],
  pipeline: ["pipeline", "pipeline", "파이프라인"],
  dataset: ["dataset", "dataset", "데이터셋"],
  agent: ["agent", "agent", "에이전트"],
  tool: ["agent", "tool", "툴"],
  actor: ["agent", "tool", "액터"],
  llm: ["agent", "agent", "LLM"],
  prompt: ["agent", "file", "프롬프트"],
  code: ["code", "code", "코드"],
  connector: ["code", "pipeline", "커넥터"],
  secret: ["secret", "key", "시크릿"],
};
const entity = (kind) => ENTITY[kind] || ["collection", "file", kind];
const entityIcon = (kind) => { const [color, ic] = entity(kind); return `<span class="entity-icon e-${color}">${icon(ic)}</span>`; };
const entityPill = (kind, text) => { const [color, , label] = entity(kind); return `<span class="entity-pill e-${color}">${esc(text ?? label)}</span>`; };

const STATES = {
  ready: "완료", error: "실패", running: "실행 중", queued: "대기", cancelled: "취소됨", interrupted: "중단됨",
  ok: "정상", warn: "주의", attention: "설정 필요", missing: "값 없음", settings: "컬렉션", shared: "공용",
  stopped: "중지됨", idle: "대기", starting: "시작 중", paused: "스케줄 일시정지",
};
const AGENT_HINTS = { idle: "처음 쓸 때 파드를 띄웁니다", starting: "파드가 준비되는 중입니다" };
const badge = (state) => `<span class="badge ${esc(state || "")}">${esc(STATES[state] || state || "없음")}</span>`;
const time = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const problems = (list) => (list && list.length ? `<ul class="problems">${list.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "");
const empty = (text, ic = "inbox") => `<div class="panel empty">${icon(ic)}${text}</div>`;

function pageHead({ kind, kicker, title, sub, actions = "" }) {
  return `
    <div class="page-head">
      ${kind ? entityIcon(kind) : ""}
      <div class="titles">
        ${kicker ? `<div class="kicker">${kicker}</div>` : ""}
        <h1>${esc(title)}</h1>
        ${sub ? `<p class="sub">${sub}</p>` : ""}
      </div>
      <div class="actions">${actions}</div>
    </div>`;
}

function setCrumbs(parts) {
  crumbs.innerHTML = parts
    .map(([label, href], i) => (href && i < parts.length - 1 ? `<a href="${href}">${esc(label)}</a>` : `<span class="current">${esc(label)}</span>`))
    .join('<span class="sep">/</span>');
}

function setNav(active) {
  document.querySelectorAll("[data-nav]").forEach((el) => el.classList.toggle("active", el.dataset.nav === active));
}

async function loadSidebar(active, nav) {
  setNav(active ? `collection:${active}` : nav);
  const list = document.getElementById("collection-list");
  try {
    const collections = await api("GET", "/collections");
    list.innerHTML = collections.length
      ? collections.map((b) => `<a class="nav-item" data-nav="collection:${esc(b.name)}" href="#/collections/${enc(b.name)}" title="${esc(b.name)}">${icon("collection")}<span class="label">${esc(b.alias || b.name)}</span></a>`).join("")
      : `<div class="nav-empty">아직 컬렉션이 없습니다.</div>`;
    setNav(active ? `collection:${active}` : nav);
    return collections;
  } catch (error) {
    list.innerHTML = `<div class="nav-empty">${esc(error.message)}</div>`;
    return [];
  }
}

// -- secrets -----------------------------------------------------------------

const SOURCES = { settings: "이 컬렉션에 설정됨", shared: "공용 시크릿 사용" };
const sourceStatus = (source) => (source ? `${badge(source)} <span class="hint">${SOURCES[source] || ""}</span>` : badge("missing"));

// One "<secret>/<key>=value" per line; "#" starts a comment; an empty value removes the key.
function parseBulk(text) {
  const values = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    const ref = at > 0 ? line.slice(0, at).trim() : "";
    if (!/^[^/\s]+\/[^/\s]+$/.test(ref)) throw new Error(`"${line.slice(0, 40)}": 시크릿/키=값 형식이 아닙니다`);
    let value = line.slice(at + 1).trim();
    if (/^(["']).*\1$/.test(value)) value = value.slice(1, -1);
    values[ref] = value || null;
  }
  return values;
}

// A form of secret keys: what is set where, a field per key for a new value,
// one more key, and bulk lines. Calls save({"<secret>/<key>": value | null}).
function secretForm(el, rows, save) {
  el.innerHTML = `
    <form class="panel" id="secret-form">
      ${rows.length ? `<table><tr><th>시크릿 / 키</th><th>상태</th><th style="width:40%">새 값</th></tr>${rows.map((x) => `
        <tr><td><span class="name">${entityIcon("secret")}<span class="mono">${esc(x.secret)} / ${esc(x.key)}</span></span></td><td>${sourceStatus(x.source)}</td>
          <td><input data-secret="${esc(x.secret)}/${esc(x.key)}" type="password" autocomplete="off" placeholder="${x.source ? "바꾸려면 입력 (지우려면 공백 한 칸)" : "값 입력"}"></td></tr>`).join("")}</table>`
        : `<div class="hint">시크릿이 없습니다.</div>`}
      <label for="secret-new">시크릿 추가</label>
      <div class="row"><input id="secret-new" type="text" placeholder="시크릿/키 (예: dhub2_llm/base_url)" style="flex:1"><input id="secret-new-value" type="password" placeholder="값" style="flex:1"></div>
      <label for="secret-bulk">여러 개 한 번에 입력</label>
      <textarea id="secret-bulk" rows="4" class="mono" autocomplete="off" spellcheck="false" placeholder="dhub2_llm/api_key=...&#10;crm/token=..."></textarea>
      <div class="hint">한 줄에 <code>시크릿/키=값</code> 하나씩. 값을 비우면 그 키를 지웁니다. 값은 암호화해 저장되고 다시 보여주지 않습니다.</div>
      <div class="form-actions"><button class="button primary" type="submit">저장</button></div>
    </form>`;
  el.querySelector("#secret-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const values = {};
      el.querySelectorAll("[data-secret]").forEach((input) => {
        if (input.value === " ") values[input.dataset.secret] = null;
        else if (input.value) values[input.dataset.secret] = input.value;
      });
      const extra = el.querySelector("#secret-new").value.trim();
      if (extra) values[extra] = el.querySelector("#secret-new-value").value || null;
      Object.assign(values, parseBulk(el.querySelector("#secret-bulk").value));
      if (!Object.keys(values).length) return toast("바꿀 값이 없습니다");
      await save(values);
      toast("저장했습니다");
      route();
    } catch (error) { toast(error.message, true); }
  });
}

// -- pages -------------------------------------------------------------------

const stat = (kind, value, label) => `<div class="stat">${entityIcon(kind)}<div><div class="value">${value}</div><div class="label">${label}</div></div></div>`;

async function home() {
  setCrumbs([["홈", null]]);
  const collections = await loadSidebar(null, "home");
  const sum = (field) => collections.reduce((total, b) => total + (b[field] || 0), 0);
  view.innerHTML = `
    ${pageHead({ title: "홈", sub: "D.Hub for AI에서 내보낸 컬렉션의 파이프라인과 에이전트를 실행합니다.", actions: `<a class="button primary" href="#/import">${icon("upload")}가져오기</a>` })}
    <div class="stats">
      ${stat("collection", collections.length, "컬렉션")}
      ${stat("pipeline", sum("pipelines"), "파이프라인")}
      ${stat("agent", sum("agents"), "에이전트")}
      ${stat("dataset", sum("datasets"), "데이터셋")}
    </div>
    <h2>컬렉션 <span class="count">${collections.length}</span></h2>
    ${collections.length ? `<div class="grid">${collections.map((b) => `
      <a class="card" href="#/collections/${enc(b.name)}">
        <div class="card-head">${entityIcon("collection")}<span class="title">${esc(b.alias || b.name)}</span></div>
        <span class="desc">${esc(b.error || b.description || "")}</span>
        <div class="meta">${entityPill("pipeline", `파이프라인 ${b.pipelines ?? "-"}`)}${entityPill("agent", `에이전트 ${b.agents ?? "-"}`)}${entityPill("dataset", `데이터셋 ${b.datasets ?? "-"}`)}</div>
      </a>`).join("")}</div>` : empty(`등록한 컬렉션이 없습니다. <a href="#/import">가져오기</a>에서 D.Hub for AI의 컬렉션 내보내기 ZIP을 올리세요.`, "collection")}`;
}

async function importPage(params) {
  const target = params.get("into") || "";
  setCrumbs([["홈", "#/"], ["가져오기", null]]);
  await loadSidebar(null, "import");
  view.innerHTML = `
    ${pageHead({ title: "가져오기", sub: "D.Hub for AI에서 내보낸 컬렉션 ZIP이나 개별 리소스 파일을 등록합니다." })}
    <form id="import-form" class="panel">
      <label for="files" style="margin-top:0">파일</label>
      <div class="dropzone"><input id="files" name="files" type="file" multiple required></div>
      <div class="hint" style="margin-top:8px">
        ZIP 하나: 내보낸 컬렉션 전체를 등록합니다.<br>
        리소스 파일 여러 개: D.Hub for AI 리소스 JSON(<code>category</code> 포함), 코드 본문(<code>&lt;코드 이름&gt;.py/.sql</code>), 데이터(<code>&lt;데이터셋&gt;.csv/.json/.parquet</code>)를 기존 컬렉션에 더하거나 새 컬렉션으로 등록합니다.
      </div>
      <label for="name">컬렉션 이름</label>
      <input id="name" name="name" type="text" required pattern="[A-Za-z0-9][A-Za-z0-9_.\\-]*" value="${esc(target)}" placeholder="my_collection">
      <div class="hint">영문, 숫자, _, -, . 만 쓸 수 있습니다. ZIP을 고르면 파일 이름으로 채웁니다.</div>
      <label class="row"><input id="replace" type="checkbox"> 같은 이름의 컬렉션을 ZIP으로 바꾸기</label>
      <div class="form-actions"><button class="button primary" type="submit">${icon("upload")}가져오기</button></div>
    </form>
    <div id="import-result"></div>`;
  const form = document.getElementById("import-form");
  const files = document.getElementById("files");
  const name = document.getElementById("name");
  files.addEventListener("change", () => {
    const first = files.files[0];
    if (!name.value && first && first.name.endsWith(".zip")) name.value = first.name.replace(/(-export)?\.zip$/, "").replace(/[^A-Za-z0-9_.-]/g, "_");
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData();
    data.append("name", name.value.trim());
    data.append("replace", document.getElementById("replace").checked ? "true" : "false");
    for (const file of files.files) data.append("files", file, file.name);
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const report = await api("POST", "/collections", data);
      document.getElementById("import-result").innerHTML = importReport(report);
      toast(`${report.name} 가져오기 완료`);
      loadSidebar(null, "import");
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

function importReport(report) {
  const KINDS = { "llm.model": "llm", "agent.tool": "tool", "agent.actor": "actor" };
  const rows = (map) => Object.entries(map || {}).map(([k, v]) => `
    <tr><td>${entityPill(KINDS[k] || k, k)}</td><td>${v.map(esc).join(", ")}</td></tr>`).join("");
  return `
    <h2>결과</h2>
    ${report.secrets && report.secrets.length ? `<div class="alert">${icon("alert")}<div>입력이 필요한 시크릿: ${report.secrets.map((e) => `<code>${esc(e)}</code>`).join(" ")} — <a href="#/collections/${enc(report.name)}/secrets">시크릿 입력</a></div></div>` : ""}
    <div class="table-card"><table><tr><th>가져온 리소스</th><th>이름</th></tr>${rows(report.imported)}</table></div>
    ${Object.keys(report.skipped || {}).length ? `<h2>건너뛴 리소스 <span class="count">러너가 실행하지 않음</span></h2><div class="table-card"><table><tr><th>종류</th><th>이름</th></tr>${rows(report.skipped)}</table></div>` : ""}
    ${report.data && report.data.length ? `<h2>초기 데이터</h2><p>${report.data.map((d) => entityPill("dataset", d)).join(" ")}</p>` : ""}
    <div class="form-actions"><a class="button primary" href="#/collections/${enc(report.name)}">${icon("collection")}${esc(report.name)} 열기</a></div>`;
}

async function collectionPage(name, tab) {
  setCrumbs([["홈", "#/"], [name, null]]);
  loadSidebar(name);
  const b = await api("GET", `/collections/${enc(name)}`);
  const tabs = [["overview", "개요", "collection"], ["secrets", "시크릿", "key"]];
  view.innerHTML = `
    ${pageHead({
      kind: "collection",
      kicker: b.collection.alias && b.collection.alias !== name ? entityPill("collection", name) : entityPill("collection"),
      title: b.collection.alias || name,
      sub: esc(b.collection.description || ""),
      actions: `<a class="button" href="#/catalog?collection=${enc(name)}">${icon("catalog")}카탈로그</a>
        <a class="button" href="#/import?into=${enc(name)}">${icon("plus")}리소스 추가</a>
        <button class="button danger" id="delete-collection">${icon("trash")}삭제</button>`,
    })}
    ${b.missing_secrets.length ? `<div class="alert">${icon("alert")}<div>값이 없는 시크릿: ${b.missing_secrets.map((e) => `<code>${esc(e)}</code>`).join(" ")} — <a href="#/collections/${enc(name)}/secrets">시크릿 입력</a></div></div>` : ""}
    <nav class="tabs">${tabs.map(([key, label, ic]) => `<a href="#/collections/${enc(name)}${key === "overview" ? "" : "/" + key}" class="${tab === key ? "active" : ""}">${icon(ic)}${label}</a>`).join("")}</nav>
    ${tab === "secrets" ? `<p class="sub" style="margin:-8px 0 16px">D.Hub for AI의 내보내기에는 시크릿 값이 없으므로 여기서 입력합니다. 여러 컬렉션이 함께 쓰는 값은 <a href="#/secrets">공용 시크릿</a>에 넣으면 되고, 이 컬렉션에 넣은 값이 먼저 쓰입니다.</p><div id="settings"></div>` : overview(name, b)}`;
  document.getElementById("delete-collection").addEventListener("click", async () => {
    try {
      await api("DELETE", `/collections/${enc(name)}`);
      toast(`${name} 삭제됨`);
      location.hash = "#/";
    } catch (error) { toast(error.message, true); }
  });
  if (tab === "secrets") {
    const rows = await api("GET", `/collections/${enc(name)}/secrets`);
    secretForm(document.getElementById("settings"), rows, (values) => api("PUT", `/collections/${enc(name)}/secrets`, { values }));
  }
  bindControls(view);
  if (b.pipelines.some((p) => p.active_run)) refreshTimer = setTimeout(route, 2000);
}

function overview(name, b) {
  const pipelines = b.pipelines.length ? `<div class="table-card"><table>
      <tr><th>파이프라인</th><th>스케줄</th><th>최근 실행</th><th></th></tr>
      ${b.pipelines.map((p) => `<tr>
        <td><a class="name" href="#/collections/${enc(name)}/pipelines/${enc(p.name)}">${entityIcon("pipeline")}${esc(p.name)}</a><div class="hint">${esc(p.description || "")}</div>${problems(p.problems)}</td>
        <td>${scheduleCell(p)}</td>
        <td>${runCell(p)}</td>
        <td><div class="row" style="justify-content:flex-end">${pipelineControls(name, p)}</div></td>
      </tr>`).join("")}</table></div>` : empty("파이프라인이 없습니다.", "pipeline");
  const agents = b.agents.length ? `<div class="grid">${b.agents.map((a) => `
      <div class="card">
        <div class="card-head">${entityIcon("agent")}<span class="title">${esc(a.name)}</span><span class="spacer"></span>${badge(a.problems.length ? "attention" : a.status)}</div>
        <span class="desc">${esc(a.description || "")}</span>
        <div class="meta">${entityPill("llm", a.model || "-")}${a.tools.map((t) => entityPill("tool", t)).join("")}${a.actors.map((t) => entityPill("actor", t)).join("")}</div>
        ${problems(a.problems)}
        <div class="row"><a class="button small primary" href="#/collections/${enc(name)}/agents/${enc(a.name)}">${icon("message")}대화</a>${agentControls(name, a)}<a class="button small" href="#/collections/${enc(name)}/resources/agent/${enc(a.name)}">정의</a></div>
      </div>`).join("")}</div>` : empty("에이전트가 없습니다.", "agent");
  const datasets = b.datasets.length ? `<div class="table-card"><table>
      <tr><th>데이터셋</th><th>행</th><th>버전</th></tr>
      ${b.datasets.map((d) => `<tr><td><a class="name" href="#/collections/${enc(name)}/datasets/${enc(d.name)}">${entityIcon("dataset")}${esc(d.name)}</a><div class="hint">${esc(d.description || "")}</div></td>
        <td>${d.rows ?? '<span class="hint">아직 없음</span>'}</td><td>${d.version ?? ""}</td></tr>`).join("")}</table></div>` : empty("데이터셋이 없습니다.", "dataset");
  const others = Object.entries(b.resources).filter(([, names]) => names.length);
  return `
    <h2>${icon("pipeline")}파이프라인 <span class="count">${b.pipelines.length}</span></h2>${pipelines}
    <h2>${icon("agent")}에이전트 <span class="count">${b.agents.length}</span></h2>${agents}
    <h2>${icon("dataset")}데이터셋 <span class="count">${b.datasets.length}</span></h2>${datasets}
    ${others.length ? `<h2>${icon("file")}그 밖의 리소스</h2><div class="table-card"><table>${others.map(([kind, names]) => `<tr><th style="width:140px">${entityPill(kind)}</th><td>${names.map((n) => `<a href="#/collections/${enc(name)}/resources/${enc(kind)}/${enc(n)}">${esc(n)}</a>`).join(" · ")}</td></tr>`).join("")}</table></div>` : ""}`;
}

async function secretsPage() {
  setCrumbs([["홈", "#/"], ["시크릿", null]]);
  await loadSidebar(null, "secrets");
  const data = await api("GET", "/secrets");
  view.innerHTML = `
    ${pageHead({ kind: "secret", title: "시크릿", sub: "D.Hub for AI의 컬렉션 내보내기에는 시크릿 값이 없습니다. 모든 컬렉션이 함께 쓰는 <strong>공용 시크릿</strong>을 여기서 입력하고, 컬렉션마다 빠진 값을 확인합니다. 컬렉션에 넣은 값이 공용 값보다 먼저 쓰입니다." })}
    <h2 style="margin-top:0">공용 시크릿 <span class="count">${data.shared.length}</span></h2>
    <div id="shared"></div>
    <h2>컬렉션별 상태</h2>
    ${data.collections.length ? `<div class="table-card"><table><tr><th>컬렉션</th><th>시크릿</th><th></th></tr>${data.collections.map((c) => `
      <tr><td><a class="name" href="#/collections/${enc(c.name)}">${entityIcon("collection")}${esc(c.alias || c.name)}</a></td>
        <td>${c.error ? `<span class="problems">${esc(c.error)}</span>` : (c.secrets || []).map((x) => `<div class="row" style="margin:2px 0"><span class="mono">${esc(x.secret)}/${esc(x.key)}</span>${sourceStatus(x.source)}</div>`).join("") || '<span class="hint">없음</span>'}</td>
        <td style="text-align:right"><a class="button small" href="#/collections/${enc(c.name)}/secrets">${icon("key")}입력</a></td></tr>`).join("")}</table></div>` : empty("등록한 컬렉션이 없습니다.", "collection")}`;
  secretForm(document.getElementById("shared"), data.shared.map((x) => ({ ...x, source: "shared" })), (values) => api("PUT", "/secrets", { values }));
}

// -- controls: run, cancel, pause and resume pipelines; start and stop agents --

const ACTIONS = {
  run: (d) => api("POST", `/collections/${enc(d.b)}/pipelines/${enc(d.p)}/runs`).then((r) => `${d.p} 실행 시작 (${r.run_id})`),
  cancel: (d) => api("POST", `/collections/${enc(d.b)}/runs/${enc(d.run)}/cancel`).then(() => "실행을 멈춥니다 (진행 중인 스텝은 끝까지 돕니다)"),
  pause: (d) => api("POST", `/collections/${enc(d.b)}/pipelines/${enc(d.p)}/pause`).then(() => `${d.p} 스케줄을 멈췄습니다`),
  resume: (d) => api("POST", `/collections/${enc(d.b)}/pipelines/${enc(d.p)}/resume`).then(() => `${d.p} 스케줄을 다시 켰습니다`),
  "agent-start": (d) => api("POST", `/collections/${enc(d.b)}/agents/${enc(d.a)}/start`).then(() => `${d.a} 시작됨`),
  "agent-stop": (d) => api("POST", `/collections/${enc(d.b)}/agents/${enc(d.a)}/stop`).then(() => `${d.a} 중지됨`),
};

function bindControls(root) {
  root.querySelectorAll("[data-act]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try { toast(await ACTIONS[button.dataset.act](button.dataset)); }
    catch (error) { toast(error.message, true); }
    route();
  }));
}

function pipelineControls(collection, p, size = "small") {
  const attrs = `data-b="${esc(collection)}" data-p="${esc(p.name)}"`;
  const cls = `button ${size}`;
  const schedule = !p.schedule ? ""
    : p.paused ? `<button class="${cls}" data-act="resume" ${attrs} title="예약 실행을 다시 켭니다">${icon("play")}스케줄 재개</button>`
    : `<button class="${cls}" data-act="pause" ${attrs} title="예약 실행을 멈춥니다. 직접 실행은 됩니다">${icon("pause")}스케줄 정지</button>`;
  const run = p.active_run
    ? `<button class="${cls} danger" data-act="cancel" data-b="${esc(collection)}" data-run="${esc(p.active_run)}">${icon("stop")}중지</button>`
    : `<button class="${cls}${size ? "" : " primary"}" data-act="run" ${attrs} ${p.problems.length ? "disabled" : ""}>${icon("play")}실행</button>`;
  return `${schedule}${run}`;
}

function agentControls(collection, a, size = "small") {
  const attrs = `data-b="${esc(collection)}" data-a="${esc(a.name)}"`;
  return a.status === "stopped"
    ? `<button class="button ${size}" data-act="agent-start" ${attrs}>${icon("play")}시작</button>`
    : `<button class="button ${size} danger" data-act="agent-stop" ${attrs} title="대화를 받지 않습니다">${icon("stop")}중지</button>`;
}

const agentStatus = (a) => `${badge(a.status)}${AGENT_HINTS[a.status] ? `<div class="hint">${AGENT_HINTS[a.status]}</div>` : ""}`;
const scheduleCell = (p) => (p.schedule ? `<span class="mono">${esc(p.schedule)}</span>${p.paused ? ` ${badge("paused")}` : ""}` : '<span class="hint">-</span>');
const runCell = (p) => `${p.active_run ? badge("running") : p.last_run ? badge(p.last_run.state) : '<span class="hint">-</span>'}<div class="hint">${time(p.last_run && p.last_run.start_time)}</div>`;

async function opsPage() {
  setCrumbs([["홈", "#/"], ["운영", null]]);
  await loadSidebar(null, "ops");
  const data = await api("GET", "/operations");
  const ok = data.filter((c) => !c.error);
  const pipelines = ok.flatMap((c) => c.pipelines);
  const agents = ok.flatMap((c) => c.agents);
  const count = (list, test) => list.filter(test).length;
  view.innerHTML = `
    ${pageHead({ title: "운영", sub: "모든 컬렉션의 파이프라인과 에이전트를 실행하고 멈춥니다. 상태는 자동으로 새로 고칩니다." })}
    <div class="stats">
      ${stat("pipeline", count(pipelines, (p) => p.active_run), "실행 중인 파이프라인")}
      ${stat("pipeline", count(pipelines, (p) => p.schedule && !p.paused), "켜진 스케줄")}
      ${stat("pipeline", count(pipelines, (p) => p.paused), "멈춘 스케줄")}
      ${stat("agent", count(agents, (a) => a.status !== "stopped"), "켜진 에이전트")}
      ${stat("agent", count(agents, (a) => a.status === "stopped"), "중지된 에이전트")}
    </div>
    ${data.length ? data.map((c) => `
      <h2><a class="name" href="#/collections/${enc(c.name)}">${entityIcon("collection")}${esc(c.alias || c.name)}</a></h2>
      ${c.error ? `<div class="alert danger">${icon("alert")}<div>${esc(c.error)}</div></div>` : `
      ${c.pipelines.length ? `<div class="table-card"><table>
        <tr><th>파이프라인</th><th>스케줄</th><th>최근 실행</th><th></th></tr>
        ${c.pipelines.map((p) => `<tr>
          <td><a class="name" href="#/collections/${enc(c.name)}/pipelines/${enc(p.name)}">${entityIcon("pipeline")}${esc(p.name)}</a>${problems(p.problems)}</td>
          <td>${scheduleCell(p)}</td><td>${runCell(p)}</td>
          <td><div class="row" style="justify-content:flex-end">${pipelineControls(c.name, p)}</div></td></tr>`).join("")}</table></div>` : ""}
      ${c.agents.length ? `<div class="table-card" style="margin-top:12px"><table>
        <tr><th>에이전트</th><th>상태</th><th>세션</th><th></th></tr>
        ${c.agents.map((a) => `<tr>
          <td><a class="name" href="#/collections/${enc(c.name)}/agents/${enc(a.name)}">${entityIcon("agent")}${esc(a.name)}</a><div class="hint">${esc(a.description || "")}</div></td>
          <td>${agentStatus(a)}</td><td>${a.sessions}</td>
          <td><div class="row" style="justify-content:flex-end">${agentControls(c.name, a)}<a class="button small" href="#/collections/${enc(c.name)}/agents/${enc(a.name)}">${icon("message")}대화</a></div></td></tr>`).join("")}</table></div>` : ""}
      ${!c.pipelines.length && !c.agents.length ? empty("파이프라인과 에이전트가 없습니다.") : ""}`}`).join("")
      : empty(`등록한 컬렉션이 없습니다. <a href="#/import">가져오기</a>에서 컬렉션을 올리세요.`, "collection")}`;
  bindControls(view);
  const busy = pipelines.some((p) => p.active_run) || agents.some((a) => a.status === "starting");
  refreshTimer = setTimeout(route, busy ? 2000 : 10000);
}

async function pipelinePage(name, pipeline) {
  setCrumbs([["홈", "#/"], [name, `#/collections/${enc(name)}`], [pipeline, null]]);
  loadSidebar(name);
  const p = await api("GET", `/collections/${enc(name)}/pipelines/${enc(pipeline)}`);
  const port = (ports) => Object.entries(ports).map(([key, d]) => {
    const target = d.kind === "dataset" ? `<a href="#/collections/${enc(name)}/datasets/${enc(d.ref)}">${esc(d.ref)}</a>` : `<span class="mono">${esc(d.ref)}</span>`;
    return `<div class="row" style="gap:6px;margin:2px 0"><span class="mono hint">${esc(key)}</span>${d.kind === "dataset" ? entityPill("dataset", "dataset") : `<span class="entity-pill e-collection">${esc(d.kind)}</span>`}${target}${d.mode ? `<span class="hint">${esc(d.mode)}</span>` : ""}</div>`;
  }).join("");
  const stepKind = (s) => (s.kind === "script" ? `${entityPill("code", "코드")} <a class="mono" href="#/collections/${enc(name)}/resources/code/${enc(s.detail)}">${esc(s.detail)}</a>`
    : s.kind === "agent" ? `${entityPill("agent", "agent")} <span class="mono">${esc(s.detail)}</span>` : `${entityPill("pipeline", "transform")} <span class="mono">${esc(s.detail)}</span>`);
  view.innerHTML = `
    ${pageHead({
      kind: "pipeline",
      kicker: `${entityPill("pipeline")}${p.active_run ? badge("running") : p.runs[0] ? badge(p.runs[0].state) : ""}${p.paused ? badge("paused") : ""}`,
      title: p.name,
      sub: `${esc(p.description || "")}${p.schedule ? ` · 스케줄 <code>${esc(p.schedule)}</code>${p.paused ? " (일시정지)" : ""}` : ""}`,
      actions: `<button class="button" id="reset" ${p.active_run ? "disabled" : ""}>${icon("refresh")}체크포인트 초기화</button>
        ${pipelineControls(name, p, "")}`,
    })}
    ${p.problems.length ? `<div class="alert danger">${icon("alert")}<div><strong>실행할 수 없습니다</strong>${problems(p.problems)}</div></div>` : ""}
    <h2>스텝 <span class="count">${p.steps.length}</span></h2>
    ${p.steps.length ? `<div class="table-card"><table><tr><th>스텝</th><th>입력</th><th>출력</th><th>상태</th></tr>${p.steps.map((s, i) => `
      <tr><td><div style="font-weight:600">${i + 1}. ${esc(s.name)}</div><div class="row" style="gap:6px;margin-top:4px">${stepKind(s)}</div></td>
        <td>${port(s.inputs) || '<span class="hint">-</span>'}</td><td>${port(s.outputs) || '<span class="hint">-</span>'}</td>
        <td>${s.checkpoint ? badge(s.checkpoint.state) : '<span class="hint">-</span>'}<div class="hint">${time(s.checkpoint && s.checkpoint.end_time)}</div>
          ${s.checkpoint && s.checkpoint.description ? `<details><summary class="hint">오류 보기</summary><pre>${esc(s.checkpoint.stacktrace || s.checkpoint.description)}</pre></details>` : ""}</td></tr>`).join("")}</table></div>` : ""}
    <h2>실행 이력 <span class="count">${p.runs.length}</span></h2>
    ${p.runs.length ? `<div class="table-card"><table><tr><th>실행</th><th>상태</th><th>시작</th><th>종료</th><th>오류</th></tr>${p.runs.map((r) => `
      <tr><td class="mono">${esc(r.id)}</td><td>${badge(r.state)}</td><td>${time(r.start_time)}</td><td>${time(r.end_time)}</td><td>${esc(r.error || "")}</td></tr>`).join("")}</table></div>` : empty("아직 실행하지 않았습니다.", "play")}
    <h2>정의</h2><details><summary>JSON 보기</summary><pre>${esc(JSON.stringify(p.definition, null, 2))}</pre></details>`;
  bindControls(view);
  document.getElementById("reset").addEventListener("click", async () => {
    try { await api("POST", `/collections/${enc(name)}/pipelines/${enc(pipeline)}/reset`); toast("체크포인트를 초기화했습니다"); route(); }
    catch (error) { toast(error.message, true); }
  });
  if (p.active_run) refreshTimer = setTimeout(route, 1500);
}

async function resourcePage(name, kind, ref) {
  setCrumbs([["홈", "#/"], [name, `#/collections/${enc(name)}`], [ref, null]]);
  loadSidebar(name);
  const doc = await api("GET", `/collections/${enc(name)}/resources/${enc(kind)}/${enc(ref)}`);
  const source = doc.content || doc.script || (doc.function && doc.function.source);
  view.innerHTML = `
    ${pageHead({ kind, kicker: entityPill(kind), title: ref, sub: esc(doc.description || "") })}
    ${source ? `<h2 style="margin-top:0">소스</h2><pre>${esc(source)}</pre>` : ""}
    <h2>정의</h2><pre>${esc(JSON.stringify(doc, null, 2))}</pre>`;
}

const chats = {};

async function agentPage(name, agent) {
  setCrumbs([["홈", "#/"], [name, `#/collections/${enc(name)}`], [agent, null]]);
  loadSidebar(name);
  const key = `${name}/${agent}`;
  const chat = (chats[key] = chats[key] || { sessionId: null, items: [], pending: [] });
  const info = await api("GET", `/collections/${enc(name)}/agents/${enc(agent)}`);
  const stopped = info.status === "stopped";
  view.innerHTML = `
    ${pageHead({
      kind: "agent",
      kicker: `${entityPill("agent")}${badge(info.status)}`,
      title: agent,
      sub: `${esc(info.description || "")}${info.description ? " · " : ""}세션 <span class="mono">${esc(chat.sessionId || "새 세션")}</span>`,
      actions: `<a class="button" href="#/collections/${enc(name)}/resources/agent/${enc(agent)}">정의</a>
        ${agentControls(name, info, "")}
        <a class="button" href="/chat?collection=${enc(name)}&agent=${enc(agent)}" target="_blank" rel="noopener">${icon("message")}챗봇 열기</a>
        <button class="button" id="new-chat">${icon("plus")}새 대화</button>`,
    })}
    ${problems(info.problems)}
    ${stopped ? `<div class="alert">${icon("alert")}<div>중지된 에이전트입니다. <strong>시작</strong>을 누르면 다시 대화할 수 있습니다.</div></div>` : ""}
    <div class="chat-layout">
      <aside class="sessions panel">
        <div class="sessions-head">세션 <span class="count">${info.sessions.length}</span></div>
        ${info.sessions.length ? info.sessions.map((x) => `
          <div class="session ${x.session_id === chat.sessionId ? "active" : ""}">
            <button class="session-open" data-session="${esc(x.session_id)}" title="${esc(x.session_id)}">
              <span class="session-title">${esc(x.title || x.session_id)}</span>
              <span class="hint">${time(x.updated_at)} · 메시지 ${x.messages}${x.pending ? " · 승인 대기" : ""}</span>
            </button>
            <button class="icon-button" data-delete-session="${esc(x.session_id)}" title="세션 삭제" aria-label="세션 삭제">${icon("trash")}</button>
          </div>`).join("") : '<div class="hint">아직 대화가 없습니다.</div>'}
      </aside>
      <div class="chat">
        <div class="messages" id="messages"></div>
        <form id="chat-form" class="chat-input"><input id="chat-input" type="text" placeholder="${stopped ? "중지된 에이전트입니다" : "메시지를 입력하세요"}" style="flex:1" autocomplete="off" ${stopped ? "disabled" : ""}><button class="button primary" type="submit" ${stopped ? "disabled" : ""}>${icon("send")}보내기</button></form>
      </div>
    </div>`;
  bindControls(view);
  view.querySelectorAll("[data-session]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const session = await api("GET", `/collections/${enc(name)}/agents/${enc(agent)}/sessions/${enc(button.dataset.session)}`);
      chats[key] = { sessionId: session.session_id, items: sessionItems(session.messages), pending: session.pending || [] };
      route();
    } catch (error) { toast(error.message, true); }
  }));
  view.querySelectorAll("[data-delete-session]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api("DELETE", `/collections/${enc(name)}/agents/${enc(agent)}/sessions/${enc(button.dataset.deleteSession)}`);
      if (chat.sessionId === button.dataset.deleteSession) delete chats[key];
      toast("세션을 삭제했습니다");
      route();
    } catch (error) { toast(error.message, true); }
  }));
  const messages = document.getElementById("messages");
  let waiting = false;
  const render = () => {
    messages.innerHTML = chat.items.map((m) => {
      if (m.role === "event") return `<div class="msg event">${icon(m.icon || "tool")}${esc(m.text)}</div>`;
      return `<div class="msg ${m.role}">${esc(m.text)}</div>`;
    }).join("") + chat.pending.map((call) => `
      <div class="approval">
        <div class="row">${icon("alert")}<strong>${esc(call.name)}</strong><span class="hint">실행 승인이 필요합니다</span></div>
        <pre>${esc(JSON.stringify(call.args, null, 2))}</pre>
        <div class="row" style="margin-top:10px"><button class="button small primary" data-approve="${esc(call.id)}">승인</button><button class="button small" data-deny="${esc(call.id)}">거부</button></div>
      </div>`).join("") + (waiting ? `<div class="typing">응답을 기다리는 중…</div>` : "")
      || `<div class="empty">${icon("message")}메시지를 보내 대화를 시작하세요.</div>`;
    messages.querySelectorAll("[data-approve],[data-deny]").forEach((button) =>
      button.addEventListener("click", () => decide(button.dataset.approve || button.dataset.deny, Boolean(button.dataset.approve))));
    messages.scrollTop = messages.scrollHeight;
  };
  const send = async (outgoing) => {
    const button = view.querySelector("#chat-form button");
    button.disabled = true;
    waiting = true;
    render();
    try {
      const result = await api("POST", `/collections/${enc(name)}/agents/${enc(agent)}/chat`, { session_id: chat.sessionId, messages: outgoing });
      chat.sessionId = result.session_id;
      for (const event of result.events) {
        if (event.type === "tool") chat.items.push({ role: "event", text: `${event.name}(${JSON.stringify(event.args)})` });
      }
      if (result.content) chat.items.push({ role: "assistant", text: result.content });
      chat.pending = result.pending || [];
    } catch (error) {
      toast(error.message, true);
    } finally {
      waiting = false;
      button.disabled = false;
      render();
    }
  };
  const decisions = {};
  const decide = async (id, approved) => {
    decisions[id] = approved;
    chat.items.push({ role: "event", icon: approved ? "play" : "stop", text: `${approved ? "승인" : "거부"}: ${chat.pending.find((c) => c.id === id).name}` });
    if (chat.pending.every((c) => c.id in decisions)) {
      const outgoing = chat.pending.map((c) => ({ role: "tool", tool_call_id: c.id, content: JSON.stringify({ approved: decisions[c.id] }) }));
      chat.pending = [];
      render();
      await send(outgoing);
    } else render();
  };
  document.getElementById("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("chat-input");
    const text = input.value.trim();
    if (!text || chat.pending.length || waiting) return;
    input.value = "";
    chat.items.push({ role: "user", text });
    send([{ role: "user", content: text }]);
  });
  document.getElementById("new-chat").addEventListener("click", () => { delete chats[key]; route(); });
  render();
}

// A stored session's messages as the chat shows them.
function sessionItems(messages) {
  const items = [];
  for (const m of messages || []) {
    if (m.role === "human") items.push({ role: "user", text: m.content });
    else if (m.role === "ai") {
      for (const call of m.tool_calls || []) items.push({ role: "event", text: `${call.name}(${JSON.stringify(call.args)})` });
      const text = typeof m.content === "string" ? m.content : (m.content || []).map((c) => c.text || "").join("");
      if (text) items.push({ role: "assistant", text });
    }
  }
  return items;
}

// -- catalog and datasets ----------------------------------------------------
// The catalog searches every collection's Delta datasets (kept in Postgres);
// a dataset's page shows its table, schema, rows at any version, history,
// lineage and SQL, and what people noted about it.

const number = (value) => (value === null || value === undefined ? "-" : Number(value).toLocaleString());
function bytes(value) {
  if (value === null || value === undefined) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(value), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${units[i]}`;
}
function ago(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "방금";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간 전`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}일 전`;
  return new Date(iso).toLocaleDateString();
}
const tagPill = (tag, href) => `<a class="tag" href="${href || `#/catalog?tag=${enc(tag)}`}">${icon("tag")}${esc(tag)}</a>`;
const cell = (v) => `<td class="mono">${v === null ? '<span class="hint">null</span>' : esc(typeof v === "object" ? JSON.stringify(v) : v)}</td>`;
const rowsTable = (columns, rows) => `<div class="table-card"><table><tr>${columns.map((c) => `<th>${esc(c.name)}${c.type ? `<div class="hint" style="font-weight:400">${esc(c.type)}</div>` : ""}</th>`).join("")}</tr>
  ${rows.map((row) => `<tr>${(Array.isArray(row) ? row : columns.map((c) => row[c.name])).map(cell).join("")}</tr>`).join("")}</table></div>`;
const datasetHref = (c, d, tab = "overview", query = "") => `#/collections/${enc(c)}/datasets/${enc(d)}${tab === "overview" ? "" : `/${tab}`}${query ? `?${query}` : ""}`;
const NEEDS_POSTGRES = "카탈로그 검색, 문서·태그, 저장한 쿼리는 Postgres에 저장합니다. <code>RUNNER_DATABASE_URL</code>을 설정하세요 (docker compose는 기본으로 설정되어 있습니다).";

const SORT_LABELS = { relevance: "관련도", name: "이름", modified: "최근 변경", rows: "행 수", size: "크기", version: "버전", collection: "컬렉션" };
const PAGE = 30;

async function catalogPage(params) {
  setCrumbs([["홈", "#/"], ["카탈로그", null]]);
  await loadSidebar(null, "catalog");
  const q = params.get("q") || "";
  const page = Math.max(0, Number(params.get("page") || 0));
  const sort = params.get("sort") || (q ? "relevance" : "name");
  const query = new URLSearchParams();
  for (const key of ["q", "collection", "owner", "has_data"]) if (params.get(key)) query.set(key, params.get(key));
  params.getAll("tag").forEach((t) => query.append("tag", t));
  query.set("sort", sort);
  query.set("limit", PAGE);
  query.set("offset", page * PAGE);
  let data;
  try { data = await api("GET", `/catalog?${query}`); }
  catch (error) {
    if (!/RUNNER_DATABASE_URL/.test(error.message)) throw error;
    view.innerHTML = `${pageHead({ kind: "dataset", title: "카탈로그" })}<div class="alert">${icon("alert")}<div>${NEEDS_POSTGRES}<br>데이터셋은 컬렉션 화면에서 볼 수 있습니다.</div></div>`;
    return;
  }
  // A link to this search with one parameter changed (null removes it).
  const link = (changes) => {
    const next = new URLSearchParams(params);
    next.delete("page");  // a new search starts on its first page
    for (const [key, value] of Object.entries(changes)) {
      if (key === "tag+") { if (!next.getAll("tag").includes(value)) next.append("tag", value); continue; }
      if (key === "tag-") { const rest = next.getAll("tag").filter((t) => t !== value); next.delete("tag"); rest.forEach((t) => next.append("tag", t)); continue; }
      if (value === null || value === "") next.delete(key); else next.set(key, String(value));
    }
    return `#/catalog${next.toString() ? `?${next}` : ""}`;
  };
  const tags = params.getAll("tag");
  const active = [
    ...(params.get("collection") ? [[`컬렉션: ${params.get("collection")}`, link({ collection: null })]] : []),
    ...tags.map((t) => [`태그: ${t}`, link({ "tag-": t })]),
    ...(params.get("owner") ? [[`소유자: ${params.get("owner")}`, link({ owner: null })]] : []),
    ...(params.get("has_data") ? [[params.get("has_data") === "true" ? "데이터 있음" : "데이터 없음", link({ has_data: null })]] : []),
  ];
  const facet = (title, items, render) => (items.length ? `<div class="facet"><div class="facet-title">${title}</div>${items.map(render).join("")}</div>` : "");
  const facetItem = (label, count, href, on) => `<a class="facet-item${on ? " active" : ""}" href="${href}"><span class="label">${label}</span><span class="count">${number(count)}</span></a>`;
  const f = data.facets;
  const totals = data.totals;
  view.innerHTML = `
    ${pageHead({
      kind: "dataset",
      title: "카탈로그",
      sub: `모든 컬렉션의 Delta 데이터셋을 찾고 스키마, 통계, 이력, 계보를 봅니다.${data.synced_at ? ` 마지막 동기화 ${esc(ago(data.synced_at))}.` : ""}`,
      actions: `<button class="button" id="catalog-sync" title="모든 테이블의 Delta 로그를 다시 읽습니다">${icon("refresh")}동기화</button>`,
    })}
    <form id="catalog-search" class="search-bar">
      ${icon("search")}
      <input id="catalog-q" type="search" value="${esc(q)}" placeholder="이름, 별칭, 설명, 컬럼, 태그, 문서로 찾기" autocomplete="off" spellcheck="false">
      <select id="catalog-sort" aria-label="정렬">${Object.entries(SORT_LABELS).map(([key, label]) => `<option value="${key}" ${key === sort ? "selected" : ""}>${label}</option>`).join("")}</select>
      <button class="button primary" type="submit">검색</button>
    </form>
    <div class="hint search-help">필드 검색: <code>tag:매출</code> <code>col:customer_id</code> <code>type:datetime</code> <code>owner:kim</code> <code>collection:demo</code> · 공백이 있으면 <code>col:"order id"</code> · 여러 단어는 모두 맞아야 합니다.</div>
    <div class="stats" style="margin:16px 0 20px">
      ${stat("dataset", number(totals.datasets), "데이터셋")}
      ${stat("dataset", number(totals.with_data), "데이터 있음")}
      ${stat("dataset", number(totals.rows), "전체 행")}
      ${stat("dataset", bytes(totals.size_bytes), "전체 크기")}
      ${stat("collection", number(totals.collections), "컬렉션")}
    </div>
    <div class="catalog-layout">
      <aside class="facets panel">
        ${facet("컬렉션", f.collections, (x) => facetItem(esc(x.label || x.value), x.count, link({ collection: params.get("collection") === x.value ? null : x.value }), params.get("collection") === x.value))}
        ${facet("데이터", f.has_data, (x) => facetItem(x.value ? "있음" : "아직 없음", x.count, link({ has_data: params.get("has_data") === String(x.value) ? null : String(x.value) }), params.get("has_data") === String(x.value)))}
        ${facet("태그", f.tags, (x) => facetItem(`${icon("tag")}${esc(x.value)}`, x.count, tags.includes(x.value) ? link({ "tag-": x.value }) : link({ "tag+": x.value }), tags.includes(x.value)))}
        ${facet("소유자", f.owners, (x) => facetItem(esc(x.value), x.count, link({ owner: params.get("owner") === x.value ? null : x.value }), params.get("owner") === x.value))}
        ${!f.collections.length ? '<div class="hint">찾은 데이터셋이 없습니다.</div>' : ""}
      </aside>
      <div class="results">
        ${active.length ? `<div class="row active-filters">${active.map(([label, href]) => `<a class="chip" href="${href}">${esc(label)} ✕</a>`).join("")}<a class="hint" href="${link({ collection: null, owner: null, has_data: null, tag: null })}">필터 지우기</a></div>` : ""}
        <div class="hint" style="margin-bottom:8px">${number(data.total)}개${q ? ` · "${esc(q)}"` : ""}</div>
        ${data.items.length ? data.items.map((d) => `
          <a class="result" href="${datasetHref(d.collection, d.name)}">
            <div class="result-head">${entityIcon("dataset")}<div class="result-title"><span class="title">${esc(d.alias || d.name)}</span>${d.alias ? `<span class="mono hint">${esc(d.name)}</span>` : ""}</div>
              <span class="spacer"></span>${d.error ? badge("error") : d.has_data ? "" : '<span class="badge">데이터 없음</span>'}</div>
            ${d.description ? `<div class="desc">${esc(d.description)}</div>` : ""}
            <div class="meta">
              ${entityPill("collection", d.collection_alias || d.collection)}
              ${d.tags.map((t) => `<span class="tag">${icon("tag")}${esc(t)}</span>`).join("")}
              ${d.matched_columns.length ? `<span class="hint">컬럼: ${d.matched_columns.map((c) => `<span class="mono">${esc(c)}</span>`).join(", ")}</span>` : ""}
              <span class="spacer"></span>
              <span class="facts">${d.has_data ? `${number(d.rows)}행 · ${bytes(d.size_bytes)} · v${d.version}${d.modified_at ? ` · ${esc(ago(d.modified_at))}` : ""}` : ""}${d.owner ? ` · ${esc(d.owner)}` : ""}</span>
            </div>
          </a>`).join("") : empty(q || active.length ? "조건에 맞는 데이터셋이 없습니다." : `카탈로그가 비어 있습니다. <a href="#/import">가져오기</a>로 컬렉션을 등록하거나 <strong>동기화</strong>를 누르세요.`, "search")}
        ${data.total > PAGE ? `<div class="row pager">
          ${page > 0 ? `<a class="button small" href="${link({ page: page - 1 || null })}">이전</a>` : ""}
          <span class="hint">${page + 1} / ${Math.ceil(data.total / PAGE)}</span>
          ${(page + 1) * PAGE < data.total ? `<a class="button small" href="${link({ page: page + 1 })}">다음</a>` : ""}</div>` : ""}
      </div>
    </div>`;
  document.getElementById("catalog-search").addEventListener("submit", (event) => {
    event.preventDefault();
    location.hash = link({ q: document.getElementById("catalog-q").value.trim(), sort: null });
  });
  document.getElementById("catalog-sort").addEventListener("change", (event) => { location.hash = link({ sort: event.target.value }); });
  document.getElementById("catalog-sync").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      const { results } = await api("POST", "/catalog/sync", {});
      const changed = results.reduce((n, r) => n + (r.synced || []).length, 0);
      const failed = results.filter((r) => r.error);
      toast(failed.length ? `동기화 실패: ${failed.map((r) => `${r.collection}: ${r.error}`).join("\n")}` : `동기화했습니다 (바뀐 데이터셋 ${changed}개)`, failed.length > 0);
      route();
    } catch (error) { toast(error.message, true); event.currentTarget.disabled = false; }
  });
}

const DATASET_TABS = [["overview", "개요", "dataset"], ["schema", "스키마", "table"], ["rows", "데이터", "eye"], ["history", "이력", "history"], ["lineage", "계보", "lineage"], ["query", "SQL", "code"]];

async function datasetPage(collection, dataset, tab, params) {
  setCrumbs([["홈", "#/"], [collection, `#/collections/${enc(collection)}`], [dataset, null]]);
  loadSidebar(collection);
  const d = await api("GET", `/collections/${enc(collection)}/datasets/${enc(dataset)}`);
  view.innerHTML = `
    ${pageHead({
      kind: "dataset",
      kicker: `${entityPill("dataset")}${d.has_data ? "" : '<span class="badge">데이터 없음</span>'}${d.tags.map((t) => d.catalog ? tagPill(t) : `<span class="tag">${icon("tag")}${esc(t)}</span>`).join("")}`,
      title: d.alias || d.name,
      sub: `${d.alias ? `<span class="mono">${esc(d.name)}</span>${d.description ? " · " : ""}` : ""}${esc(d.description || "")}${d.owner ? ` · 소유자 ${esc(d.owner)}` : ""}`,
      actions: `${d.defined ? `<a class="button" href="#/collections/${enc(collection)}/resources/dataset/${enc(dataset)}">정의</a>` : ""}
        ${d.catalog ? `<a class="button" href="#/catalog?collection=${enc(collection)}">${icon("catalog")}카탈로그</a>
        <button class="button" id="dataset-sync" title="이 컬렉션의 Delta 로그를 다시 읽습니다">${icon("refresh")}다시 읽기</button>` : ""}`,
    })}
    ${d.error ? `<div class="alert danger">${icon("alert")}<div><strong>테이블을 읽지 못했습니다</strong><div class="mono">${esc(d.error)}</div></div></div>` : ""}
    ${d.has_data ? `<div class="stats" style="margin-bottom:20px">
      ${stat("dataset", number(d.rows), "행")}
      ${stat("dataset", bytes(d.size_bytes), "크기")}
      ${stat("dataset", number(d.files), "파일")}
      ${stat("dataset", `v${d.version}`, "버전")}
      ${stat("dataset", esc(ago(d.modified_at) || "-"), "마지막 변경")}
    </div>` : `<div class="alert">${icon("alert")}<div>아직 테이블이 없습니다. 이 데이터셋에 쓰는 파이프라인을 실행하면 채워집니다.${d.lineage.writers.length ? ` 쓰는 파이프라인: ${d.lineage.writers.map((w) => `<a href="#/collections/${enc(collection)}/pipelines/${enc(w.pipeline)}">${esc(w.pipeline)}</a>`).join(", ")}` : ""}</div></div>`}
    <nav class="tabs">${DATASET_TABS.map(([key, label, ic]) => `<a href="${datasetHref(collection, dataset, key)}" class="${tab === key ? "active" : ""}">${icon(ic)}${label}${key === "schema" ? ` <span class="count">${d.columns.length}</span>` : key === "history" && d.history.length ? ` <span class="count">${d.history.length}</span>` : ""}</a>`).join("")}</nav>
    <div id="catalog-tab"></div>`;
  const sync = document.getElementById("dataset-sync");
  if (sync) sync.addEventListener("click", async () => {
    sync.disabled = true;
    try { await api("POST", "/catalog/sync", { collection, force: true }); toast("다시 읽었습니다"); route(); }
    catch (error) { toast(error.message, true); sync.disabled = false; }
  });
  const el = document.getElementById("catalog-tab");
  const tabs = { overview: datasetOverview, schema: datasetSchema, rows: datasetRows, history: datasetHistory, lineage: datasetLineage, query: datasetQuery };
  await (tabs[tab] || datasetOverview)(el, d, params);
}

async function saveNotes(d, changes) {
  await api("PUT", `/collections/${enc(d.collection)}/datasets/${enc(d.name)}/notes`, changes);
  toast("저장했습니다");
  route();
}

function datasetOverview(el, d) {
  const props = [
    ["위치", `<span class="mono">${esc(d.uri)}</span>`],
    ["형식", "Delta Lake"],
    ["파티션", d.partition_columns.length ? d.partition_columns.map((c) => `<code>${esc(c)}</code>`).join(" ") : '<span class="hint">없음</span>'],
    ["만든 시각", esc(time(d.created_at)) || "-"],
    ["마지막 변경", d.modified_at ? `${esc(time(d.modified_at))} <span class="hint">${esc(ago(d.modified_at))}</span>` : "-"],
    ["테이블 속성", Object.keys(d.properties).length ? Object.entries(d.properties).map(([k, v]) => `<div><span class="mono">${esc(k)}</span> = <span class="mono">${esc(v)}</span></div>`).join("") : '<span class="hint">없음</span>'],
    ["데이터셋 id", d.dataset_id ? `<span class="mono">${esc(d.dataset_id)}</span>` : '<span class="hint">-</span>'],
    ...(d.catalog ? [["카탈로그 동기화", `${esc(time(d.synced_at))} <span class="hint">${esc(ago(d.synced_at))}</span>`]] : []),
  ];
  const n = d.notes;
  el.innerHTML = `
    <div class="catalog-overview">
      <div>
        <h2 style="margin-top:0">문서</h2>
        <div class="panel readme">${n.readme ? esc(n.readme) : '<span class="hint">아직 문서가 없습니다. 오른쪽에서 이 데이터셋이 무엇인지, 어떻게 쓰는지 적어 두세요.</span>'}</div>
        <h2>속성</h2>
        <div class="table-card"><table>${props.map(([k, v]) => `<tr><th style="width:160px">${k}</th><td>${v}</td></tr>`).join("")}</table></div>
      </div>
      <form class="panel notes" id="notes-form">
        <h2 style="margin-top:0">${icon("tag")}설명 정보</h2>
        ${d.catalog ? "" : `<div class="hint">${NEEDS_POSTGRES}</div>`}
        <fieldset ${d.catalog ? "" : "disabled"}>
        <label for="notes-owner">소유자</label>
        <input id="notes-owner" type="text" value="${esc(n.owner || "")}" placeholder="담당자나 팀">
        <label for="notes-tags">태그</label>
        <input id="notes-tags" type="text" value="${esc(n.tags.join(", "))}" placeholder="쉼표로 구분 (예: 매출, 일간)">
        <div class="hint">D.Hub for AI 정의의 태그는 따로 붙어 있으며 여기서 바꾸지 않습니다.</div>
        <label for="notes-readme">문서</label>
        <textarea id="notes-readme" rows="8" placeholder="이 데이터셋에 무엇이 들어 있고 어떻게 만들어지는지">${esc(n.readme || "")}</textarea>
        <div class="form-actions"><button class="button primary" type="submit">저장</button>${n.updated_at ? `<span class="hint" style="align-self:center">${esc(time(n.updated_at))}에 고침</span>` : ""}</div>
        </fieldset>
      </form>
    </div>`;
  el.querySelector("#notes-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await saveNotes(d, {
        owner: el.querySelector("#notes-owner").value,
        tags: el.querySelector("#notes-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
        readme: el.querySelector("#notes-readme").value,
      });
    } catch (error) { toast(error.message, true); }
  });
}

function datasetSchema(el, d) {
  if (!d.columns.length) { el.innerHTML = empty("컬럼이 없습니다. 테이블이 생기거나 정의에 스키마가 있으면 보입니다.", "table"); return; }
  el.innerHTML = `
    <div class="row" style="margin-bottom:12px"><input id="column-filter" type="search" placeholder="컬럼 찾기" style="max-width:320px"><span class="spacer"></span><span class="hint">통계는 Delta 로그에 적힌 파일별 통계를 모은 것입니다.</span></div>
    <form id="columns-form"><div class="table-card"><table id="columns">
      <tr><th>#</th><th>컬럼</th><th>타입</th><th>null 허용</th><th style="min-width:260px">설명 / 메모</th><th>null 수</th><th>최소</th><th>최대</th></tr>
      ${d.columns.map((c, i) => `<tr data-column="${esc(c.name.toLowerCase())} ${esc((c.description || "").toLowerCase())} ${esc((c.note || "").toLowerCase())}">
        <td class="hint">${i + 1}</td>
        <td><span class="mono" style="font-weight:500">${esc(c.name)}</span>${d.partition_columns.includes(c.name) ? ' <span class="badge">파티션</span>' : ""}</td>
        <td class="mono">${esc(c.type || "-")}</td>
        <td>${c.nullable === null ? "-" : c.nullable ? "예" : "아니오"}</td>
        <td>${c.description ? `<div>${esc(c.description)}</div>` : ""}${d.catalog ? `<input type="text" data-note="${esc(c.name)}" value="${esc(c.note || "")}" placeholder="메모 추가" class="inline-input">` : ""}</td>
        <td>${c.null_count === null ? '<span class="hint">-</span>' : `${number(c.null_count)}${d.rows ? `<div class="hint">${((c.null_count / d.rows) * 100).toFixed(1)}%</div>` : ""}`}</td>
        <td class="mono">${c.min === null ? '<span class="hint">-</span>' : esc(c.min)}</td>
        <td class="mono">${c.max === null ? '<span class="hint">-</span>' : esc(c.max)}</td></tr>`).join("")}
    </table></div>
    ${d.catalog ? '<div class="form-actions"><button class="button primary" type="submit">메모 저장</button></div>' : ""}</form>`;
  el.querySelector("#column-filter").addEventListener("input", (event) => {
    const needle = event.target.value.trim().toLowerCase();
    el.querySelectorAll("[data-column]").forEach((row) => (row.hidden = needle && !row.dataset.column.includes(needle)));
  });
  el.querySelector("#columns-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const columns = {};
    el.querySelectorAll("[data-note]").forEach((input) => {
      const before = d.columns.find((c) => c.name === input.dataset.note).note || "";
      if (input.value.trim() !== before) columns[input.dataset.note] = input.value.trim();
    });
    if (!Object.keys(columns).length) return toast("바뀐 메모가 없습니다");
    try { await saveNotes(d, { columns }); } catch (error) { toast(error.message, true); }
  });
}

async function datasetRows(el, d, params) {
  if (!d.has_data) { el.innerHTML = empty("아직 테이블이 없습니다.", "eye"); return; }
  const version = params.get("version");
  const limit = Number(params.get("limit") || 100);
  const base = `/collections/${enc(d.collection)}/datasets/${enc(d.name)}`;
  const at = version !== null && version !== "" ? `version=${enc(version)}` : "";
  el.innerHTML = `<div class="hint">불러오는 중…</div>`;
  const p = await api("GET", `${base}/rows?limit=${limit}${at ? `&${at}` : ""}`);
  const versions = d.history.map((h) => h.version);
  el.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <label class="row" style="margin:0">버전
        <select id="preview-version"><option value="">최신 (v${d.version})</option>${versions.map((v) => `<option value="${v}" ${String(v) === version ? "selected" : ""}>v${v}</option>`).join("")}</select></label>
      <label class="row" style="margin:0">행
        <select id="preview-limit">${[20, 100, 500, 1000].map((n) => `<option ${n === limit ? "selected" : ""}>${n}</option>`).join("")}</select></label>
      <span class="spacer"></span>
      <span class="hint">${number(p.total)}행 중 ${number(p.rows.length)}행${version ? ` · v${esc(version)} 시점` : ""}</span>
      <a class="button small" href="/api${base}/export?format=csv${at ? `&${at}` : ""}">${icon("download")}CSV</a>
      <a class="button small" href="/api${base}/export?format=parquet${at ? `&${at}` : ""}">${icon("download")}Parquet</a>
    </div>
    ${rowsTable(p.columns, p.rows)}`;
  const go = () => {
    const v = el.querySelector("#preview-version").value;
    location.hash = datasetHref(d.collection, d.name, "rows", `${v ? `version=${v}&` : ""}limit=${el.querySelector("#preview-limit").value}`);
  };
  el.querySelector("#preview-version").addEventListener("change", go);
  el.querySelector("#preview-limit").addEventListener("change", go);
}

const OPERATIONS = { WRITE: "쓰기", DELETE: "삭제", MERGE: "병합", UPDATE: "갱신", "CREATE TABLE": "생성", OPTIMIZE: "최적화", VACUUM: "정리" };

function datasetHistory(el, d) {
  if (!d.history.length) { el.innerHTML = empty("커밋 이력이 없습니다.", "history"); return; }
  const metric = (m, key) => (m[key] === undefined ? "" : number(m[key]));
  el.innerHTML = `<div class="table-card"><table>
    <tr><th>버전</th><th>시각</th><th>작업</th><th>모드</th><th>추가 행</th><th>파일 (+/−)</th><th>조건</th><th></th></tr>
    ${d.history.map((h) => `<tr>
      <td class="mono">v${h.version}</td>
      <td>${esc(time(h.committed_at))}<div class="hint">${esc(ago(h.committed_at))}</div></td>
      <td>${esc(OPERATIONS[h.operation] || h.operation || "")}</td>
      <td>${esc(h.parameters.mode || "")}</td>
      <td>${metric(h.metrics, "num_added_rows") || metric(h.metrics, "numOutputRows")}</td>
      <td>${metric(h.metrics, "num_added_files")}${h.metrics.num_removed_files ? ` / ${number(h.metrics.num_removed_files)}` : ""}</td>
      <td class="mono">${esc(h.parameters.predicate || "")}</td>
      <td style="text-align:right"><a class="button small" href="${datasetHref(d.collection, d.name, "rows", `version=${h.version}`)}">${icon("eye")}이 버전 보기</a></td></tr>`).join("")}
  </table></div>`;
}

function datasetLineage(el, d) {
  const L = d.lineage;
  const datasetCard = (name) => `<a class="lineage-node" href="${datasetHref(d.collection, name)}">${entityIcon("dataset")}<span>${esc(name)}</span></a>`;
  const stepCard = (u) => `<a class="lineage-node step" href="#/collections/${enc(d.collection)}/pipelines/${enc(u.pipeline)}">${entityIcon("pipeline")}<span><span class="mono">${esc(u.pipeline)}</span><span class="hint"> / ${esc(u.step)}${u.mode ? ` · ${esc(u.mode)}` : ""}</span></span></a>`;
  const unique = (list) => [...new Set(list)];
  const column = (title, body) => `<div class="lineage-col"><div class="facet-title">${title}</div>${body || '<div class="hint">없음</div>'}</div>`;
  const arrow = `<div class="lineage-arrow">${icon("arrow")}</div>`;
  if (!L.writers.length && !L.readers.length) { el.innerHTML = empty("이 데이터셋을 읽거나 쓰는 파이프라인 스텝이 없습니다.", "lineage"); return; }
  el.innerHTML = `
    <div class="lineage">
      ${column("상위 데이터셋", unique(L.upstream.map((u) => u.dataset)).map(datasetCard).join(""))}${arrow}
      ${column("쓰는 스텝", L.writers.map(stepCard).join(""))}${arrow}
      ${column("이 데이터셋", `<div class="lineage-node current">${entityIcon("dataset")}<span>${esc(d.name)}</span></div>`)}${arrow}
      ${column("읽는 스텝", L.readers.map(stepCard).join(""))}${arrow}
      ${column("하위 데이터셋", unique(L.downstream.map((u) => u.dataset)).map(datasetCard).join(""))}
    </div>
    <p class="hint">계보는 컬렉션 파이프라인의 스텝 입력·출력에서 만듭니다. 상위는 이 데이터셋을 쓰는 스텝이 읽는 데이터셋, 하위는 이 데이터셋을 읽는 스텝이 쓰는 데이터셋입니다.</p>`;
}

async function datasetQuery(el, d) {
  const name = d.collection;
  const quote = (n) => `"${n.replace(/"/g, '""')}"`;
  el.innerHTML = `
    <div class="query-layout">
      <div>
        <form id="query-form" class="panel">
          <textarea id="query-sql" rows="7" class="mono" spellcheck="false">SELECT *\nFROM ${esc(quote(d.name))}\nLIMIT 100</textarea>
          <div class="row" style="margin-top:10px">
            <button class="button primary" type="submit">${icon("play")}실행</button><span class="hint">Ctrl/⌘ + Enter</span>
            <span class="spacer"></span>
            <input id="query-title" type="text" placeholder="저장할 이름" style="max-width:220px">
            <button class="button" type="button" id="query-save">저장</button>
          </div>
          <div class="hint" style="margin-top:8px">Polars SQL로 <code>SELECT</code> 하나를 실행합니다(읽기 전용, 최대 1000행). 이 컬렉션의 데이터셋끼리 조인할 수 있습니다.</div>
        </form>
        <div id="query-result" style="margin-top:16px"></div>
      </div>
      <aside class="panel saved-queries"><div class="facet-title">저장한 쿼리</div><div id="saved-list"><div class="hint">불러오는 중…</div></div></aside>
    </div>`;
  const sql = el.querySelector("#query-sql");
  const result = el.querySelector("#query-result");
  let editing = null;
  const run = async () => {
    result.innerHTML = '<div class="hint">실행 중…</div>';
    const started = performance.now();
    try {
      const r = await api("POST", `/collections/${enc(name)}/query`, { sql: sql.value, max_rows: 1000 });
      const columns = r.rows.length ? Object.keys(r.rows[0]).map((c) => ({ name: c })) : [];
      result.innerHTML = `<div class="row" style="margin-bottom:8px"><span class="hint">${number(r.row_count)}행${r.truncated ? " (1000행에서 잘림)" : ""} · ${Math.round(performance.now() - started)}ms · 읽은 테이블 ${r.tables.map((t) => `<span class="mono">${esc(t)}</span>`).join(", ") || "없음"}</span></div>
        ${r.rows.length ? rowsTable(columns, r.rows) : empty("결과가 없습니다.", "table")}`;
    } catch (error) {
      result.innerHTML = `<div class="alert danger">${icon("alert")}<pre style="background:none;border:none;padding:0">${esc(error.message)}</pre></div>`;
    }
  };
  const loadSaved = async () => {
    if (!d.catalog) {
      el.querySelector("#saved-list").innerHTML = `<div class="hint">${NEEDS_POSTGRES}</div>`;
      el.querySelector("#query-save").disabled = true;
      return;
    }
    const list = await api("GET", `/collections/${enc(name)}/queries`);
    const box = el.querySelector("#saved-list");
    box.innerHTML = list.length ? list.map((q) => `
      <div class="saved"><button type="button" class="session-open" data-load="${esc(q.id)}"><span class="session-title">${esc(q.title)}</span><span class="hint mono">${esc(q.sql.slice(0, 60))}</span></button>
        <button class="icon-button" type="button" data-remove="${esc(q.id)}" title="삭제" aria-label="삭제">${icon("trash")}</button></div>`).join("")
      : '<div class="hint">저장한 쿼리가 없습니다. 이 컬렉션의 모든 데이터셋에서 함께 씁니다.</div>';
    box.querySelectorAll("[data-load]").forEach((b) => b.addEventListener("click", () => {
      const q = list.find((x) => x.id === b.dataset.load);
      sql.value = q.sql;
      el.querySelector("#query-title").value = q.title;
      editing = q.id;
      run();
    }));
    box.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
      try { await api("DELETE", `/collections/${enc(name)}/queries/${enc(b.dataset.remove)}`); if (editing === b.dataset.remove) editing = null; loadSaved(); }
      catch (error) { toast(error.message, true); }
    }));
  };
  el.querySelector("#query-form").addEventListener("submit", (event) => { event.preventDefault(); run(); });
  sql.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); run(); } });
  el.querySelector("#query-save").addEventListener("click", async () => {
    const title = el.querySelector("#query-title").value.trim();
    if (!title) return toast("저장할 이름을 입력하세요", true);
    try {
      const saved = await api("POST", `/collections/${enc(name)}/queries`, { id: editing, title, sql: sql.value });
      editing = saved.id;
      toast("저장했습니다");
      loadSaved();
    } catch (error) { toast(error.message, true); }
  });
  await loadSaved();
}

// -- chrome ------------------------------------------------------------------

function setupChrome() {
  document.querySelectorAll("[data-icon]").forEach((el) => (el.outerHTML = icon(el.dataset.icon)));
  const toggle = document.getElementById("theme-toggle");
  const paint = () => (toggle.innerHTML = icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon"));
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("runner-theme", next); } catch {}
    paint();
  });
  paint();
}

// -- router ------------------------------------------------------------------

async function route() {
  clearTimeout(refreshTimer);
  const [path, query = ""] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  try {
    if (!parts.length) return await home();
    if (parts[0] === "import") return await importPage(params);
    if (parts[0] === "secrets") return await secretsPage();
    if (parts[0] === "ops") return await opsPage();
    if (parts[0] === "catalog") return await catalogPage(params);
    if (parts[0] === "collections" && parts[1]) {
      const [, name, section, a, b] = parts;
      if (!section) return await collectionPage(name, "overview");
      if (section === "secrets") return await collectionPage(name, "secrets");
      if (section === "pipelines") return await pipelinePage(name, a);
      if (section === "datasets") return await datasetPage(name, a, b || "overview", params);
      if (section === "agents") return await agentPage(name, a);
      if (section === "resources") return await resourcePage(name, a, b);
    }
    view.innerHTML = empty("페이지를 찾을 수 없습니다.", "alert");
  } catch (error) {
    view.innerHTML = `<div class="alert danger">${icon("alert")}<div><strong>불러오지 못했습니다</strong><div>${esc(error.message)}</div></div></div>`;
  }
}

setupChrome();
window.addEventListener("hashchange", route);
route();
