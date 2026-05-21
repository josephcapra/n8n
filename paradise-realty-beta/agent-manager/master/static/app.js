"use strict";
/* Agent-Manager PWA — chat client + WebAuthn passkey (Face ID). */

const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("agentmgr_token") || "";
let conversationId = null;
let approvalsTimer = null;

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
}
function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("messages").innerHTML = "";
  addBubble("sys", "Signed in. Message the manager — try “$ git status” or “cloud run, list jobs”.");
  refreshApprovals();
  approvalsTimer = setInterval(refreshApprovals, 6000);
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

/* ---- chat ---- */
function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  div.textContent = text;
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
}
async function sendMessage(ev) {
  ev.preventDefault();
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addBubble("user", text);
  try {
    const res = await api("POST", "/chat", {
      body: { message: text, conversation_id: conversationId },
    });
    conversationId = res.conversation_id;
    addBubble("master", res.reply);
  } catch (e) {
    addBubble("sys", "Error: " + e.message);
  }
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
