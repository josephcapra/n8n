"use strict";
/* Agent-Manager PWA — chat client + WebAuthn passkey (Face ID). */

const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("agentmgr_token") || "";
// Chat is split into threads: "" is the Master; any other key is one agent you
// picked from the left rail ("Message"). Each thread keeps its own server-side
// conversation id + rendered bubbles, so switching peers never mixes context.
let activeTarget = null;           // null = Master; otherwise an agent name
const threads = {};                // key -> { conversationId, bubbles: [...] }
let approvalsTimer = null;
let agentsTimer = null;
let lastCost = null;
let selectedModel = localStorage.getItem("agentmgr_model") || "auto";
const runningAgents = new Set();   // agent names with in-flight work (progress bar)
let agentsByName = {};             // last /agents payload, keyed by name (info modal)

/* ---- base64url <-> ArrayBuffer ---- */
function b64uToBuf(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  s += "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
function bufToB64u(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/* ---- API wrapper ---- */
async function api(method, path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.admin) headers["Authorization"] = "Bearer " + opts.admin;
  else if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && !opts.admin) {
    logout();
    throw new Error("session expired — sign in again");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

/* ---- WebAuthn ceremonies ---- */
async function doRegister(optionsJson) {
  const o = JSON.parse(optionsJson);
  o.challenge = b64uToBuf(o.challenge);
  o.user.id = b64uToBuf(o.user.id);
  (o.excludeCredentials || []).forEach((c) => (c.id = b64uToBuf(c.id)));
  const cred = await navigator.credentials.create({ publicKey: o });
  return {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      attestationObject: bufToB64u(cred.response.attestationObject),
      clientDataJSON: bufToB64u(cred.response.clientDataJSON),
      transports: cred.response.getTransports ? cred.response.getTransports() : [],
    },
  };
}
async function doAssertion(optionsJson) {
  const o = JSON.parse(optionsJson);
  o.challenge = b64uToBuf(o.challenge);
  (o.allowCredentials || []).forEach((c) => (c.id = b64uToBuf(c.id)));
  const cred = await navigator.credentials.get({ publicKey: o });
  return {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      authenticatorData: bufToB64u(cred.response.authenticatorData),
      clientDataJSON: bufToB64u(cred.response.clientDataJSON),
      signature: bufToB64u(cred.response.signature),
      userHandle: cred.response.userHandle
        ? bufToB64u(cred.response.userHandle)
        : null,
    },
  };
}

/* ---- screens ---- */
function showLogin() {
  $("login").classList.remove("hidden");
  $("app").classList.add("hidden");
  if (approvalsTimer) clearInterval(approvalsTimer);
  if (agentsTimer) clearInterval(agentsTimer);
}
function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  activeTarget = null;
  for (const k in threads) delete threads[k];
  $("messages").innerHTML = "";
  updateChatContext();
  addBubble("sys", "Command center online. Message the manager, or pick an agent on the left.");
  refreshApprovals();
  approvalsTimer = setInterval(refreshApprovals, 6000);
  loadModels();
  loadStats().then(loadAgents);
  loadSecurity();
  loadWebsiteHealth();
  loadReports();
  agentsTimer = setInterval(() => {
    loadStats().then(loadAgents);
    loadSecurity();
    loadWebsiteHealth();
    loadReports();
  }, 12000);
}
function logout() {
  token = "";
  localStorage.removeItem("agentmgr_token");
  showLogin();
}

/* ---- auth flows ---- */
async function register() {
  const admin = $("adminToken").value.trim();
  if (!admin) return setMsg("setupMsg", "Enter the admin token first.", true);
  try {
    setMsg("setupMsg", "Creating passkey…");
    const begin = await api("POST", "/passkey/register/begin", { admin });
    const credential = await doRegister(begin.options);
    await api("POST", "/passkey/register/complete", {
      admin,
      body: { credential, label: $("passkeyLabel").value.trim() || "passkey" },
    });
    setMsg("setupMsg", "Passkey registered. Sign in with Face ID above.", false, true);
  } catch (e) {
    setMsg("setupMsg", "Registration failed: " + e.message, true);
  }
}
async function login() {
  try {
    setMsg("loginMsg", "Waiting for Face ID…");
    const begin = await api("POST", "/passkey/login/begin");
    const credential = await doAssertion(begin.options);
    const res = await api("POST", "/passkey/login/complete", {
      body: { credential },
    });
    token = res.token;
    localStorage.setItem("agentmgr_token", token);
    setMsg("loginMsg", "");
    showApp();
  } catch (e) {
    setMsg("loginMsg", "Sign-in failed: " + e.message, true);
  }
}
async function passwordLogin(ev) {
  if (ev) ev.preventDefault();
  const password = $("passwordInput").value;
  if (!password) return setMsg("loginMsg", "Enter your password.", true);
  try {
    setMsg("loginMsg", "Signing in…");
    const res = await api("POST", "/password/login", { body: { password } });
    token = res.token;
    localStorage.setItem("agentmgr_token", token);
    $("passwordInput").value = "";
    setMsg("loginMsg", "");
    showApp();
  } catch (e) {
    setMsg("loginMsg", "Sign-in failed: " + e.message, true);
  }
}

/* ---- chat + attachments ---- */
let pendingAttachments = []; // {file, kind:"image"|"file", url, name, size}
const MAX_FILES = 10;
const MAX_BYTES = 25 * 1024 * 1024;

function humanSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

/* ---- chat threads (Master + one per messaged agent) ---- */
function threadKey() { return activeTarget || ""; }
function curThread() {
  const k = threadKey();
  if (!threads[k]) {
    threads[k] = {
      conversationId: k === "" ? (localStorage.getItem("agentmgr_conversation") || null) : null,
      bubbles: [],
    };
  }
  return threads[k];
}
// Re-render the message pane from the active thread's stored bubbles.
function renderThread() {
  const m = $("messages");
  m.innerHTML = "";
  curThread().bubbles.forEach((b) => appendBubbleDom(b.role, b.text, b.atts));
  m.scrollTop = m.scrollHeight;
}

// Which agents answer a direct chat themselves vs. via the Master (mirrors the
// backend's _agent_chat_mode). "direct" = the agent's own loop; else proxied.
function chatModeFor(a) {
  const m = (a && a.details && a.details.chat_mode) || "";
  if (m === "direct" || m === "proxy") return m;
  return a && a.kind === "assistant" ? "direct" : "proxy";
}

// Switch the chat to `name` (null = back to the Manager).
function switchTarget(name) {
  activeTarget = name || null;
  curThread();                 // ensure it exists
  renderThread();
  updateChatContext();
  highlightActiveCard();
  $("chatInput").focus();
}

function openAgentChat(a) {
  switchTarget(a.name);
  // on phones the roster covers the chat — collapse it so the thread shows
  if (window.matchMedia("(max-width: 860px)").matches)
    $("rosterPane").classList.add("collapsed");
}

// Header bar naming the current peer + the composer placeholder.
function updateChatContext() {
  const bar = $("chatContext");
  const input = $("chatInput");
  if (!activeTarget) {
    bar.classList.add("hidden");
    if (input) input.placeholder = "Message the manager…  ($ shell · cloud run …)";
    return;
  }
  const a = agentsByName[activeTarget] || {};
  const title = a.title || activeTarget;
  bar.classList.remove("hidden");
  $("chatPeer").textContent = title;
  $("chatPeerMode").textContent =
    chatModeFor(a) === "direct" ? "· direct" : "· via Manager";
  if (input) input.placeholder = "Message " + title + "…";
}

// Mark the messaged agent's card as active in the rail.
function highlightActiveCard() {
  const list = $("agentList");
  if (!list) return;
  list.querySelectorAll(".agent-card").forEach((c) =>
    c.classList.toggle("active", c.dataset.agent === activeTarget));
}

// Append a chat bubble to the DOM (no thread bookkeeping — see addBubble).
function appendBubbleDom(role, text, atts) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  const imgs = (atts || []).filter((a) => a.kind === "image" && a.url);
  const files = (atts || []).filter((a) => a.kind !== "image");
  if (imgs.length) {
    const wrap = document.createElement("div");
    wrap.className = "att-imgs";
    imgs.forEach((a) => {
      const im = document.createElement("img");
      im.src = a.url;
      im.alt = a.name || "image";
      wrap.appendChild(im);
    });
    div.appendChild(wrap);
  }
  files.forEach((a) => {
    const f = document.createElement("div");
    f.className = "att-file";
    f.textContent = "📎 " + (a.name || "file");
    div.appendChild(f);
  });
  if (text) {
    const t = document.createElement("div");
    t.textContent = text;
    div.appendChild(t);
  }
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
}

// Add a bubble to the active thread: render it AND remember it so switching
// peers and switching back restores the conversation.
function addBubble(role, text, atts) {
  appendBubbleDom(role, text, atts);
  curThread().bubbles.push({ role, text, atts: atts || [] });
}

function addFiles(fileList) {
  for (const file of fileList) {
    if (pendingAttachments.length >= MAX_FILES) {
      addBubble("sys", "Attachment limit reached (" + MAX_FILES + ").");
      break;
    }
    if (file.size > MAX_BYTES) {
      addBubble("sys", '"' + file.name + '" is too large (max 25 MB).');
      continue;
    }
    const kind = (file.type || "").startsWith("image/") ? "image" : "file";
    pendingAttachments.push({
      file, kind, name: file.name || "file", size: file.size,
      url: kind === "image" ? URL.createObjectURL(file) : null,
    });
  }
  renderTray();
}

function removeAttachment(i) {
  const a = pendingAttachments[i];
  if (a && a.url) URL.revokeObjectURL(a.url);
  pendingAttachments.splice(i, 1);
  renderTray();
}

