// D.Hub for AI chatbot: talk to one agent of the runner (/chat?collection=&agent=[&embed=1]).
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;
const params = new URLSearchParams(location.search);

// -- icons (Tabler, MIT) ------------------------------------------------------

const ICONS = {
  agent: '<path d="M6 4m0 2a2 2 0 0 1 2 -2h8a2 2 0 0 1 2 2v4a2 2 0 0 1 -2 2h-8a2 2 0 0 1 -2 -2z"/><path d="M12 2v2"/><path d="M9 12v9"/><path d="M15 12v9"/><path d="M5 16l4 -2"/><path d="M15 14l4 2"/><path d="M9 18h6"/><path d="M10 8v.01"/><path d="M14 8v.01"/>',
  plus: '<path d="M12 5l0 14"/><path d="M5 12l14 0"/>',
  trash: '<path d="M4 7l16 0"/><path d="M10 11l0 6"/><path d="M14 11l0 6"/><path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2 -2l1 -12"/><path d="M9 7v-3a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v3"/>',
  send: '<path d="M10 14l11 -11"/><path d="M21 3l-6.5 18a.55 .55 0 0 1 -1 0l-3.5 -7l-7 -3.5a.55 .55 0 0 1 0 -1l18 -6.5"/>',
  tool: '<path d="M7 10h3v-3l-3.5 -3.5a6 6 0 0 1 8 8l6 6a2 2 0 0 1 -3 3l-6 -6a6 6 0 0 1 -8 -8l3.5 3.5"/>',
  check: '<path d="M5 12l5 5l10 -10"/>',
  x: '<path d="M18 6l-12 12"/><path d="M6 6l12 12"/>',
  chevron: '<path d="M9 6l6 6l-6 6"/>',
  menu: '<path d="M4 6l16 0"/><path d="M4 12l16 0"/><path d="M4 18l16 0"/>',
  external: '<path d="M12 6h-6a2 2 0 0 0 -2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-6"/><path d="M11 13l9 -9"/><path d="M15 4h5v5"/>',
  copy: '<path d="M7 7m0 2.667a2.667 2.667 0 0 1 2.667 -2.667h8.666a2.667 2.667 0 0 1 2.667 2.667v8.666a2.667 2.667 0 0 1 -2.667 2.667h-8.666a2.667 2.667 0 0 1 -2.667 -2.667z"/><path d="M4.012 16.737a2.005 2.005 0 0 1 -1.012 -1.737v-10c0 -1.1 .9 -2 2 -2h10c.75 0 1.158 .385 1.5 1"/>',
  alert: '<path d="M12 9v4"/><path d="M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0z"/><path d="M12 16h.01"/>',
  sun: '<path d="M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"/><path d="M3 12h1m8 -9v1m8 8h1m-9 8v1m-6.4 -15.4l.7 .7m12.1 -.7l-.7 .7m0 11.4l.7 .7m-12.1 -.7l-.7 .7"/>',
  moon: '<path d="M12 3c.132 0 .263 0 .393 0a7.5 7.5 0 0 0 7.92 12.446a9 9 0 1 1 -8.313 -12.454z"/>',
};
const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

// -- markdown -----------------------------------------------------------------
// A small renderer for what agents answer with: paragraphs, headings, lists,
// quotes, tables, fenced code and inline code/bold/italic/links. Everything is
// escaped first; only the tags made here reach the page.

