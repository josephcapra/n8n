"use strict";
/* Agent-Manager PWA — chat client + WebAuthn passkey (Face ID). */

const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("agentmgr_token") || "";
let conversationId = null;
let approvalsTimer = null;
let agentsTimer = null;
let lastCost = null;

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
  $("messages").innerHTML = "";
  addBubble("sys", "Command center online. Message the manager, or pick an agent on the left.");
  refreshApprovals();
  approvalsTimer = setInterval(refreshApprovals, 6000);
  loadStats().then(loadAgents);
  agentsTimer = setInterval(() => loadStats().then(loadAgents), 12000);
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

function addBubble(role, text, atts) {
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

  try {
    const res = await api("POST", "/chat", {
      body: { message: text, conversation_id: conversationId, attachments: refs },
    });
    conversationId = res.conversation_id;
    addBubble("master", res.reply);
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
async function loadStats() {
  try { lastCost = await api("GET", "/cost"); } catch (e) {}
}

async function loadAgents() {
  let data;
  try { data = await api("GET", "/agents"); } catch (e) { return; }
  renderAgents(data);
}

function agentActionLabel(a) {
  if (a.group === "cloud") return "▶ Run job";
  if (a.name === "mac-shell") return "⌨︎ Command";
  if (a.name === "cloudrun-admin") return "☁︎ List jobs";
  if (a.name === "security-health") return "🛡 Run scan";
  return "💬 Message";
}
function agentAction(a) {
  if (a.group === "cloud") return runJobByName(a.job_name || a.name);
  if (a.name === "mac-shell") return focusChat("$ ");
  if (a.name === "cloudrun-admin") return quickSend("cloud run, list jobs");
  if (a.name === "security-health") return quickSend("run a security health check");
  return focusChat("");
}

function renderAgents(data) {
  const agents = data.agents || [];
  $("agentCount").textContent = agents.length;
  const list = $("agentList");
  list.innerHTML = "";
  agents.forEach((a) => {
    const card = document.createElement("div");
    card.className = "agent-card";

    const top = document.createElement("div");
    top.className = "ac-top";
    const dot = document.createElement("span");
    dot.className = "sdot " + a.status;
    dot.title = a.status;
    const name = document.createElement("span");
    name.className = "ac-name";
    name.textContent = a.name;
    const grp = document.createElement("span");
    grp.className = "grp";
    grp.textContent = a.group;
    top.append(dot, name, grp);

    const meta = document.createElement("div");
    meta.className = "ac-meta";
    const caps = (a.capabilities || []).slice(0, 3).join(", ");
    meta.textContent = a.status + " · " + a.runtime + (caps ? " · " + caps : "");

    const act = document.createElement("button");
    act.className = "ac-act";
    act.textContent = agentActionLabel(a);
    act.onclick = () => agentAction(a);

    card.append(top, meta, act);
    list.appendChild(card);
  });
  renderStats(agents);
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
async function runJobByName(name) {
  if (!name) return;
  addBubble("sys", "Starting job “" + name + "”…");
  try {
    await api("POST", "/cloudrun/jobs/" + encodeURIComponent(name) + "/run");
    addBubble("sys", "Started “" + name + "”.");
  } catch (e) {
    addBubble("sys", "Couldn’t run “" + name + "”: " + e.message);
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

  if (!window.PublicKeyCredential) {
    setMsg("loginMsg", "This browser has no passkey support.", true);
  }
  if (token) showApp();
  else showLogin();
}
document.addEventListener("DOMContentLoaded", init);