function renderTray() {
  const tray = $("attachTray");
  tray.innerHTML = "";
  if (!pendingAttachments.length) {
    tray.classList.add("hidden");
    return;
  }
  tray.classList.remove("hidden");
  pendingAttachments.forEach((a, i) => {
    const item = document.createElement("div");
    item.className = "attach-item";
    if (a.kind === "image" && a.url) {
      const im = document.createElement("img");
      im.src = a.url;
      item.appendChild(im);
    } else {
      const chip = document.createElement("div");
      chip.className = "fchip";
      const fn = document.createElement("span");
      fn.className = "fname";
      fn.textContent = a.name;
      const fm = document.createElement("span");
      fm.className = "fmeta";
      fm.textContent = humanSize(a.size);
      chip.append(fn, fm);
      item.appendChild(chip);
    }
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "rm";
    rm.textContent = "×";
    rm.onclick = () => removeAttachment(i);
    item.appendChild(rm);
    tray.appendChild(item);
  });
}

async function uploadAttachments() {
  const fd = new FormData();
  pendingAttachments.forEach((a) => fd.append("files", a.file));
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch("/upload", { method: "POST", headers, body: fd });
  if (res.status === 401) {
    logout();
    throw new Error("session expired — sign in again");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return (await res.json()).attachments;
}

async function sendMessage(ev) {
  ev.preventDefault();
  const input = $("chatInput");
  let text = input.value.trim();
  if (!text && !pendingAttachments.length) return;

  const bubbleAtts = pendingAttachments.map((a) => ({
    kind: a.kind, url: a.url, name: a.name,
  }));
  input.value = "";
  addBubble("user", text, bubbleAtts);

  let refs = [];
  try {
    if (pendingAttachments.length) {
      refs = await uploadAttachments();
    }
  } catch (e) {
    addBubble("sys", "Upload failed: " + e.message);
    return;
  }
  pendingAttachments = [];
  renderTray();

  if (!text && refs.length) text = "I've attached some files — take a look.";

  const th = curThread();
  const target = activeTarget;     // capture — user may switch peers mid-request
  try {
    const res = await api("POST", "/chat", {
      body: {
        message: text, conversation_id: th.conversationId,
        attachments: refs, model: selectedModel,
        target_agent: target || undefined,
      },
    });
    th.conversationId = res.conversation_id;
    if (target === null && res.conversation_id)
      localStorage.setItem("agentmgr_conversation", res.conversation_id);
    // land the reply in its own thread even if the user has since switched
    if (target === activeTarget) addBubble("master", res.reply);
    else if (threads[target || ""]) threads[target || ""].bubbles.push(
      { role: "master", text: res.reply, atts: [] });
  } catch (e) {
    addBubble("sys", "Error: " + e.message);
  }
}

/* ---- drag & drop + paste (screenshots) ---- */
function setupDropAndPaste() {
  const app = $("app");
  const zone = $("dropZone");
  let depth = 0;
  const hasFiles = (e) =>
    e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  app.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    zone.classList.remove("hidden");
  });
  app.addEventListener("dragover", (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
  app.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (--depth <= 0) {
      depth = 0;
      zone.classList.add("hidden");
    }
  });
  app.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    zone.classList.add("hidden");
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  document.addEventListener("paste", (e) => {
    if ($("app").classList.contains("hidden")) return;
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    const files = [];
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  });
}

/* ---- approvals ---- */
async function refreshApprovals() {
  let pending = [];
  try {
    pending = (await api("GET", "/approvals")).approvals;
  } catch (e) {
    return;
  }
  const banner = $("approvalBanner");
  if (!pending.length) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  banner.classList.remove("hidden");
  banner.innerHTML = "<strong>Approval needed</strong>";
  pending.forEach((a) => {
    const row = document.createElement("div");
    row.className = "arow";
    const label = document.createElement("div");
    label.className = "grow";
    label.textContent = a.action + " — " + a.reason;
    const ok = document.createElement("button");
    ok.className = "primary";
    ok.textContent = "Approve (Face ID)";
    ok.onclick = () => decideApproval(a.id, "approve", ok);
    const no = document.createElement("button");
    no.className = "secondary";
    no.textContent = "Deny";
    no.onclick = () => decideApproval(a.id, "deny", no);
    row.append(label, ok, no);
    banner.appendChild(row);
  });
}
async function decideApproval(id, decision, btn) {
  btn.disabled = true;
  try {
    const begin = await api("POST", "/approvals/" + id + "/options", {
      body: { decision },
    });
    const assertion = await doAssertion(begin.options);
    await api("POST", "/approvals/" + id + "/submit", {
      body: { decision, assertion },
    });
    addBubble("sys", "Approval " + decision + "d.");
    refreshApprovals();
  } catch (e) {
    addBubble("sys", "Approval failed: " + e.message);
    btn.disabled = false;
  }
}

/* ---- sessions ---- */
async function refreshSessions() {
  const list = $("sessionList");
  list.innerHTML = "";
  let sessions = [];
  try {
    sessions = (await api("GET", "/sessions")).sessions;
  } catch (e) {
    return;
  }
  sessions.forEach((s) => {
    const row = document.createElement("div");
    row.className = "sess";
    const info = document.createElement("div");
    info.className = "grow";
    info.innerHTML =
      "<strong>" + s.scope + "</strong><br><span class='small muted'>expires " +
      s.expires_at + "</span>";
    const pill = document.createElement("span");
    pill.className = "pill " + (s.active ? "on" : "off");
    pill.textContent = s.active ? "active" : "inactive";
    row.append(info, pill);
    if (s.active) {
      const rev = document.createElement("button");
      rev.className = "secondary";
      rev.textContent = "Revoke";
      rev.onclick = async () => {
        await api("POST", "/sessions/" + s.id + "/revoke");
        refreshSessions();
      };
      row.appendChild(rev);
    }
    list.appendChild(row);
  });
}
async function startSession() {
  const scope = $("sessScope").value;
  try {
    const begin = await api("POST", "/sessions/start/begin", { body: { scope } });
    const assertion = await doAssertion(begin.options);
    await api("POST", "/sessions/" + begin.grant_id + "/activate", {
      body: { assertion },
    });
    addBubble("sys", "Session window opened (" + scope + ").");
    refreshSessions();
  } catch (e) {
    alert("Could not start session: " + e.message);
  }
}