function inline(text) {
  const codes = [];
  let out = esc(text).replace(/`([^`]+)`/g, (_, code) => `\u0000${codes.push(code) - 1}\u0000`);
  out = out
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}

const isRow = (line) => /^\s*\|.*\|\s*$/.test(line);
const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
const NUMBER = /^-?[\d,]+(\.\d+)?%?$/;

function table(lines) {
  const head = cells(lines[0]);
  const rows = lines.slice(2).map(cells);
  // A column of numbers is right-aligned, its header too.
  const numeric = head.map((_, i) => rows.length > 0 && rows.every((r) => !r[i] || NUMBER.test(r[i])));
  const cls = (i) => (numeric[i] ? ' class="num"' : "");
  return `<div class="md-table"><table><thead><tr>${head.map((h, i) => `<th${cls(i)}>${inline(h)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${head.map((_, i) => `<td${cls(i)}>${inline(r[i] || "")}</td>`).join("")}</tr>`)
    .join("")}</tbody></table></div>`;
}

function codeBlock(lang, code) {
  return `<div class="md-code"><div class="md-code-head"><span>${esc(lang || "code")}</span><button class="icon-button" type="button" data-copy title="복사" aria-label="복사">${icon("copy")}</button></div><pre><code>${esc(code)}</code></pre></div>`;
}

function markdown(source) {
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```\s*([\w+-]*)/);
    if (fence) {
      const body = [];
      for (i++; i < lines.length && !/^\s*```\s*$/.test(lines[i]); i++) body.push(lines[i]);
      i++;
      html.push(codeBlock(fence[1], body.join("\n")));
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { html.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { html.push("<hr>"); i++; continue; }
    if (isRow(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const rows = [];
      while (i < lines.length && isRow(lines[i])) rows.push(lines[i++]);
      html.push(table(rows));
      continue;
    }
    const list = line.match(/^\s*([-*+]|\d+[.)])\s+/);
    if (list) {
      const ordered = /\d/.test(list[1]);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let item = lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) item += " " + lines[i++].trim();
        items.push(`<li>${inline(item)}</li>`);
      }
      html.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      html.push(`<blockquote>${markdown(quote.join("\n"))}</blockquote>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*(```|#{1,4}\s|>|([-*+]|\d+[.)])\s)/.test(lines[i]) && !isRow(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    html.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  return html.join("");
}

// -- api ----------------------------------------------------------------------

async function api(method, path, body) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!response.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

// POST that answers with server-sent events; each event goes to onEvent.
async function stream(path, body, onEvent) {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
      if (data) onEvent(JSON.parse(data));
    }
  }
}

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), isError ? 8000 : 2500);
}

// -- state --------------------------------------------------------------------
// A conversation is a list of turns: {user, steps: [{name, args, state, error}],
// answer, error, pending: [{id, name, args}], decided}.

const state = {
  agents: [],        // [{collection, alias, name, description, status}]
  current: null,     // the chosen agent
  sessions: [],
  sessionId: null,
  turns: [],
  busy: false,
};

const agentPath = (a) => `/collections/${enc(a.collection)}/agents/${enc(a.name)}`;
const keyOf = (a) => `${a.collection}/${a.name}`;

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}
function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

async function loadAgents() {
  const collections = await api("GET", "/operations");
  state.agents = collections.flatMap((c) =>
    (c.agents || []).map((a) => ({ collection: c.name, alias: c.alias, ...a })));
  const select = $("agent-select");
  const groups = {};
  for (const a of state.agents) (groups[a.collection] = groups[a.collection] || []).push(a);
  select.innerHTML = Object.entries(groups).map(([collection, agents]) =>
    `<optgroup label="${esc(agents[0].alias || collection)}">${agents.map((a) =>
      `<option value="${esc(keyOf(a))}">${esc(a.name)}${a.status === "stopped" ? " (중지됨)" : ""}</option>`).join("")}</optgroup>`).join("")
    || '<option value="">에이전트가 없습니다</option>';
}

async function choose(agent, sessionId = null) {
  state.current = agent;
  state.sessionId = null;
  state.turns = [];
  if (!agent) { renderHead(); renderThread(); renderSessions(); return; }
  $("agent-select").value = keyOf(agent);
  remember("chatbot-agent", keyOf(agent));
  const url = new URL(location.href);
  url.searchParams.set("collection", agent.collection);
  url.searchParams.set("agent", agent.name);
  history.replaceState(null, "", url);
  await refreshAgent();
  if (sessionId) await openSession(sessionId);
  else { renderThread(); renderSessions(); }
}

async function refreshAgent() {
  try {
    const info = await api("GET", agentPath(state.current));
    Object.assign(state.current, { status: info.status, description: info.description, problems: info.problems });
    state.sessions = info.sessions || [];
  } catch (error) {
    state.sessions = [];
    state.current.problems = [error.message];
  }
  renderHead();
  renderSessions();
}

async function openSession(id) {
  try {
    const session = await api("GET", `${agentPath(state.current)}/sessions/${enc(id)}`);
    state.sessionId = session.session_id;
    state.turns = turnsOf(session.messages, session.pending || []);
  } catch (error) {
    toast(error.message, true);
  }
  closeSide();
  renderSessions();
  renderThread();
}

// A stored session's messages as turns.
function turnsOf(messages, pending) {
  const turns = [];
  let turn = null;
  const results = {};
  for (const m of messages || []) if (m.role === "tool") results[m.tool_call_id] = m.content;
  for (const m of messages || []) {
    const text = typeof m.content === "string" ? m.content : (m.content || []).map((c) => c.text || "").join("");
    if (m.role === "human") turns.push((turn = { user: text, steps: [], answer: "", pending: [] }));
    else if (m.role === "ai" && turn) {
      for (const call of m.tool_calls || []) {
        const error = toolError(results[call.id]);
        turn.steps.push({ name: call.name, args: call.args, state: error ? "fail" : "ok", error });
      }
      if (text) turn.answer = text;
    }
  }
  if (turn && pending.length) turn.pending = pending;
  return turns;
}

function toolError(content) {
  try {
    const parsed = JSON.parse(content);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed.error || null : null;
  } catch { return null; }
}

// -- sending ------------------------------------------------------------------

async function send(turn, messages) {
  state.busy = true;
  turn.working = true;
  renderThread();
  renderComposer();
  try {
    await stream(`${agentPath(state.current)}/chat/stream`, { session_id: state.sessionId, messages }, (event) => {
      if (event.type === "tool") {
        turn.steps.push({ name: event.name, args: event.args, state: "run" });
      } else if (event.type === "tool_result") {
        const step = [...turn.steps].reverse().find((s) => s.name === event.name && s.state === "run");
        if (step) Object.assign(step, { state: event.error ? "fail" : "ok", error: event.error || null });
      } else if (event.type === "done") {
        state.sessionId = event.session_id;
        if (event.content) turn.answer = event.content;
        turn.pending = event.pending || [];
        turn.decided = {};
      } else if (event.type === "error") {
        turn.error = event.message;
      }
      renderThread();
    });
  } catch (error) {
    turn.error = error.message;
  } finally {
    turn.working = false;
    for (const step of turn.steps) if (step.state === "run") step.state = "ok";
    state.busy = false;
    renderThread();
    renderComposer();
    refreshAgent();
  }
}

function ask(text) {
  if (!state.current || state.busy || !text.trim()) return;
  if (state.turns.some((t) => t.pending && t.pending.length)) {
    toast("승인을 기다리는 작업이 있습니다. 먼저 승인하거나 거부하세요.", true);
    return;
  }
  const turn = { user: text, steps: [], answer: "", pending: [] };
  state.turns.push(turn);
  send(turn, [{ role: "user", content: text }]);
}

function decide(turn, id, approved) {
  turn.decided = turn.decided || {};
  turn.decided[id] = approved;
  if (turn.pending.every((c) => c.id in turn.decided)) {
    const outgoing = turn.pending.map((c) => ({ role: "tool", tool_call_id: c.id, content: JSON.stringify({ approved: turn.decided[c.id] }) }));
    turn.settled = turn.pending.map((c) => ({ ...c, approved: turn.decided[c.id] }));
    turn.pending = [];
    send(turn, outgoing);
  } else renderThread();
}

// -- rendering ----------------------------------------------------------------

function renderHead() {
  const a = state.current;
  $("agent-name").textContent = a ? a.name : "에이전트를 고르세요";
  $("agent-sub").textContent = a ? [a.alias || a.collection, a.description].filter(Boolean).join(" · ") : "";
  const status = a && a.status;
  // Only what keeps the agent from answering at once: stopped, or its pod starting.
  const label = { stopped: "중지됨", starting: "시작 중", idle: "대기 중" }[status];
  $("agent-status").innerHTML = label ? `<span class="badge ${esc(status)}">${label}</span>` : "";
  $("admin-link").href = a ? `/#/b/${enc(a.collection)}/a/${enc(a.name)}` : "/";
  document.title = a ? `${a.name} · D.Hub for AI 챗봇` : "D.Hub for AI 챗봇";
  renderComposer();
}

function renderSessions() {
  const box = $("sessions");
  if (!state.current) { box.innerHTML = ""; return; }
  box.innerHTML = state.sessions.length ? state.sessions.map((s) => `
    <div class="cb-session ${s.session_id === state.sessionId ? "active" : ""}">
      <button class="open" type="button" data-open="${esc(s.session_id)}" title="${esc(s.title || s.session_id)}">
        <span class="title">${esc(s.title || s.session_id)}</span>
        <span class="meta">${esc(when(s.updated_at))}${s.pending ? " · 승인 대기" : ""}</span>
      </button>
      <button class="icon-button" type="button" data-remove="${esc(s.session_id)}" title="대화 삭제" aria-label="대화 삭제">${icon("trash")}</button>
    </div>`).join("") : '<div class="cb-none">아직 대화가 없습니다.</div>';
}

function when(value) {
  if (!value) return "";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return String(value);
  const today = new Date().toDateString() === date.toDateString();
  return today ? date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }) : date.toLocaleDateString("ko-KR", { month: "short", day: "numeric" });
}