/* ---- cloud run jobs ---- */
async function loadJobs() {
  const sel = $("jobSelect");
  setMsg("jobsMsg", "");
  sel.innerHTML = "<option value=''>Loading jobs…</option>";
  try {
    const jobs = (await api("GET", "/cloudrun/jobs")).jobs || [];
    if (!jobs.length) {
      sel.innerHTML = "<option value=''>No jobs found</option>";
      return;
    }
    sel.innerHTML = jobs
      .map((j) => `<option value="${j.name}">${j.name}</option>`)
      .join("");
  } catch (e) {
    sel.innerHTML = "<option value=''>Couldn’t load jobs</option>";
    setMsg("jobsMsg", e.message, true);
  }
}
async function runSelectedJob() {
  const name = $("jobSelect").value;
  if (!name) return setMsg("jobsMsg", "Pick a job first.", true);
  const btn = $("runJobBtn");
  btn.disabled = true;
  setMsg("jobsMsg", "Starting " + name + "…");
  try {
    await api("POST", "/cloudrun/jobs/" + encodeURIComponent(name) + "/run");
    setMsg("jobsMsg", "Started " + name + ".", false, true);
    addBubble("sys", "Started Cloud Run job “" + name + "”.");
  } catch (e) {
    setMsg("jobsMsg", "Couldn’t run " + name + ": " + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ---- memory ---- */
async function loadMemory() {
  const list = $("memoryList");
  list.innerHTML = "";
  let memories = [];
  try {
    memories = (await api("GET", "/memory")).memories || [];
  } catch (e) {
    setMsg("memoryMsg", e.message, true);
    return;
  }
  if (!memories.length) {
    list.innerHTML = "<p class='muted small'>Nothing saved yet.</p>";
    return;
  }
  memories.forEach((m) => {
    const row = document.createElement("div");
    row.className = "sess";
    const info = document.createElement("div");
    info.className = "grow";
    info.textContent = m.text;
    const del = document.createElement("button");
    del.className = "secondary";
    del.textContent = "Forget";
    del.onclick = async () => {
      await api("DELETE", "/memory/" + encodeURIComponent(m.id));
      loadMemory();
    };
    row.append(info, del);
    list.appendChild(row);
  });
}
async function addMemory(ev) {
  if (ev) ev.preventDefault();
  const text = $("memoryInput").value.trim();
  if (!text) return;
  try {
    await api("POST", "/memory", { body: { text } });
    $("memoryInput").value = "";
    setMsg("memoryMsg", "Saved.", false, true);
    loadMemory();
  } catch (e) {
    setMsg("memoryMsg", e.message, true);
  }
}

/* ---- command center: agent roster + status ---- */
function setLight(id, color, msg) {
  const el = $(id);
  if (!el) return;
  el.className = "light " + color;
  const m = $(id.replace("Light", "Msg"));
  if (m && msg != null) m.textContent = msg;
}

async function loadStats() {
  try { lastCost = await api("GET", "/cost"); } catch (e) {}
}

async function loadModels() {
  const sel = $("modelSelect");
  if (!sel) return;
  let models;
  try { models = (await api("GET", "/models")).models || []; } catch (e) { return; }
  sel.innerHTML = models
    .map((m) => `<option value="${m.id}">${m.label}</option>`)
    .join("");
  if (models.some((m) => m.id === selectedModel)) sel.value = selectedModel;
  else { selectedModel = "auto"; sel.value = "auto"; }
  sel.onchange = () => {
    selectedModel = sel.value;
    localStorage.setItem("agentmgr_model", selectedModel);
  };
}

let lastSecurity = null;
function secLabel(s) {
  return s.light === "unknown" ? "couldn’t check"
    : s.grade && s.grade !== "?" ? "grade " + s.grade : s.light;
}
async function loadSecurity() {
  try {
    const s = await api("GET", "/security/status");
    lastSecurity = s;
    setLight("secLight", s.light || "unknown", secLabel(s));
  } catch (e) {
    setLight("secLight", "unknown", "unavailable");
  }
}

async function openSecuritySheet(forceFresh) {
  $("securitySheet").classList.remove("hidden");
  const summary = $("secSummary");
  if (forceFresh || !lastSecurity) {
    summary.textContent = "Scanning…";
    $("secFindings").innerHTML = "";
    try {
      lastSecurity = await api("GET", "/security/status" + (forceFresh ? "?fresh=1" : ""));
      setLight("secLight", lastSecurity.light || "unknown", secLabel(lastSecurity));
    } catch (e) {
      summary.textContent = "Couldn’t scan: " + e.message;
      return;
    }
  }
  renderSecurity(lastSecurity);
}

function renderSecurity(s) {
  const summary = $("secSummary");
  const list = $("secFindings");
  list.innerHTML = "";
  if (!s || s.light === "unknown") {
    summary.textContent = "Security scan unavailable" + (s && s.error ? ": " + s.error : ".");
    return;
  }
  const c = s.counts || {};
  summary.textContent = "Grade " + (s.grade || "?") + " — " +
    (c.critical || 0) + " critical · " + (c.high || 0) + " high · " +
    (c.medium || 0) + " medium · " + (c.low || 0) + " low";
  const order = { critical: 0, high: 1, medium: 2, low: 3, ok: 4 };
  const defs = (s.findings || [])
    .filter((f) => f.severity !== "ok")
    .sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9));
  if (!defs.length) {
    const ok = document.createElement("p");
    ok.className = "muted small";
    ok.style.padding = "0 4px";
    ok.textContent = "No deficiencies found — all clear. ✅";
    list.appendChild(ok);
    return;
  }
  defs.forEach((f) => {
    const row = document.createElement("div");
    row.className = "finding sev-" + f.severity;
    const tag = document.createElement("span");
    tag.className = "sev";
    tag.textContent = (f.severity || "").toUpperCase();
    const body = document.createElement("div");
    body.className = "fbody";
    const title = document.createElement("div");
    title.className = "ftitle";
    title.textContent = f.title || "";
    const detail = document.createElement("div");
    detail.className = "fdetail";
    detail.textContent = f.detail || "";
    body.append(title, detail);
    if (f.fix) {
      const fix = document.createElement("div");
      fix.className = "ffix";
      fix.textContent = "Fix: " + f.fix;
      body.appendChild(fix);
    }
    row.append(tag, body);
    list.appendChild(row);
  });
}

/* ---- website health light (live site checks) ---- */
let lastSite = null;
let sitePollTimer = null;

function siteLabel(s) {
  if (!s || s.light === "unknown") return s && s.scanning ? "scanning…" : "unavailable";
  const g = s.grade && s.grade !== "?" ? "grade " + s.grade : s.light;
  return s.scanning ? g + " · rescanning…" : g;
}

async function loadWebsiteHealth() {
  try {
    const s = await api("GET", "/site-health/status");
    lastSite = s;
    setLight("siteLight", s.light || "unknown", siteLabel(s));
    // first cold scan reports unknown+scanning — poll so the light lands on a color
    if (s.scanning && s.light === "unknown") pollSiteUntilReady();
  } catch (e) {
    setLight("siteLight", "unknown", "unavailable");
  }
}

// One finding row (same shape the security sheet uses).
function findingRow(f) {
  const row = document.createElement("div");
  row.className = "finding sev-" + f.severity;
  const tag = document.createElement("span");
  tag.className = "sev";
  tag.textContent = (f.severity || "").toUpperCase();
  const body = document.createElement("div");
  body.className = "fbody";
  const title = document.createElement("div");
  title.className = "ftitle";
  title.textContent = f.title || "";
  const detail = document.createElement("div");
  detail.className = "fdetail";
  detail.textContent = f.detail || "";
  body.append(title, detail);
  if (f.fix) {
    const fix = document.createElement("div");
    fix.className = "ffix";
    fix.textContent = "Fix: " + f.fix;
    body.appendChild(fix);
  }
  row.append(tag, body);
  return row;
}

async function openSiteSheet(forceFresh) {
  $("siteSheet").classList.remove("hidden");
  try {
    lastSite = await api("GET", "/site-health/status" + (forceFresh ? "?fresh=1" : ""));
    setLight("siteLight", lastSite.light || "unknown", siteLabel(lastSite));
  } catch (e) {
    $("siteSummary").textContent = "Couldn’t check the site: " + e.message;
    $("siteFindings").innerHTML = "";
    return;
  }
  renderWebsiteHealth(lastSite);
  // cold cache or a forced re-scan runs in the background (~15s) — poll for it
  if (lastSite.scanning) pollSiteUntilReady();
}

function pollSiteUntilReady() {
  clearTimeout(sitePollTimer);
  const tick = async () => {
    let s;
    try { s = await api("GET", "/site-health/status"); } catch (e) { return; }
    lastSite = s;
    setLight("siteLight", s.light || "unknown", siteLabel(s));
    if (!$("siteSheet").classList.contains("hidden")) renderWebsiteHealth(s);
    if (s.scanning) sitePollTimer = setTimeout(tick, 2000);
  };
  sitePollTimer = setTimeout(tick, 2000);
}

function renderWebsiteHealth(s) {
  const summary = $("siteSummary");
  const list = $("siteFindings");
  list.innerHTML = "";
  if (!s || (s.light === "unknown" && !s.scanning)) {
    summary.textContent = "Website check unavailable" + (s && s.error ? ": " + s.error : ".");
    return;
  }
  if (s.scanning && !(s.findings || []).length) {
    summary.textContent = "Scanning the live site… (~15s — the sitemap is slow to generate)";
    return;
  }
  const c = s.counts || {};
  const issues = (c.critical || 0) + (c.high || 0) + (c.medium || 0) + (c.low || 0);
  const host = (s.checked || "").replace(/^https?:\/\//, "");
  summary.textContent = "Grade " + (s.grade || "?") + " — " +
    (issues ? (c.critical || 0) + " critical · " + (c.high || 0) + " high · " +
      (c.medium || 0) + " medium · " + (c.low || 0) + " low" : "all checks passing ✅") +
    (host ? " · " + host : "") + (s.scanning ? " · rescanning…" : "");
  const order = { critical: 0, high: 1, medium: 2, low: 3, ok: 4 };
  const all = s.findings || [];
  const issuesList = all.filter((f) => f.severity !== "ok")
    .sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9));
  if (!issuesList.length) {
    const ok = document.createElement("p");
    ok.className = "muted small";
    ok.style.padding = "0 4px";
    ok.textContent = "No issues found — the website is healthy. ✅";
    list.appendChild(ok);
  } else {
    issuesList.forEach((f) => list.appendChild(findingRow(f)));
  }
  // a quiet line listing what passed, for reassurance
  const passed = all.filter((f) => f.severity === "ok").map((f) => f.title);
  if (passed.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.style.padding = "8px 4px 0";
    p.textContent = "Passing: " + passed.join(" · ");
    list.appendChild(p);
  }
}

async function loadAgents() {
  let data;
  try {
    data = await api("GET", "/agents");
  } catch (e) {
    setLight("healthLight", "red", "Master unreachable");
    return;
  }
  renderAgents(data);
}

/* ---- command center: reports ---- */
async function loadReports() {
  let reports;
  try { reports = (await api("GET", "/reports")).reports || []; } catch (e) { return; }
  const list = $("reportList");
  list.innerHTML = "";
  if (!reports.length) {
    list.innerHTML = "<p class='muted small' style='padding:0 12px'>No reports yet.</p>";
    return;
  }
  reports.forEach((r) => {
    const link = document.createElement("a");
    link.className = "report-link";
    link.href = "#";
    const t = document.createElement("div");
    t.className = "rl-title";
    t.textContent = r.title;
    const m = document.createElement("div");
    m.className = "rl-meta";
    m.textContent = (r.source ? r.source + " · " : "") + r.updated_at;
    link.append(t, m);
    link.onclick = (e) => { e.preventDefault(); openReport(r.id, r.title); };
    list.appendChild(link);
  });
}

async function openReport(id, title) {
  try {
    const headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
    const res = await fetch("/reports/" + encodeURIComponent(id), { headers });
    if (res.status === 401) { logout(); throw new Error("session expired — sign in again"); }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const html = await res.text();
    const w = window.open("", "_blank");
    if (!w) { addBubble("sys", "Pop-up blocked — allow pop-ups to open reports."); return; }
    w.document.open();
    w.document.write(html);
    w.document.close();
    try { w.document.title = title || "Report"; } catch (e) {}
  } catch (e) {
    addBubble("sys", "Couldn’t open report: " + e.message);
  }
}

// Agents with a special primary verb; everything else's big button is chat.
const _SPECIAL_ACTION_AGENTS = ["mac-shell", "cloudrun-admin", "security-health"];
// True when the card's big button already opens the agent chat.
function cardActionIsChat(a) {
  return a.group !== "cloud" && !_SPECIAL_ACTION_AGENTS.includes(a.name);
}
function agentActionLabel(a) {
  if (a.name === "joegpt") return "📊 Chats & health";
  if (a.group === "cloud") return "▶ Run job";
  if (a.name === "mac-shell") return "⌨︎ Command";
  if (a.name === "cloudrun-admin") return "☁︎ List jobs";
  if (a.name === "security-health") return "🛡 Run scan";
  return "💬 Message";
}
function agentAction(a) {
  if (a.name === "joegpt") return openAgentInfo(a);   // chatbot monitor: health + Q&A
  if (a.group === "cloud") return runJobByName(a.job_name || a.name, a.name);
  if (a.name === "mac-shell") return focusChat("$ ");
  if (a.name === "cloudrun-admin") return quickSend("cloud run, list jobs");
  if (a.name === "security-health") return quickSend("run a security health check");
  return openAgentChat(a);   // "💬 Message" → scope the chat to this one agent
}

function renderAgents(data) {
  const agents = data.agents || [];
  agentsByName = {};
  agents.forEach((a) => (agentsByName[a.name] = a));
  $("agentCount").textContent = agents.length;
  const list = $("agentList");
  list.innerHTML = "";
  agents.forEach((a) => {
    const card = document.createElement("div");
    card.className = "agent-card";
    card.dataset.agent = a.name;
    if (runningAgents.has(a.name)) card.classList.add("running");

    const top = document.createElement("div");
    top.className = "ac-top";
    const dot = document.createElement("span");
    dot.className = "sdot " + a.status;
    dot.title = a.status;
    const name = document.createElement("span");
    name.className = "ac-name";
    name.textContent = a.title || a.name;
    name.title = "Click pencil or double-click to rename";
    name.ondblclick = (e) => { e.stopPropagation(); inlineRenameCard(name, a); };
    // ✎ pencil — discoverable rename affordance (same path as double-click)
    const edit = document.createElement("button");
    edit.className = "ac-edit";
    edit.title = "Rename this agent";
    edit.innerHTML = "&#9998;";
    edit.onclick = (e) => { e.stopPropagation(); inlineRenameCard(name, a); };
    const grp = document.createElement("span");
    grp.className = "grp";
    grp.textContent = a.group;
    // ⓘ info button — opens the full description
    const info = document.createElement("button");
    info.className = "ac-info";
    info.title = "What this agent does";
    info.innerHTML = "&#9432;";
    info.onclick = (e) => { e.stopPropagation(); openAgentInfo(a); };
    top.append(dot, name, edit, grp);
    // 💬 chat — only where the big button is something else (cloud run, shell,
    // …); agents whose primary action is already "Message" don't need it.
    if (!cardActionIsChat(a)) {
      const chat = document.createElement("button");
      chat.className = "ac-chat";
      chat.title = "Chat with this agent";
      chat.innerHTML = "&#128172;";
      chat.onclick = (e) => { e.stopPropagation(); openAgentChat(a); };
      top.append(chat);
    }
    top.append(info);

    const desc = document.createElement("div");
    desc.className = "ac-desc";
    desc.textContent = a.description || "";

    const meta = document.createElement("div");
    meta.className = "ac-meta";
    const caps = (a.capabilities || []).slice(0, 3).join(", ");
    meta.textContent = a.status + " · " + a.runtime + (caps ? " · " + caps : "");

    // progress bar — visible only while this agent has in-flight work
    const prog = document.createElement("div");
    prog.className = "ac-progress";
    prog.innerHTML = '<div class="ac-progress-bar"></div>';

    const act = document.createElement("button");
    act.className = "ac-act";
    act.textContent = agentActionLabel(a);
    act.onclick = () => agentAction(a);

    card.append(top);
    if (a.description) card.append(desc);
    card.append(meta, prog, act);
    list.appendChild(card);
  });
  renderStats(agents);
  highlightActiveCard();     // cards were rebuilt — restore the active marker
  updateChatContext();       // and refresh the peer's title if it changed
}

/* ---- progress bar: mark an agent busy while it has in-flight work ---- */
function setAgentRunning(name, on) {
  if (!name) return;
  if (on) runningAgents.add(name); else runningAgents.delete(name);
  const list = $("agentList");
  if (!list) return;
  const card = [...list.querySelectorAll(".agent-card")]
    .find((c) => c.dataset.agent === name);
  if (card) card.classList.toggle("running", on);
}

/* ---- rename an agent (shared by the card double-click + the info modal) ---- */
// PATCH the display title, update the local cache, refresh the rail. The
// canonical `name` (worker id) never changes — only the label the operator sees.
async function saveAgentRename(a, title) {
  const res = await api("PATCH", "/agents/" + encodeURIComponent(a.name),
    { body: { title } });
  const updated = Object.assign({}, a, { title: res.updated.title });
  agentsByName[a.name] = updated;
  addBubble("sys", "Renamed “" + (a.title || a.name) + "” → “" + title + "”.");
  await loadAgents();          // refresh the left rail with the new label
  return updated;
}

// Double-clicking a card's name swaps it for an inline input — Enter saves,
// Escape/blur cancels. Keeps rename right where the user reads the name.
function inlineRenameCard(nameEl, a) {
  const current = a.title || a.name;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "ac-name-input";
  input.value = current;
  input.maxLength = 60;
  nameEl.replaceWith(input);
  input.focus();
  input.setSelectionRange(0, current.length);
  let settled = false;
  const restore = () => { if (!settled) { settled = true; input.replaceWith(nameEl); } };
  input.onblur = restore;
  input.onkeydown = async (e) => {
    e.stopPropagation();
    if (e.key === "Escape") { restore(); return; }
    if (e.key !== "Enter") return;
    e.preventDefault();
    const title = input.value.trim();
    if (!title || title === current) { restore(); return; }
    settled = true;                 // stop blur from also firing
    input.disabled = true;
    try {
      await saveAgentRename(a, title);   // loadAgents() re-renders the rail
    } catch (err) {
      addBubble("sys", "Rename failed: " + err.message);
      input.replaceWith(nameEl);
    }
  };
}

/* ---- agent info modal: full description + details + inline rename ---- */
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Friendly labels for the right-side value cells.
function runtimeLabel(rt) {
  return ({
    "local-agent": "local-agent · on this Mac",
    "master-inline": "master-inline · runs in the Master",
    "cloudrun-job": "cloudrun-job · Cloud Run",
  })[rt] || rt || "—";
}
function statusLabel(s, group) {
  if (s === "online") return "online · ready";
  if (s === "ready") return "ready";
  if (s === "deployed") return "deployed";
  if (s === "offline") return group === "cloud" ? "offline · job not deployed" : "offline";
  if (s === "unknown") return "unknown · couldn't reach Cloud Run";
  return s || "—";
}

/* ---- JoeGPT customer chats (info sheet) ---- */
const _chatStyles = '<style>'
  + '.chat-row{display:flex;gap:10px;align-items:center;padding:8px 6px;border-top:1px solid rgba(255,255,255,.08);cursor:pointer}'
  + '.chat-row:hover{background:rgba(255,255,255,.05)}'
  + '.chat-when{font-size:12px;color:#8a96a3;min-width:62px}'
  + '.chat-meta{flex:1;font-size:13px}.chat-open{font-size:12px;color:#58a6ff}'
  + '.chat-actions{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}'
  + '.chat-actions button,.chat-actions a{font:inherit;font-size:13px;cursor:pointer;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.06);color:inherit;padding:5px 11px;border-radius:7px;text-decoration:none}'
  + '.chat-share{background:#1f8a8a!important;border-color:#1f8a8a!important;color:#fff!important}'
  + '.chat-transcript{max-height:360px;overflow-y:auto;padding-right:4px}'
  + '.ct-msg{margin:9px 0}.ct-who{font-size:11px;color:#8a96a3;margin-bottom:2px}'
  + '.ct-bub{padding:8px 11px;border-radius:10px;display:inline-block;max-width:90%;font-size:14px}'
  + '.ct-user{text-align:right}.ct-user .ct-bub{background:#1f8a8a;color:#fff}'
  + '.ct-assistant .ct-bub{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12)}'
  + '</style>';