// The argument worth showing for a step: a query's SQL, else compact JSON.
function stepDetail(step) {
  const args = step.args || {};
  const sql = typeof args.query === "string" && /\b(select|with)\b/i.test(args.query) ? args.query : null;
  const rest = Object.fromEntries(Object.entries(args).filter(([k]) => !(sql && k === "query")));
  return { sql, summary: Object.keys(rest).length ? JSON.stringify(rest) : "" };
}

function renderSteps(turn) {
  if (!turn.steps.length) return "";
  const running = turn.steps.some((s) => s.state === "run");
  const failed = turn.steps.filter((s) => s.state === "fail").length;
  const title = running ? `작업 중 · ${turn.steps.length}단계` : `${turn.steps.length}단계 작업${failed ? ` · 실패 ${failed}` : ""}`;
  return `<details class="cb-steps"${running || turn.open ? " open" : ""} data-steps>
    <summary>${running ? '<span class="spinner"></span>' : icon("tool")}<span>${title}</span><span class="chev">${icon("chevron")}</span></summary>
    ${turn.steps.map((s) => {
      const { sql, summary } = stepDetail(s);
      const mark = s.state === "run" ? '<span class="cb-state"><span class="spinner"></span></span>'
        : `<span class="cb-state ${s.state}">${icon(s.state === "fail" ? "x" : "check")}</span>`;
      return `<div class="cb-step"><div class="cb-step-head">${mark}<span class="cb-step-name">${esc(s.name)}</span><span class="cb-step-args" title="${esc(summary)}">${esc(summary)}</span></div>
        ${sql ? codeBlock("sql", sql) : ""}${s.error ? `<div class="cb-step-error">${esc(s.error)}</div>` : ""}</div>`;
    }).join("")}
  </details>`;
}