function _shortUrl(u){ try { const x = new URL(u); return x.pathname === "/" ? x.hostname : x.pathname; } catch (e){ return u || ""; } }
function _mdLite(s){
  s = escapeHtml(s || "");
  s = s.replace(/!\[[^\]]*\]\([^)]+\)/g, "");
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/^#{1,6}\s*(.+)$/gm, "<b>$1</b>");
  return s.replace(/\n/g, "<br>");
}
function _chatRow(s){
  const when = timeAgo(s.last_active_at || s.created_at) || "";
  // The visitor's question is the useful label; fall back to a msg count.
  const meta = s.preview
    ? escapeHtml(s.preview)
    : ((s.total_messages || 0) + " msgs" + (s.page_url ? " · " + escapeHtml(_shortUrl(s.page_url)) : ""));
  return '<div class="chat-row" data-sid="' + escapeHtml(s.session_id) + '">'
    + '<span class="chat-when">' + escapeHtml(when) + '</span>'
    + '<span class="chat-meta">' + meta + '</span>'
    + '<span class="chat-open">View ›</span></div>';
}
async function loadAgentChats(){
  const box = document.getElementById("aiChats"); if (!box) return;
  box.innerHTML = '<span class="muted">Loading recent conversations…</span>';
  try {
    const d = await api("GET", "/agents/joegpt/chats?limit=25");
    const list = d.sessions || [];
    if (!list.length){ box.innerHTML = '<span class="muted">No customer conversations yet.</span>'; return; }
    box.innerHTML = list.map(_chatRow).join("");
    box.querySelectorAll(".chat-row").forEach((row) => {
      row.onclick = () => openChatTranscript(row.getAttribute("data-sid"));
    });
  } catch (e){ box.innerHTML = '<span class="ai-warn">Could not load chats: ' + escapeHtml(e.message) + '</span>'; }
}
async function openChatTranscript(sid){
  const box = document.getElementById("aiChats"); if (!box) return;
  box.innerHTML = '<span class="muted">Loading transcript…</span>';
  try {
    const d = await api("GET", "/agents/joegpt/chats/" + encodeURIComponent(sid));
    let h = '<div class="chat-actions"><button class="chat-back">‹ Back</button>';
    if (d.share_url){
      h += '<button class="chat-share" data-url="' + escapeHtml(d.share_url) + '">🔗 Copy share link</button>'
        +  '<a class="chat-openshare" href="' + escapeHtml(d.share_url) + '" target="_blank" rel="noopener">Open ↗</a>';
    }
    h += '</div><div class="chat-transcript">';
    h += ((d.messages || []).map((m) => {
      const who = m.role === "user" ? "Visitor" : "JoeGPT";
      return '<div class="ct-msg ct-' + escapeHtml(m.role) + '"><div class="ct-who">' + who + '</div>'
        + '<div class="ct-bub">' + _mdLite(m.content) + '</div></div>';
    }).join("")) || '<span class="muted">No messages.</span>';
    h += '</div>';
    box.innerHTML = h;
    const back = box.querySelector(".chat-back"); if (back) back.onclick = loadAgentChats;
    const sh = box.querySelector(".chat-share");
    if (sh) sh.onclick = () => {
      navigator.clipboard.writeText(sh.getAttribute("data-url")).then(() => {
        const o = sh.textContent; sh.textContent = "✓ Copied!"; setTimeout(() => { sh.textContent = o; }, 1500);
      }).catch(() => { sh.textContent = "Copy failed — select & copy manually"; });
    };
  } catch (e){
    box.innerHTML = '<span class="ai-warn">Could not load transcript: ' + escapeHtml(e.message) + '</span> '
      + '<button class="chat-back">‹ Back</button>';
    const back = box.querySelector(".chat-back"); if (back) back.onclick = loadAgentChats;
  }
}

function openAgentInfo(a) {
  renderAgentInfo(a);
  $("agentInfoSheet").classList.remove("hidden");
}

// A labeled section: <h4> + arbitrary HTML body. Returns "" when empty so we
// don't render blank headers for agents missing that detail.
function aiSection(label, bodyHtml) {
  if (!bodyHtml) return "";
  return '<section class="ai-sec"><h4 class="ai-sec-h">' + escapeHtml(label) +
    '</h4><div class="ai-sec-b">' + bodyHtml + "</div></section>";
}
// Render an array of strings as a bulleted list (escaped). "" if empty.
function aiList(items) {
  const arr = (items || []).filter((x) => x && String(x).trim());
  if (!arr.length) return "";
  return '<ul class="ai-ul">' +
    arr.map((x) => "<li>" + escapeHtml(x) + "</li>").join("") + "</ul>";
}

// Compact "3h ago" from an ISO-8601 timestamp (now_iso() on the backend).
function timeAgo(iso) {
  const t = Date.parse(iso || "");
  if (isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

// Plain-English health: live status pill + last actual RUN (from TaskResults)
// + last emailed/saved REPORT, whichever exist.
function healthHtml(a) {
  const cls = { online: "ok", ready: "ok", deployed: "ok",
                offline: "bad", unknown: "warn" }[a.status] || "warn";
  let html = '<span class="ai-health ' + cls + '">' +
    escapeHtml(statusLabel(a.status, a.group)) + "</span>";
  const lr = a.last_run || {};
  if (lr.status) {
    const when = timeAgo(lr.finished_at);
    html += '<div class="muted small ai-lastrep">Last run: ' +
      (lr.ok ? "✅ " : "❌ ") + escapeHtml(lr.status) +
      (when ? " · " + when : "") + "</div>";
  }
  if (a.last_report)
    html += '<div class="muted small ai-lastrep">Last report: ' +
      escapeHtml(a.last_report) + "</div>";
  if (!lr.status && !a.last_report)
    html += (a.group === "cloud" && a.status === "offline")
      ? '<div class="muted small ai-lastrep">Cloud Run job not deployed.</div>'
      : '<div class="muted small ai-lastrep">No runs recorded yet.</div>';
  return html;
}

// Render (or re-render) the info sheet body for agent `a`. Kept in its own
// function so the rename-save handler can rerun it after a successful PATCH.
function renderAgentInfo(a) {
  const display = a.title || a.name;
  // Header — title (display name) + small pencil that swaps to an input.
  $("aiName").innerHTML =
    '<span id="aiTitle" class="ai-title-text">' + escapeHtml(display) + "</span>" +
    ' <button id="aiRenameBtn" class="ai-rename-btn" title="Rename agent">&#9998;</button>' +
    ' <button id="aiEditBtn" class="ai-edit-details" title="Edit this info sheet">Edit details</button>';

  const d = a.details || {};
  const caps = a.capabilities || [];
  const capsHtml = caps.length
    ? '<div class="ai-chips">' +
        caps.map((c) => '<span class="ai-chip">' + escapeHtml(c) + "</span>").join("") +
      "</div>"
    : "";

  // Security: authored note + the sensitive flag, kept together.
  let secHtml = d.security ? "<p>" + escapeHtml(d.security) + "</p>" : "";
  secHtml += a.sensitive_default
    ? '<p class="ai-warn">⚠ Sensitive — needs your one-click approval to run.</p>'
    : (d.security ? "" : "<p>Not flagged sensitive.</p>");

  // Collapsible technical footer — the old identity rows, out of the way.
  const rows = [
    ["Runtime", escapeHtml(runtimeLabel(a.runtime))],
    ["Kind", escapeHtml(a.kind || "—")],
    ["Group", escapeHtml(a.group || "—")],
    ["Region", escapeHtml(a.region || "—")],
  ];
  if (a.job_name) rows.push(["Cloud job", escapeHtml(a.job_name)]);
  if (a.worker_module) rows.push(["Worker module", escapeHtml(a.worker_module)]);
  rows.push(["Identifier",
    '<code class="ai-id">' + escapeHtml(a.name) + "</code>" +
    (a.title && a.title !== a.name
      ? ' <span class="muted small">· stays the same after rename</span>'
      : "")]);
  const techRows = '<div class="ai-rows">' +
    rows.map(([k, v]) =>
      '<div class="ai-row"><span class="ai-k">' + k + "</span>" +
      '<span class="ai-v">' + v + "</span></div>").join("") + "</div>";

  // Summary always shows (falls back to the freeform description).
  const summary = d.summary || a.description || "No description provided.";

  $("aiBody").innerHTML =
    '<p class="ai-summary">' + escapeHtml(summary) + "</p>" +
    ((a.name === "joegpt")
      ? aiSection("Live health",
          '<div id="aiHealth" class="muted">Checking the chatbot…</div>') +
        aiSection("Customer chats",
          _chatStyles + '<div id="aiChats" class="muted">Loading recent conversations…</div>')
      : "") +
    aiSection("What it does", aiList(d.tasks)) +
    aiSection("Purpose", d.purpose ? "<p>" + escapeHtml(d.purpose) + "</p>" : "") +
    aiSection("Capabilities", capsHtml) +
    aiSection("Automation & schedule",
      d.automation ? "<p>" + escapeHtml(d.automation) + "</p>" : "") +
    aiSection("Who it can talk to", aiList(d.talks_to)) +
    aiSection("Health", healthHtml(a)) +
    aiSection("Security", secHtml) +
    aiSection("Recently added",
      (d.recent && d.recent.length)
        ? aiList(d.recent)
        : '<p class="muted">No recent changes recorded.</p>') +
    '<details class="ai-tech"><summary>Technical details</summary>' +
      techRows + "</details>" +
    '<p id="aiMsg" class="msg"></p>';

  $("aiRenameBtn").onclick = () => startRenameAgent(a);
  $("aiEditBtn").onclick = () => startEditDetails(a);
  if (a.name === "joegpt") { loadChatbotHealth(); loadAgentChats(); }
}

// Live stoplight for the chatbot — proxied JoeGPT /status.json (no Firestore).
async function loadChatbotHealth() {
  const el = $("aiHealth");
  if (!el) return;
  let d;
  try { d = await api("GET", "/agents/joegpt/health"); }
  catch (e) { el.innerHTML = '<span class="muted">Health check failed: ' + escapeHtml(e.message) + "</span>"; return; }
  if (!d.supported) { el.textContent = "n/a"; return; }
  const COLOR = { green: "#2ea043", yellow: "#d4a017", red: "#da3633" };
  const LVL = { ok: "#2ea043", warn: "#d4a017", error: "#da3633" };
  const LABEL = { green: "All systems operational", yellow: "Degraded — needs attention", red: "Critical issue" };
  const dot = (c) => '<span style="width:9px;height:9px;border-radius:50%;flex:0 0 auto;' +
    "margin-top:5px;display:inline-block;background:" + c + '"></span>';
  let html = '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">' +
    dot(COLOR[d.overall] || "#8b949e") +
    "<strong>" + escapeHtml(LABEL[d.overall] || d.overall || "unknown") + "</strong></div>";
  (d.checks || []).forEach((c) => {
    html += '<div style="display:flex;gap:8px;align-items:flex-start;margin:4px 0">' +
      dot(LVL[c.level] || "#8b949e") +
      "<div><div>" + escapeHtml(c.name || "") + "</div>" +
      '<div class="muted small">' + escapeHtml(c.detail || "") + "</div></div></div>";
  });
  if (!d.reachable) html += '<div class="muted small">' + escapeHtml(d.error || "unreachable") + "</div>";
  if (d.checked_at) html += '<div class="muted small" style="margin-top:6px">Checked ' +
    escapeHtml(d.checked_at) + "</div>";
  el.innerHTML = html;
}

// The editable fields of the info sheet. "lines" = one list item per line.
// `recent` is intentionally absent — Scout maintains it.
const _AI_EDIT_FIELDS = [
  { k: "summary", label: "Summary", type: "area", rows: 3 },
  { k: "tasks", label: "What it does (one per line)", type: "lines", rows: 4 },
  { k: "purpose", label: "Purpose", type: "area", rows: 2 },
  { k: "automation", label: "Automation & schedule", type: "area", rows: 2 },
  { k: "talks_to", label: "Who it can talk to (one per line)", type: "lines", rows: 3 },
  { k: "security", label: "Security", type: "area", rows: 3 },
];

// Swap the info-sheet body into an editor for the structured details. Save
// PATCHes only `details` (merged server-side, so `recent` is preserved).
function startEditDetails(a) {
  const d = a.details || {};
  const fieldHtml = (f) => {
    const val = f.type === "lines" ? (d[f.k] || []).join("\n") : (d[f.k] || "");
    return '<label class="ai-ed-l">' + escapeHtml(f.label) + "</label>" +
      '<textarea id="aied_' + f.k + '" class="ai-ed-ta" rows="' + f.rows + '">' +
      escapeHtml(val) + "</textarea>";
  };
  $("aiBody").innerHTML =
    '<div class="ai-ed">' + _AI_EDIT_FIELDS.map(fieldHtml).join("") +
    '<p class="muted small">“Recently added” is maintained automatically by Scout.</p>' +
    '<div class="ai-ed-actions">' +
      '<button id="aiEdSave" class="primary">Save</button> ' +
      '<button id="aiEdCancel" class="secondary">Cancel</button></div>' +
    '<p id="aiMsg" class="msg"></p></div>';
  $("aiEdCancel").onclick = () => renderAgentInfo(a);
  $("aiEdSave").onclick = async () => {
    const details = {};
    for (const f of _AI_EDIT_FIELDS) {
      const raw = $("aied_" + f.k).value;
      details[f.k] = f.type === "lines"
        ? raw.split("\n").map((s) => s.trim()).filter(Boolean)
        : raw.trim();
    }
    $("aiEdSave").disabled = true;
    try {
      renderAgentInfo(await saveAgentDetails(a, details));
    } catch (e) {
      setMsg("aiMsg", "Save failed: " + e.message, true);
      $("aiEdSave").disabled = false;
    }
  };
}

// PATCH the edited details, refresh the local cache + rail, return the merged
// agent so the sheet can re-render.
async function saveAgentDetails(a, details) {
  const res = await api("PATCH", "/agents/" + encodeURIComponent(a.name),
    { body: { details } });
  const updated = Object.assign({}, a, { details: res.updated.details });
  agentsByName[a.name] = updated;
  addBubble("sys", "Updated “" + (a.title || a.name) + "” info sheet.");
  await loadAgents();
  return updated;
}

function startRenameAgent(a) {
  const header = $("aiName");
  const current = a.title || a.name;
  header.innerHTML =
    '<input id="aiRenameInput" type="text" class="ai-rename-input" />' +
    ' <button id="aiRenameSave" class="primary ai-rename-save">Save</button>' +
    ' <button id="aiRenameCancel" class="secondary ai-rename-cancel">Cancel</button>';
  const input = $("aiRenameInput");
  input.value = current;
  input.maxLength = 60;
  input.focus();
  input.setSelectionRange(0, current.length);
  const cancel = () => renderAgentInfo(a);
  $("aiRenameCancel").onclick = cancel;
  input.onkeydown = (e) => {
    if (e.key === "Escape") cancel();
    if (e.key === "Enter") { e.preventDefault(); $("aiRenameSave").click(); }
  };
  $("aiRenameSave").onclick = async () => {
    const title = input.value.trim();
    if (!title) return setMsg("aiMsg", "Name can't be blank.", true);
    if (title === current) return cancel();
    $("aiRenameSave").disabled = true;
    try {
      renderAgentInfo(await saveAgentRename(a, title));  // re-render sheet with new buttons
    } catch (e) {
      setMsg("aiMsg", "Rename failed: " + e.message, true);
      $("aiRenameSave").disabled = false;
    }
  };
}

/* ---- relay graph: who talks to whom (through the Master) ---- */
// Every agent talks only to the Master; a few RELAY work to another agent
// through it. This draws those edges as an SVG node-link diagram.
function openRelayGraph() {
  renderRelayGraph();
  $("relaySheet").classList.remove("hidden");
}

function renderRelayGraph() {
  const agents = Object.values(agentsByName);
  const nodes = new Set();
  const edges = [];                 // {from, to}
  let anyNode = null;
  const watchers = [];
  agents.forEach((a) => {
    const d = a.details || {};
    if (d.relays && d.relays.length) {
      nodes.add(a.name);
      d.relays.forEach((t) => { nodes.add(t); edges.push({ from: a.name, to: t }); });
    }
    if (d.relays_any) { nodes.add(a.name); anyNode = a.name; }
    if (d.watches_all) { nodes.add(a.name); watchers.push(a.name); }
  });
  const names = [...nodes];
  const title = (n) => {
    const a = agentsByName[n];
    let t = a ? (a.title || a.name) : n;
    return t.length > 18 ? t.slice(0, 17) + "…" : t;
  };

  if (!names.length) {
    $("relayBody").innerHTML = '<p class="muted">No inter-agent relays configured.</p>';
    return;
  }

  const W = 460, H = 380, cx = W / 2, cy = 186, R = 128, MR = 26, NR = 7;
  const pos = {};
  names.forEach((n, i) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / names.length;
    pos[n] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang) };
  });

  const parts = [];
  // faint spoke from every participant to the Master hub
  names.forEach((n) => {
    parts.push('<line class="rg-spoke" x1="' + cx + '" y1="' + cy +
      '" x2="' + pos[n].x.toFixed(1) + '" y2="' + pos[n].y.toFixed(1) + '"/>');
  });
  // relays_any → dashed arrow to the hub, labelled "any"
  if (anyNode) {
    parts.push('<line class="rg-any" marker-end="url(#rgarrow)" x1="' +
      pos[anyNode].x.toFixed(1) + '" y1="' + pos[anyNode].y.toFixed(1) +
      '" x2="' + cx + '" y2="' + cy + '"/>');
  }
  // highlighted directed relay edges, curved toward the centre
  edges.forEach((e) => {
    const F = pos[e.from], T = pos[e.to];
    const mx = (F.x + T.x) / 2 + (cx - (F.x + T.x) / 2) * 0.35;
    const my = (F.y + T.y) / 2 + (cy - (F.y + T.y) / 2) * 0.35;
    // stop short of the target node so the arrowhead sits just outside it
    const ang = Math.atan2(T.y - my, T.x - mx);
    const ex = T.x - (NR + 5) * Math.cos(ang), ey = T.y - (NR + 5) * Math.sin(ang);
    parts.push('<path class="rg-edge" marker-end="url(#rgarrow)" d="M' +
      F.x.toFixed(1) + ',' + F.y.toFixed(1) + ' Q' + mx.toFixed(1) + ',' +
      my.toFixed(1) + ' ' + ex.toFixed(1) + ',' + ey.toFixed(1) + '"/>');
  });
  // Master hub
  parts.push('<circle class="rg-hub" cx="' + cx + '" cy="' + cy + '" r="' + MR + '"/>' +
    '<text class="rg-hub-t" x="' + cx + '" y="' + (cy + 4) + '">Master</text>');
  // participant nodes + labels
  names.forEach((n) => {
    const p = pos[n];
    const cls = watchers.includes(n) ? "rg-node rg-watch" : "rg-node";
    const below = p.y >= cy;
    parts.push('<circle class="' + cls + '" cx="' + p.x.toFixed(1) + '" cy="' +
      p.y.toFixed(1) + '" r="' + NR + '"/>');
    parts.push('<text class="rg-label" x="' + p.x.toFixed(1) + '" y="' +
      (p.y + (below ? 20 : -12)).toFixed(1) + '">' + escapeHtml(title(n)) + "</text>");
  });

  const legend = [];
  if (anyNode) legend.push(escapeHtml(title(anyNode)) + " can relay to <b>any</b> agent.");
  watchers.forEach((w) => legend.push(escapeHtml(title(w)) + " <b>watches all</b> agents (advisory)."));

  $("relayBody").innerHTML =
    '<p class="muted small rg-caption">All ' + agents.length +
      " agents route through the Master. Highlighted arrows are agent→agent relays.</p>" +
    '<svg viewBox="0 0 ' + W + " " + H + '" class="rg-svg" role="img" aria-label="relay graph">' +
      '<defs><marker id="rgarrow" markerWidth="8" markerHeight="8" refX="6" refY="3" ' +
        'orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="rg-arrowhead"/></marker></defs>' +
      parts.join("") +
    "</svg>" +
    (legend.length ? '<p class="muted small rg-legend">' + legend.join("<br>") + "</p>" : "");
}

/* ---- in-app Claude Code console (runs `claude -p` via mac-shell) ---- */
let termBusy = false;
// One persistent Claude Code session per page load; each typed line continues it.
let claudeSession = (crypto.randomUUID ? crypto.randomUUID()
  : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    }));