// Calls the user approved or denied: shown before what the agent did next.
function renderSettled(turn) {
  return (turn.settled || []).map((c) =>
    `<div class="cb-approval"><div class="row">${icon(c.approved ? "check" : "x")}<strong>${esc(c.name)}</strong><span class="done">${c.approved ? "승인함" : "거부함"}</span></div></div>`).join("");
}

function renderApprovals(turn, index) {
  return (turn.pending || []).map((c) => {
    const decided = turn.decided && c.id in turn.decided;
    return `<div class="cb-approval">
      <div class="row">${icon("alert")}<strong>${esc(c.name)}</strong><span class="done">실행하려면 승인이 필요합니다</span></div>
      <pre>${esc(JSON.stringify(c.args, null, 2))}</pre>
      <div class="row">${decided ? `<span class="done">${turn.decided[c.id] ? "승인함" : "거부함"} · 나머지를 기다리는 중</span>` : `
        <button class="button small primary" type="button" data-approve="${index}:${esc(c.id)}">승인</button>
        <button class="button small" type="button" data-deny="${index}:${esc(c.id)}">거부</button>`}</div>
    </div>`;
  }).join("");
}

function renderThread() {
  const thread = $("thread");
  const a = state.current;
  if (!a) {
    thread.innerHTML = `<div class="cb-empty"><div class="cb-avatar">${icon("agent")}</div><h1>에이전트가 없습니다</h1><p>runner에서 컬렉션을 가져오면 그 에이전트와 대화할 수 있습니다.</p></div>`;
    return;
  }
  if (!state.turns.length) {
    const problems = (a.problems || []).length ? `<div class="cb-error" style="text-align:left">${esc(a.problems.join("\n"))}</div>` : "";
    thread.innerHTML = `<div class="cb-empty"><div class="cb-avatar">${icon("agent")}</div>
      <h1>${esc(a.name)}</h1><p>${esc(a.description || "무엇이든 물어보세요.")}</p>${problems}</div>`;
    return;
  }
  const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  const open = [...thread.querySelectorAll("[data-steps]")].map((d) => d.open);
  thread.innerHTML = state.turns.map((t, i) => {
    if (open[i] !== undefined) t.open = open[i];
    const thinking = t.working && !t.steps.some((s) => s.state === "run") ? `<div class="cb-thinking"><span class="spinner"></span>생각하는 중…</div>` : "";
    return `<div class="cb-turn">
      <div class="cb-user">${esc(t.user)}</div>
      <div class="cb-bot"><div class="cb-avatar">${icon("agent")}</div><div class="cb-body">
        ${renderSettled(t)}${renderSteps(t)}${thinking}
        ${t.answer ? `<div class="md">${markdown(t.answer)}</div>` : ""}
        ${renderApprovals(t, i)}
        ${t.error ? `<div class="cb-error">${esc(t.error)}</div>` : ""}
      </div></div>
    </div>`;
  }).join("");
  if (nearBottom || state.busy) thread.scrollTop = thread.scrollHeight;
}

function renderComposer() {
  const a = state.current;
  const stopped = a && a.status === "stopped";
  const input = $("input");
  input.disabled = !a || stopped;
  input.placeholder = !a ? "에이전트를 고르세요" : stopped ? "중지된 에이전트입니다. runner에서 시작하세요." : "메시지를 입력하세요";
  $("send").disabled = !a || stopped || state.busy;
}

// -- events -------------------------------------------------------------------

function closeSide() {
  document.body.classList.remove("side-open");
  $("scrim").hidden = true;
}

function bind() {
  $("new-chat").innerHTML = `${icon("plus")}새 대화`;
  $("send").innerHTML = icon("send");
  $("side-toggle").innerHTML = icon("menu");
  $("admin-link").innerHTML = icon("external");
  const toggle = $("theme-toggle");
  const paint = () => (toggle.innerHTML = icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon"));
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    remember("runner-theme", next);
    paint();
  });
  paint();

  $("agent-select").addEventListener("change", (event) => {
    choose(state.agents.find((a) => keyOf(a) === event.target.value) || null);
    closeSide();
  });
  $("new-chat").addEventListener("click", () => {
    if (state.busy) return;
    state.sessionId = null;
    state.turns = [];
    renderSessions();
    renderThread();
    closeSide();
    $("input").focus();
  });
  $("sessions").addEventListener("click", async (event) => {
    const open = event.target.closest("[data-open]");
    const remove = event.target.closest("[data-remove]");
    if (state.busy) return;
    if (open) openSession(open.dataset.open);
    if (remove) {
      if (!confirm("이 대화를 삭제할까요?")) return;
      try {
        await api("DELETE", `${agentPath(state.current)}/sessions/${enc(remove.dataset.remove)}`);
        if (state.sessionId === remove.dataset.remove) { state.sessionId = null; state.turns = []; renderThread(); }
        await refreshAgent();
      } catch (error) { toast(error.message, true); }
    }
  });
  $("side-toggle").addEventListener("click", () => {
    document.body.classList.add("side-open");
    $("scrim").hidden = false;
  });
  $("scrim").addEventListener("click", closeSide);

  $("thread").addEventListener("click", (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const code = copy.closest(".md-code").querySelector("code").textContent;
      navigator.clipboard.writeText(code).then(() => toast("복사했습니다"), () => toast("복사하지 못했습니다", true));
      return;
    }
    const button = event.target.closest("[data-approve],[data-deny]");
    if (button && !state.busy) {
      const [index, id] = (button.dataset.approve || button.dataset.deny).split(/:(.*)/s);
      decide(state.turns[Number(index)], id, Boolean(button.dataset.approve));
    }
  });

  const input = $("input");
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 200)}px`; };
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value;
    if (!text.trim() || state.busy) return;
    input.value = "";
    grow();
    ask(text);
  });
}

async function main() {
  if (params.get("embed") === "1") document.body.classList.add("embed");
  bind();
  try {
    await loadAgents();
  } catch (error) {
    toast(error.message, true);
  }
  const asked = params.get("collection") && params.get("agent") ? `${params.get("collection")}/${params.get("agent")}` : null;
  const wanted = asked || recall("chatbot-agent");
  const agent = state.agents.find((a) => keyOf(a) === wanted) || (asked ? null : state.agents[0]) || null;
  if (asked && !agent) toast(`에이전트 ${asked}를 찾을 수 없습니다`, true);
  await choose(agent, params.get("session"));
  if (agent) $("input").focus();
}

main();