let claudeFresh = true;
function newClaudeSession() {
  claudeSession = crypto.randomUUID ? crypto.randomUUID() : claudeSession + "x";
  claudeFresh = true;
}
function shq(s) { return "'" + s.replace(/'/g, "'\\''") + "'"; }
function toggleTerminal(force) {
  const consolePane = document.querySelector(".pane.console");
  const pane = $("terminalPane");
  const open = force !== undefined ? force : pane.classList.contains("hidden");
  pane.classList.toggle("hidden", !open);
  consolePane.classList.toggle("term-open", open);
  $("terminalToggle").classList.toggle("active", open);
  if (open) setTimeout(() => $("termInput").focus(), 50);
}
function termWrite(text, cls) {
  const line = document.createElement("div");
  line.className = "term-line" + (cls ? " " + cls : "");
  line.textContent = text;
  $("termOutput").appendChild(line);
  $("termOutput").scrollTop = $("termOutput").scrollHeight;
}
// Render one streamed line. Raw shell output prints verbatim; Claude output is
// NDJSON, so each line is parsed into a friendly progress line.
// Returns the number of visible lines written (so the caller can keep the
// "working…" spinner up until the first real line appears).
function renderTermLine(line, isClaude) {
  line = line.replace(/\r$/, "");
  if (!isClaude) { termWrite(line); return 1; }
  if (!line.trim()) return 0;
  let ev;
  try { ev = JSON.parse(line); }
  catch (e) { termWrite(line, "muted"); return 1; }   // stray warning on stderr
  return renderClaudeEvent(ev);
}

// One-line summary of a tool call, e.g. "Edit  header.html" / "Bash  npm run build".
function summarizeTool(name, input) {
  input = input || {};
  const base = (p) => (p ? String(p).split("/").pop() : "");
  switch (name) {
    case "Read": case "Write": case "Edit": case "NotebookEdit":
      return name + "  " + base(input.file_path || input.notebook_path);
    case "Bash": return "Bash  " + String(input.command || "").replace(/\s+/g, " ").slice(0, 90);
    case "Grep": return "Grep  " + (input.pattern || "");
    case "Glob": return "Glob  " + (input.pattern || "");
    case "Task": return "Task  " + (input.description || input.subagent_type || "");
    case "WebFetch": return "WebFetch  " + (input.url || "");
    case "WebSearch": return "WebSearch  " + (input.query || "");
    default: return name;
  }
}

// Map a stream-json event to terminal lines. system/init and tool_result
// events are suppressed as noise; assistant text + tool calls + the final
// result are what the operator wants to watch.
function renderClaudeEvent(ev) {
  if (!ev || !ev.type) return 0;
  let n = 0;
  if (ev.type === "assistant" && ev.message && Array.isArray(ev.message.content)) {
    for (const b of ev.message.content) {
      if (b.type === "text" && b.text && b.text.trim()) { termWrite(b.text.trim()); n++; }
      else if (b.type === "tool_use") { termWrite("● " + summarizeTool(b.name, b.input), "term-tool"); n++; }
    }
  } else if (ev.type === "result") {
    if (ev.is_error) { termWrite("✗ " + (ev.result || ev.error || "error"), "term-err"); n++; }
    else {
      if (ev.result && ev.result.trim()) { termWrite(ev.result.trim()); n++; }
      const secs = ev.duration_ms ? " · " + (ev.duration_ms / 1000).toFixed(1) + "s" : "";
      termWrite("✔ done" + secs, "muted"); n++;
    }
  }
  return n;
}

async function termRun(ev) {
  if (ev) ev.preventDefault();
  if (termBusy) return;
  const input = $("termInput").value.trim();
  if (!input) return;
  $("termInput").value = "";
  let command;
  if (input.startsWith("!")) {            // power escape: run a raw shell command
    command = input.slice(1).trim();
    if (!command) { termBusy = false; return; }
    termWrite("$ " + command, "term-cmd");
  } else {                                 // default: talk to Claude Code
    const flag = claudeFresh
      ? "--session-id " + claudeSession
      : "--resume " + claudeSession;
    claudeFresh = false;
    // Full autonomy, matching the operator's desktop terminal (~/.claude
    // settings.json runs auto-mode + skip-dangerous-prompt). bypassPermissions
    // is the headless equivalent: no prompts, any Bash, write anywhere — so the
    // in-app console isn't sandboxed to agent-manager/ like acceptEdits was.
    // stream-json (needs --verbose) emits one NDJSON event per step so the UI
    // can show the work — file reads, edits, bash — live as Claude does it.
    command = "claude -p --permission-mode bypassPermissions"
      + " --output-format stream-json --verbose " + flag + " " + shq(input);
    termWrite("▸ " + input, "term-cmd");
  }
  termBusy = true;
  setAgentRunning("mac-shell", true);
  const pending = document.createElement("div");
  pending.className = "term-line muted";
  pending.textContent = "⏳ Claude Code is working…";
  $("termOutput").appendChild(pending);
  try {
    // Start in the operator's home dir so the console behaves like a fresh
    // desktop terminal (the login shell expands ~). Together with
    // bypassPermissions above, the session can cd/reach any repo — it's no
    // longer pinned to agent-manager/. A raw `!cd …` the user types still wins.
    const execCommand = "cd ~ && " + command;
    const { task_id } = await api("POST", "/terminal/exec",
      { body: { command: execCommand, stream: true } });
    // Poll the live stream fast and render only the new tail each time. Claude
    // output is NDJSON (one event/line) → parsed into progress; a raw `!`
    // command is plain text → printed line-by-line.
    const isClaude = !input.startsWith("!");
    let consumed = 0;        // chars already pulled from the stream
    let buffer = "";         // leftover partial line
    let rendered = 0;        // visible lines written so far
    const flushLines = (final) => {
      let wrote = 0, nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        wrote += renderTermLine(buffer.slice(0, nl), isClaude);
        buffer = buffer.slice(nl + 1);
      }
      if (final && buffer.trim()) { wrote += renderTermLine(buffer, isClaude); buffer = ""; }
      // Keep the "working…" spinner until the first *visible* line — Claude's
      // big init/rate-limit events are skipped, so don't drop it on those.
      if (wrote && pending.parentNode) pending.remove();
      rendered += wrote;
    };
    const started = Date.now();
    while (Date.now() - started < 15 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 400));
      let r;
      try { r = await api("GET", "/terminal/stream/" + encodeURIComponent(task_id)); }
      catch (e) { continue; }                 // transient — keep polling
      const out = r.output || "";
      if (out.length > consumed) {
        buffer += out.slice(consumed);
        consumed = out.length;
        flushLines(false);
      }
      if (r.done) {
        flushLines(true);
        if (pending.parentNode) pending.remove();
        if (r.status === "FAILED" && r.error) termWrite("✗ " + r.error, "term-err");
        else if (rendered === 0) termWrite("(no output)", "muted");
        return;
      }
    }
    pending.textContent = "still running… (is the Mac agent up?)";
  } catch (e) {
    pending.remove();
    termWrite("✗ " + e.message, "term-err");
  } finally {
    termBusy = false;
    setAgentRunning("mac-shell", false);
  }
}

function renderStats(agents) {
  const live = (s) => ["online", "ready", "deployed"].includes(s);
  const online = agents.filter((a) => live(a.status)).length;
  const cloud = agents.filter((a) => a.group === "cloud").length;
  const local = agents.filter((a) => a.group === "local").length;
  $("masterStatus").innerHTML =
    '<span class="dot"></span> Master online · ' + online + "/" + agents.length + " agents";
  const spend = lastCost ? "$" + (lastCost.total_llm_cost_usd || 0).toFixed(2) : "—";
  const tiles = [
    ["Online", online + "/" + agents.length],
    ["Cloud", cloud],
    ["Local", local],
    ["Spend", spend],
  ];
  $("statTiles").innerHTML = tiles
    .map((t) => `<div class="stat-tile"><div class="v">${t[1]}</div><div class="k">${t[0]}</div></div>`)
    .join("");

  // health light: red if a local agent is down, yellow if cloud status is
  // unknown, green otherwise. (Undeployed scaffolding doesn't drag it down.)
  const localDown = agents.filter((a) => a.group === "local" && a.status !== "online").length;
  const unknown = agents.filter((a) => a.status === "unknown").length;
  if (localDown) setLight("healthLight", "red", localDown + " local agent(s) down");
  else if (unknown) setLight("healthLight", "yellow", "cloud status unknown");
  else setLight("healthLight", "green", "all systems operational");
}

/* ---- command center: actions ---- */
function focusChat(prefix) {
  const i = $("chatInput");
  if (prefix) i.value = prefix;
  i.focus();
}
function quickSend(text) {
  $("chatInput").value = text;
  sendMessage({ preventDefault() {} });
}
async function runJobByName(name, agentName) {
  if (!name) return;
  addBubble("sys", "Starting job “" + name + "”…");
  if (agentName) setAgentRunning(agentName, true);
  try {
    await api("POST", "/cloudrun/jobs/" + encodeURIComponent(name) + "/run");
    addBubble("sys", "Started “" + name + "”.");
    // The job runs in the cloud (completion isn't polled here) — show the bar
    // briefly as launch feedback.
    if (agentName) setTimeout(() => setAgentRunning(agentName, false), 6000);
  } catch (e) {
    addBubble("sys", "Couldn’t run “" + name + "”: " + e.message);
    if (agentName) setAgentRunning(agentName, false);
  }
}

/* ---- command center: create agent ---- */
function openNewAgent() {
  setMsg("newAgentMsg", "");
  $("newAgentSheet").classList.remove("hidden");
}
async function submitNewAgent(ev) {
  if (ev) ev.preventDefault();
  const name = $("naName").value.trim();
  if (!name) return setMsg("newAgentMsg", "Name is required.", true);
  const body = {
    name,
    runtime: $("naRuntime").value,
    kind: $("naKind").value.trim(),
    job_name: $("naJob").value.trim(),
    capabilities: $("naCaps").value.split(",").map((s) => s.trim()).filter(Boolean),
    description: $("naDesc").value.trim(),
    sensitive_default: $("naSensitive").checked,
  };
  try {
    await api("POST", "/agents", { body });
    setMsg("newAgentMsg", "Created “" + name + "”.", false, true);
    addBubble("sys", "New agent “" + name + "” registered.");
    ["naName", "naKind", "naJob", "naCaps", "naDesc"].forEach((id) => ($(id).value = ""));
    $("naSensitive").checked = false;
    loadAgents();
    setTimeout(() => $("newAgentSheet").classList.add("hidden"), 900);
  } catch (e) {
    setMsg("newAgentMsg", e.message, true);
  }
}

/* ---- website (jazzysphotos) actions ---- */
let webBusy = false;

function openWebsite() {
  setMsg("webMsg", "");
  $("webOutput").textContent = "Pick an action above to begin.";
  $("websiteSheet").classList.remove("hidden");
}

// Upload one file via /upload and return its on-disk path (so the Mac agent,
// which shares this machine, can read it for add_photo).
async function uploadOneFile(file) {
  const fd = new FormData();
  fd.append("files", file);
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch("/upload", { method: "POST", headers, body: fd });
  if (res.status === 401) { logout(); throw new Error("session expired — sign in again"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  const refs = (await res.json()).attachments || [];
  if (!refs.length) throw new Error("upload returned no file");
  return refs[0].path;
}

function renderWebResult(r) {
  const o = r.output || {};
  if (r.status === "FAILED") return "✗ " + (r.error || "failed");
  const parts = [];
  if (o.answer) parts.push(o.answer);            // agentic goal
  if (o.note) parts.push(o.note);                // publish/build summaries
  if (o.published) parts.push("Published ✓");
  // shell-style output (status / build / refresh)
  if (o.stdout && !o.answer) parts.push(o.stdout.trim());
  if (o.stderr && o.exit_code !== 0) parts.push(o.stderr.trim());
  if (o.hit_limit) parts.push("(stopped at the step limit — ask again to continue.)");
  return "✓ " + (parts.join("\n\n").trim() || "Done.");
}

// Dispatch a website action, then poll for its result. Publishing actions block
// in the agent on the approval gate, so the approval banner may pop up — approve
// it with Face ID and this keeps polling until the action finishes.
async function runWebAction(payload, label) {
  if (webBusy) return;
  webBusy = true;
  setAgentRunning("jazzysphotos-site", true);
  setMsg("webMsg", "");
  const out = $("webOutput");
  out.classList.add("muted");
  out.textContent = "⏳ " + (label || "Working") + "… (approve in the banner if asked)";
  try {
    const { task_id } = await api("POST", "/website/dispatch", { body: payload });
    const started = Date.now();
    while (Date.now() - started < 15 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 1500));
      const r = await api("GET", "/website/result/" + encodeURIComponent(task_id));
      if (r.ready) {
        out.classList.toggle("muted", false);
        out.textContent = renderWebResult(r);
        addBubble("sys", "Website · " + (label || payload.action) + " — " +
          (r.status === "FAILED" ? "failed" : "done") + ".");
        return;
      }
    }
    out.textContent = "Still running — check back from the chat. (Is the Mac agent up?)";
  } catch (e) {
    out.classList.toggle("muted", false);
    out.textContent = "✗ " + e.message;
  } finally {
    webBusy = false;
    setAgentRunning("jazzysphotos-site", false);
  }
}

async function webAddPhoto() {
  const file = $("webPhotoFile").files[0];
  const title = $("webPhotoTitle").value.trim();
  const alt = $("webPhotoAlt").value.trim();
  if (!file) return setMsg("webMsg", "Choose an image first.", true);
  if (!title || !alt) return setMsg("webMsg", "Title and alt text are required.", true);
  setMsg("webMsg", "Uploading image…");
  let image_path;
  try { image_path = await uploadOneFile(file); }
  catch (e) { return setMsg("webMsg", "Upload failed: " + e.message, true); }
  setMsg("webMsg", "");
  runWebAction({
    action: "add_photo", image_path, title, alt,
    category: $("webPhotoCat").value,
    featured: $("webPhotoFeatured").checked,
    order: Number($("webPhotoOrder").value) || 99,
  }, "Add photo");
}

function wireWebsite() {
  $("websiteBtn").onclick = openWebsite;
  $("closeWebsite").onclick = () => $("websiteSheet").classList.add("hidden");
  document.querySelectorAll("[data-web]").forEach((btn) => {
    btn.onclick = () => runWebAction({ action: btn.dataset.web }, btn.textContent);
  });
  $("webUpdateCopy").onclick = () => {
    const value = $("webCopyValue").value.trim();
    if (!value) return setMsg("webMsg", "Type the new text first.", true);
    setMsg("webMsg", "");
    runWebAction({ action: "update_copy", field: $("webCopyField").value, value },
      "Update " + $("webCopyField").value);
  };
  $("webAddPhoto").onclick = webAddPhoto;
  $("webRemovePhoto").onclick = () => {
    const slug = $("webRemoveSlug").value.trim();
    if (!slug) return setMsg("webMsg", "Enter the photo slug first.", true);
    setMsg("webMsg", "");
    runWebAction({ action: "remove_photo", slug }, "Remove photo");
  };
  $("webPublish").onclick = () =>
    runWebAction({ action: "publish", message: $("webPublishMsg").value.trim() }, "Publish");
  $("webGoalBtn").onclick = () => {
    const goal = $("webGoal").value.trim();
    if (!goal) return setMsg("webMsg", "Describe the change first.", true);
    setMsg("webMsg", "");
    runWebAction({ action: "goal", goal }, "Site change");
  };
}

/* ---- misc ---- */
function setMsg(id, text, isErr, isOk) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg" + (isErr ? " err" : isOk ? " ok" : "");
}

/* ---- init ---- */
function init() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  $("loginBtn").onclick = login;
  $("passwordForm").onsubmit = passwordLogin;
  $("registerBtn").onclick = register;
  $("logoutBtn").onclick = logout;
  $("chatForm").onsubmit = sendMessage;
  $("attachBtn").onclick = () => $("fileInput").click();
  $("fileInput").onchange = (e) => {
    addFiles(e.target.files);
    e.target.value = "";
  };
  setupDropAndPaste();

  // command center
  $("refreshAgents").onclick = loadAgents;
  $("chatBack").onclick = () => switchTarget(null);   // back to the Manager
  $("refreshReports").onclick = loadReports;
  $("secRow").onclick = () => openSecuritySheet(false);
  $("closeSecurity").onclick = () => $("securitySheet").classList.add("hidden");
  $("refreshSecurity").onclick = () => openSecuritySheet(true);
  $("siteRow").onclick = () => openSiteSheet(false);
  $("closeSite").onclick = () => { $("siteSheet").classList.add("hidden"); clearTimeout(sitePollTimer); };
  $("refreshSite").onclick = () => openSiteSheet(true);
  $("newAgentBtn").onclick = openNewAgent;
  $("closeNewAgent").onclick = () => $("newAgentSheet").classList.add("hidden");
  $("newAgentForm").onsubmit = submitNewAgent;
  $("uploadBtn").onclick = () => $("fileInput").click();
  $("rosterToggle").onclick = () => $("rosterPane").classList.toggle("collapsed");
  $("claudeBtn").onclick = () => {
    addBubble("sys", "Claude console — type a goal and it runs on your Mac. Read-only commands run automatically; anything else asks for approval.");
    focusChat("");
  };
  $("sessionsBtn").onclick = () => {
    $("sessionsSheet").classList.remove("hidden");
    refreshSessions();
  };
  $("closeSessions").onclick = () =>
    $("sessionsSheet").classList.add("hidden");
  $("startSession").onclick = startSession;
  $("jobsBtn").onclick = () => {
    $("jobsSheet").classList.remove("hidden");
    loadJobs();
  };
  $("closeJobs").onclick = () => $("jobsSheet").classList.add("hidden");
  $("runJobBtn").onclick = runSelectedJob;
  $("memoryBtn").onclick = () => {
    $("memorySheet").classList.remove("hidden");
    setMsg("memoryMsg", "");
    loadMemory();
  };
  $("closeMemory").onclick = () => $("memorySheet").classList.add("hidden");
  $("memoryForm").onsubmit = addMemory;
  wireWebsite();

  // in-app terminal
  $("terminalToggle").onclick = () => toggleTerminal();
  $("termClose").onclick = () => toggleTerminal(false);
  $("termClear").onclick = () => { $("termOutput").innerHTML = ""; newClaudeSession(); };
  $("termForm").onsubmit = termRun;
  // default to the side-by-side split on desktop; collapsed on narrow screens
  if (window.matchMedia("(min-width: 861px)").matches) toggleTerminal(true);
  // agent info modal
  $("closeAgentInfo").onclick = () => $("agentInfoSheet").classList.add("hidden");
  // relay graph
  $("relayBtn").onclick = openRelayGraph;
  $("closeRelay").onclick = () => $("relaySheet").classList.add("hidden");

  if (!window.PublicKeyCredential) {
    setMsg("loginMsg", "This browser has no passkey support.", true);
  }
  if (token) showApp();
  else showLogin();
}
document.addEventListener("DOMContentLoaded", init);
