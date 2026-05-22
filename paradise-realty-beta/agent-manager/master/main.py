"""Master Agent FastAPI service.

Phase 2 — routing + the message hub:
  * a command is decomposed by a swappable :class:`Planner` into subtasks,
  * each subtask is routed to the right agent by the registry,
  * the inter-agent message bus is wired: a worker can emit a message that the
    Master relays on to another worker (workers never call each other),
  * SENSITIVE Cloud Run subtasks are routed through the approval gate BEFORE
    the Job is triggered,
  * BOTH bounds are enforced on every hop — job fan-out (`check_task`) AND
    relay depth (`check_relay`) — so A->Master->B->Master->A cannot loop.

Authentication: Cloud Run IAM + an app-level token (static admin token or a
short-lived PWA token from a passkey login). Fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import re
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agentmgr import __version__
from agentmgr.approval_gate import (
    ApprovalDenied,
    ApprovalGate,
    ApprovalNotProvisioned,
    canonical_message,
)
from agentmgr import connectors as connectors_registry
from agentmgr.cloudrun_admin import MANAGE_OPS
from agentmgr.config import Config, load_config
from agentmgr.fanout_guard import FanoutGuard, FanoutLimitExceeded
from agentmgr.job_runner import JobRunner, make_job_runner
from agentmgr.logging_utils import get_logger, set_correlation_id
from agentmgr.passkey import PasskeyService, challenge_for, verify_assertion
from agentmgr.llm import make_llm_router
from agentmgr.memory import MemoryStore
from agentmgr.planner import Plan, make_planner
from agentmgr.registry import AgentRegistry, AgentSpec
from agentmgr.schemas import (
    ApprovalResponse,
    ChatRequest,
    ChatResponse,
    ConversationTurn,
    Message,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from agentmgr.session import (
    SessionManager,
    canonical_session_message,
    new_grant,
    sign_grant_with_token,
)
from agentmgr.state_store import StateStore, make_state_store
from agentmgr.util import TimeoutExceeded, gen_id, poll_until

log = get_logger("agentmgr.master")
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Chat attachments are saved here so the local assistant (which runs shell
# commands on this same Mac) can read them by path; images are also shown to
# the model directly. See /upload and agentmgr.assistant.build_user_content.
_UPLOAD_DIR = Path.home() / ".agentmgr-uploads"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_UPLOAD_FILES = 10
_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


# --- PWA bearer tokens (HMAC over expiry; no storage needed) -------------

def mint_pwa_token(secret: str, ttl_s: float) -> str:
    exp = str(int(time.time() + ttl_s))
    sig = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}.{sig}".encode()).decode()


def check_pwa_token(secret: str, token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token).decode()
        exp, sig = raw.split(".", 1)
        expected = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and time.time() < int(exp)
    except Exception:  # noqa: BLE001
        return False


# --- application factory -------------------------------------------------

def build_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    store: StateStore = make_state_store(cfg)
    runner: JobRunner = make_job_runner(cfg, store)
    registry = AgentRegistry.load()
    guard = FanoutGuard(cfg, store)
    gate = ApprovalGate(
        store,
        public_key_b64=cfg.approval_public_key,
        channel=cfg.approval_channel,
        poll_interval_s=cfg.poll_interval_s,
        spend_threshold_usd=cfg.spend_threshold_usd,
        rp_id=cfg.rp_id,
        origin=cfg.origin,
    )
    session_mgr = SessionManager(
        store, cfg.approval_public_key, cfg.always_confirm_patterns,
        rp_id=cfg.rp_id, origin=cfg.origin, api_token=cfg.api_token,
    )
    passkey_svc = PasskeyService(cfg, store)
    llm_router = make_llm_router(cfg, store, gate)
    planner = make_planner(cfg, router=llm_router)
    memory = MemoryStore(cfg.memory_path)

    app = FastAPI(title="agentmgr-master", version=__version__)
    app.state.config = cfg
    app.state.store = store
    app.state.runner = runner
    app.state.registry = registry
    app.state.guard = guard
    app.state.gate = gate
    app.state.session_mgr = session_mgr
    app.state.passkey = passkey_svc
    app.state.planner = planner
    app.state.llm_router = llm_router
    app.state.memory = memory
    app.state.cloudrun_admin = None

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- auth dependencies ----------------------------------------------
    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not cfg.api_token:
            log.error("AGENTMGR_API_TOKEN not provisioned; refusing request")
            raise HTTPException(status_code=503, detail="auth not provisioned")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="unauthorized")
        token = authorization[7:]
        if hmac.compare_digest(token, cfg.api_token) or check_pwa_token(
            cfg.api_token, token
        ):
            return
        raise HTTPException(status_code=401, detail="unauthorized")

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        if not cfg.api_token:
            raise HTTPException(status_code=503, detail="auth not provisioned")
        if not authorization or not hmac.compare_digest(
            authorization, f"Bearer {cfg.api_token}"
        ):
            raise HTTPException(status_code=403, detail="admin token required")

    # --- PWA shell -------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "service": "agentmgr-master", "version": __version__}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/manifest.json")
    def manifest() -> FileResponse:
        return FileResponse(_STATIC_DIR / "manifest.json")

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        return FileResponse(_STATIC_DIR / "sw.js", media_type="application/javascript")

    # --- passkey: registration (admin-token gated) ----------------------
    @app.post("/passkey/register/begin", dependencies=[Depends(require_admin)])
    def passkey_register_begin() -> dict:
        return {"options": passkey_svc.registration_options()}

    @app.post("/passkey/register/complete", dependencies=[Depends(require_admin)])
    def passkey_register_complete(body: dict = Body(...)) -> dict:
        cred = passkey_svc.verify_registration(
            json.dumps(body["credential"]), body.get("label", "passkey")
        )
        return {"registered": cred.credential_id, "label": cred.label}

    # --- passkey: login --------------------------------------------------
    @app.post("/passkey/login/begin")
    def passkey_login_begin() -> dict:
        if not passkey_svc.has_credentials():
            raise HTTPException(status_code=409, detail="no passkey registered yet")
        return {"options": passkey_svc.login_options()}

    @app.post("/passkey/login/complete")
    def passkey_login_complete(body: dict = Body(...)) -> dict:
        if not cfg.api_token:
            raise HTTPException(status_code=503, detail="auth not provisioned")
        try:
            cred = passkey_svc.verify_login(json.dumps(body["credential"]))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        log.info("passkey login", extra={"credential_id": cred.credential_id})
        return {
            "token": mint_pwa_token(cfg.api_token, cfg.pwa_token_ttl_s),
            "expires_in": cfg.pwa_token_ttl_s,
        }

    # --- password login (desktop, no biometric) -------------------------
    # Sign in with the api_token as a password. Besides a bearer token, this
    # opens an 'all'-scope session window signed by the token so writes/deletes
    # run without per-command Face ID — see Config.allow_password_login for the
    # trade-off. Phones should keep using /passkey/login instead.
    @app.post("/password/login")
    def password_login(body: dict = Body(...)) -> dict:
        if not cfg.allow_password_login:
            raise HTTPException(status_code=403, detail="password login disabled")
        if not cfg.api_token:
            raise HTTPException(status_code=503, detail="auth not provisioned")
        password = str(body.get("password", ""))
        if not hmac.compare_digest(password, cfg.api_token):
            raise HTTPException(status_code=401, detail="invalid password")
        grant = sign_grant_with_token(
            new_grant("all", cfg.password_session_ttl_s), cfg.api_token
        )
        store.put_session(grant)
        log.info("password login", extra={"session_id": grant.id})
        return {
            "token": mint_pwa_token(cfg.api_token, cfg.password_session_ttl_s),
            "expires_in": cfg.password_session_ttl_s,
            "session": grant.id,
        }

    # --- chat + agents ---------------------------------------------------
    @app.get("/agents", dependencies=[Depends(require_auth)])
    def agents() -> dict:
        return {
            "agents": [
                {
                    "name": a.name, "kind": a.kind, "runtime": a.runtime,
                    "capabilities": list(a.capabilities), "description": a.description,
                }
                for a in registry.all()
            ]
        }

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_auth)])
    def chat(req: ChatRequest) -> ChatResponse:
        return run_command(app, req)

    @app.post("/upload", dependencies=[Depends(require_auth)])
    async def upload(files: list[UploadFile] = File(...)) -> dict:
        """Receive chat attachments (files + screenshots) and save them to disk
        so the assistant can read them; return typed refs to pass back to /chat.
        Images are flagged so the model can view them directly."""
        if len(files) > _MAX_UPLOAD_FILES:
            raise HTTPException(
                status_code=413, detail=f"too many files (max {_MAX_UPLOAD_FILES})"
            )
        dest_dir = _UPLOAD_DIR / time.strftime("%Y%m%d")
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            data = await f.read()
            if len(data) > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"{f.filename!r} exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                )
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename or "file")[:100] or "file"
            fid = uuid.uuid4().hex[:12]
            path = dest_dir / f"{fid}-{safe}"
            path.write_bytes(data)
            media_type = (
                f.content_type
                or mimetypes.guess_type(safe)[0]
                or "application/octet-stream"
            )
            kind = "image" if media_type in _IMAGE_MEDIA_TYPES else "file"
            saved.append({
                "id": fid, "filename": f.filename or safe, "path": str(path),
                "kind": kind, "media_type": media_type, "size": len(data),
            })
        log.info("received chat attachments", extra={"count": len(saved)})
        return {"attachments": saved}

    @app.get("/cost", dependencies=[Depends(require_auth)])
    def cost() -> dict:
        """Cumulative LLM spend and the per-command budget ceiling."""
        return {
            "llm_provider": cfg.llm_provider,
            "total_llm_cost_usd": round(store.get_total_llm_cost(), 6),
            "budget_per_command_usd": cfg.llm_budget_usd,
        }

    # --- Cloud Run jobs (pre-populated run selector) --------------------
    @app.get("/cloudrun/jobs", dependencies=[Depends(require_auth)])
    def cloudrun_jobs() -> dict:
        """List the project's Cloud Run jobs so the UI can pre-populate a picker."""
        try:
            jobs = _get_cloudrun_admin(app).list_jobs()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"could not list Cloud Run jobs: {_clean_error(str(exc))}",
            ) from exc
        return {"jobs": jobs, "project": cfg.project_id, "region": cfg.region}

    @app.post("/cloudrun/jobs/{job_name}/run", dependencies=[Depends(require_auth)])
    def cloudrun_run_job(job_name: str) -> dict:
        """Trigger one Cloud Run job. Running a job is SENSITIVE, so it needs an
        active 'cloudrun'/'all' session window — desktop password login opens
        one; otherwise open a window from the session sheet first."""
        if session_mgr.active_grant("cloudrun") is None:
            raise HTTPException(
                status_code=409,
                detail="No active session. Sign in with your password (desktop) "
                "or open a Cloud Run session window first.",
            )
        try:
            admin = _get_cloudrun_admin(app)
            known = {j["name"] for j in admin.list_jobs()}
            if job_name not in known:
                raise HTTPException(status_code=404, detail=f"no such job: {job_name}")
            execution = admin.run_job(job_name)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"could not run {job_name}: {_clean_error(str(exc))}",
            ) from exc
        log.info("cloud run job triggered via UI", extra={"job": job_name})
        return {"job": job_name, "execution": execution, "started": True}

    # --- persistent memory ----------------------------------------------
    @app.get("/memory", dependencies=[Depends(require_auth)])
    def memory_list() -> dict:
        return {"memories": [e.model_dump() for e in memory.all()]}

    @app.post("/memory", dependencies=[Depends(require_auth)])
    def memory_add(body: dict = Body(...)) -> dict:
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty memory text")
        return {"memory": memory.add(text, source=body.get("source", "user")).model_dump()}

    @app.delete("/memory/{entry_id}", dependencies=[Depends(require_auth)])
    def memory_delete(entry_id: str) -> dict:
        if not memory.delete(entry_id):
            raise HTTPException(status_code=404, detail="no such memory")
        return {"ok": True, "deleted": entry_id}

    @app.delete("/memory", dependencies=[Depends(require_auth)])
    def memory_clear() -> dict:
        return {"ok": True, "cleared": memory.clear()}

    # --- connectors (authenticated external services) -------------------
    @app.get("/connectors", dependencies=[Depends(require_auth)])
    def connectors_status() -> dict:
        """Which external services are authenticated. Never returns secrets."""
        return {"connectors": connectors_registry.status()}

    # --- security-health worker -----------------------------------------
    @app.post("/security/health-check", dependencies=[Depends(require_auth)])
    def security_health_check(body: dict = Body(default={})) -> dict:
        """Dispatch the security-health worker (scans this Mac, emails a report)."""
        payload = {"send": bool(body.get("send", True))}
        if body.get("to"):
            payload["to"] = body["to"]
        task = TaskSpec(
            agent="security-health", kind="security", payload=payload,
            conversation_id=gen_id("conv"), correlation_id=gen_id("cmd"),
        )
        store.put_task(task)
        try:
            result = poll_until(
                lambda: store.get_task_result(task.id),
                timeout_s=cfg.task_timeout_s, interval_s=cfg.poll_interval_s,
            )
        except TimeoutExceeded as exc:
            raise HTTPException(
                status_code=504,
                detail="security scan timed out — is the Mac agent running?",
            ) from exc
        if result.status != TaskStatus.COMPLETED:
            raise HTTPException(status_code=502, detail=f"scan failed: {result.error}")
        out = result.output or {}
        return {"grade": out.get("grade"), "counts": out.get("counts"),
                "subject": out.get("subject"), "email": out.get("email")}

    # --- approvals -------------------------------------------------------
    @app.get("/approvals", dependencies=[Depends(require_auth)])
    def list_approvals() -> dict:
        return {
            "approvals": [
                {
                    "id": a.id, "action": a.action,
                    "reason": a.details.get("_sensitive_reason", "explicit"),
                    "details": {k: v for k, v in a.details.items()
                                if not k.startswith("_")},
                    "created_at": a.created_at,
                }
                for a in store.list_pending_approvals()
            ]
        }

    @app.post("/approvals/{approval_id}/options", dependencies=[Depends(require_auth)])
    def approval_options(approval_id: str, body: dict = Body(...)) -> dict:
        decision = body.get("decision", "approve")
        appr = store.get_approval(approval_id)
        if appr is None:
            raise HTTPException(status_code=404, detail="unknown approval")
        challenge = challenge_for(canonical_message(appr.id, appr.nonce, decision))
        return {"options": passkey_svc.assertion_options(challenge)}

    @app.post("/approvals/{approval_id}/submit", dependencies=[Depends(require_auth)])
    def approval_submit(approval_id: str, body: dict = Body(...)) -> dict:
        decision = body.get("decision", "approve")
        appr = store.get_approval(approval_id)
        if appr is None:
            raise HTTPException(status_code=404, detail="unknown approval")
        assertion = json.dumps(body["assertion"])
        challenge = challenge_for(canonical_message(appr.id, appr.nonce, decision))
        if not verify_assertion(store, assertion, challenge, cfg.rp_id, cfg.origin):
            raise HTTPException(status_code=401, detail="assertion failed verification")
        store.submit_approval_response(
            approval_id,
            ApprovalResponse(
                decision=decision, proof_type="webauthn",
                webauthn_assertion=assertion,
                credential_id=json.loads(assertion)["id"],
                approver_note=body.get("note", ""),
            ),
        )
        return {"ok": True, "approval_id": approval_id, "decision": decision}

    # --- session windows -------------------------------------------------
    @app.get("/sessions", dependencies=[Depends(require_auth)])
    def sessions() -> dict:
        return {
            "sessions": [
                {
                    "id": g.id, "scope": g.scope, "expires_at": g.expires_at,
                    "revoked": g.revoked, "active": session_mgr.verify_grant(g),
                }
                for g in store.list_sessions()
            ]
        }

    @app.post("/sessions/start/begin", dependencies=[Depends(require_auth)])
    def session_start_begin(body: dict = Body(...)) -> dict:
        scope = body.get("scope", "shell")
        ttl = float(body.get("ttl_s") or cfg.session_ttl_s)
        if scope not in ("shell", "cloudrun", "all"):
            raise HTTPException(status_code=400, detail="bad scope")
        grant = new_grant(scope, ttl)
        grant.proof_type = "webauthn"
        store.put_session(grant)  # inert until activated
        challenge = challenge_for(canonical_session_message(grant))
        return {"grant_id": grant.id, "options": passkey_svc.assertion_options(challenge)}

    @app.post("/sessions/{session_id}/activate", dependencies=[Depends(require_auth)])
    def session_activate(session_id: str, body: dict = Body(...)) -> dict:
        grant = store.get_session(session_id)
        if grant is None:
            raise HTTPException(status_code=404, detail="unknown session")
        assertion = json.dumps(body["assertion"])
        challenge = challenge_for(canonical_session_message(grant))
        if not verify_assertion(store, assertion, challenge, cfg.rp_id, cfg.origin):
            raise HTTPException(status_code=401, detail="assertion failed verification")
        grant.webauthn_assertion = assertion
        grant.credential_id = json.loads(assertion)["id"]
        store.put_session(grant)
        log.info("session activated", extra={"session_id": session_id})
        return {"activated": session_id, "expires_at": grant.expires_at}

    @app.post("/sessions/{session_id}/revoke", dependencies=[Depends(require_auth)])
    def revoke_session(session_id: str) -> dict:
        store.revoke_session(session_id)
        log.info("session revoked", extra={"session_id": session_id})
        return {"revoked": session_id}

    return app


# --- persistent memory: conversational control --------------------------

def _handle_memory_intent(message: str, memory: MemoryStore) -> str | None:
    """If the message is an explicit memory command, handle it and return a
    reply; otherwise return None so normal planning proceeds."""
    msg = message.strip()
    low = msg.lower()

    if low.startswith(("what do you remember", "what do you know about me",
                       "what's in your memory", "whats in your memory")):
        items = memory.all()
        if not items:
            return ("I don't have anything saved yet. Tell me to \"remember\" "
                    "something and it'll stick across sessions.")
        return "Here's what I remember:\n" + "\n".join(f"- {e.text}" for e in items)

    if low.startswith("forget"):
        rest = msg[len("forget"):].strip(" :,-")
        if rest.lower() in ("everything", "all", "it all", "all of it"):
            return f"Done — cleared all {memory.clear()} saved memories."
        if not rest:
            return "Forget what? e.g. \"forget that I prefer morning calls\"."
        removed = memory.delete_matching(rest)
        if removed:
            return "Forgotten:\n" + "\n".join(f"- {e.text}" for e in removed)
        return f"I couldn't find anything matching \"{rest}\" in memory."

    if low.startswith("remember"):
        rest = msg[len("remember"):].lstrip(" :,-")
        for prefix in ("that ", "this: ", "this ", "to ", "my ", "i "):
            if rest.lower().startswith(prefix):
                # keep "my"/"i" as part of the fact; only strip filler leads
                if prefix in ("that ", "this: ", "this "):
                    rest = rest[len(prefix):]
                break
        rest = rest.strip(" :,-")
        if not rest:
            return ("Remember what? e.g. \"remember that my brokerage is "
                    "Paradise Realty\".")
        entry = memory.add(rest, source="user")
        return f"Got it — I'll remember that: “{entry.text}”."

    return None


# --- command execution: the planner + the message hub -------------------

def run_command(app: FastAPI, req: ChatRequest) -> ChatResponse:
    cfg: Config = app.state.config
    store: StateStore = app.state.store

    correlation_id = gen_id("cmd")
    set_correlation_id(correlation_id)
    conversation_id = req.conversation_id or store.create_conversation()
    store.append_turn(conversation_id, ConversationTurn(role="user", text=req.message))
    log.info("received command", extra={"conversation_id": conversation_id})

    # Explicit memory commands ("remember …", "what do you remember", "forget …")
    # are handled directly — deterministic, no LLM, no Mac agent.
    mem_reply = _handle_memory_intent(req.message, app.state.memory)
    if mem_reply is not None:
        store.append_turn(conversation_id, ConversationTurn(role="master", text=mem_reply))
        return ChatResponse(
            conversation_id=conversation_id, correlation_id=correlation_id,
            interpretation="memory", task_ids=[], reply=mem_reply,
        )

    plan: Plan = app.state.planner.plan(
        req.message, app.state.registry, correlation_id
    )
    interpretation = f"{plan.summary} · command: {req.message!r}"
    log.info("planned", extra={"plan": plan.summary})

    # Give the assistant its persistent memory + connector awareness so it
    # "remembers" the operator and knows which services it can reach.
    mem_block = app.state.memory.prompt_block(cfg.memory_inject_limit)
    conn_block = connectors_registry.prompt_block()
    attachments_payload = [a.model_dump() for a in req.attachments]
    for step in plan.steps:
        if step.agent == "assistant":
            if mem_block:
                step.payload.setdefault("memory", mem_block)
            if conn_block:
                step.payload.setdefault("connectors", conn_block)
            if attachments_payload:
                step.payload["attachments"] = attachments_payload

    execution = _Execution(app, correlation_id, conversation_id)
    execution.run_plan(plan)

    # The chat bubble shows only the result(s), in plain prose — the technical
    # plan summary stays in the `interpretation` field for logs/debugging.
    reply = "\n\n".join(line for line in execution.result_lines if line) or "Done."
    store.append_turn(conversation_id, ConversationTurn(role="master", text=reply))
    return ChatResponse(
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        interpretation=interpretation,
        task_ids=execution.task_ids,
        reply=reply,
    )


class _Execution:
    """Runs one command's plan: dispatches subtasks and brokers the message bus.

    The Master is the hub — workers never call each other. A worker emits a
    Message; the Master picks it up here and relays it as a follow-up task.
    Every relay hop passes through ``check_relay`` (relay-depth bound) and every
    task through ``check_task`` (job fan-out bound).
    """

    def __init__(self, app: FastAPI, correlation_id: str, conversation_id: str) -> None:
        self.app = app
        self.cfg: Config = app.state.config
        self.store: StateStore = app.state.store
        self.registry: AgentRegistry = app.state.registry
        self.guard: FanoutGuard = app.state.guard
        self.gate: ApprovalGate = app.state.gate
        self.correlation_id = correlation_id
        self.conversation_id = conversation_id
        self.task_ids: list[str] = []
        self.result_lines: list[str] = []
        self._seen_messages: set[str] = set()

    def run_plan(self, plan: Plan) -> None:
        prev_output: dict = {}
        for idx, step in enumerate(plan.steps):
            payload = dict(step.payload)
            depth = 0
            if step.consume_previous and prev_output:
                # pipeline relay — prior step's output flows THROUGH the Master
                relay = Message(
                    correlation_id=self.correlation_id,
                    from_agent=plan.steps[idx - 1].agent,
                    to_agent=step.agent,
                    payload={"result": prev_output.get("result")},
                    relay_depth=1,
                )
                try:
                    self.guard.check_relay(relay)
                except FanoutLimitExceeded as exc:
                    self.result_lines.append(f"pipeline relay bounded: {exc}")
                    return
                self.store.put_message(relay)
                self._seen_messages.add(relay.id)
                payload["relayed_input"] = prev_output
                depth = 1

            task = TaskSpec(
                agent=step.agent, kind=step.kind, payload=payload,
                conversation_id=self.conversation_id,
                correlation_id=self.correlation_id,
                depth=depth, sensitive=step.sensitive,
            )
            prev_output = self._run_task_and_relays(task)

    def _run_task_and_relays(self, initial: TaskSpec) -> dict:
        """Run a task, then drain the messages it (and its relay descendants)
        emit to other agents — bounded by check_relay and check_task."""
        worklist: deque[TaskSpec] = deque([initial])
        last_output: dict = {}
        while worklist:
            task = worklist.popleft()
            agent = self.registry.get(task.agent)

            try:
                self.guard.check_task(self.correlation_id, depth=task.depth)
            except FanoutLimitExceeded as exc:
                self.result_lines.append(f"[{task.agent}] fan-out bounded: {exc}")
                return last_output

            # SENSITIVE Cloud Run subtask -> approval gate BEFORE the trigger.
            # local-agent and master-inline runtimes self-gate (Mac agent /
            # _execute_inline), so they are not double-gated here.
            if task.sensitive and agent.runtime == "cloudrun-job":
                try:
                    self.gate.request_approval(f"task:{task.kind}", task.payload)
                except (ApprovalDenied, ApprovalNotProvisioned) as exc:
                    self.result_lines.append(f"[{task.agent}] blocked: {exc}")
                    continue

            self.store.put_task(task)
            self.task_ids.append(task.id)
            _dispatch(self.app, agent, task)
            self.result_lines.append(_await_result(self.store, self.cfg, task))
            result = self.store.get_task_result(task.id)
            if result is not None:
                last_output = result.output

            self._drain_relays(task, worklist)
        return last_output

    def _drain_relays(self, task: TaskSpec, worklist: deque) -> None:
        """Enqueue follow-up tasks for messages this task emitted to other
        agents. Each hop is bounded by the relay-depth guard."""
        for msg in self.store.get_messages(self.correlation_id):
            if (
                msg.id in self._seen_messages
                or msg.from_agent != task.agent
                or msg.to_agent in ("master", "")
            ):
                continue
            self._seen_messages.add(msg.id)
            try:
                self.guard.check_relay(msg)
            except FanoutLimitExceeded as exc:
                self.result_lines.append(
                    f"relay bounded at depth {msg.relay_depth}: {exc}"
                )
                continue
            followup_agent = self.registry.get(msg.to_agent)
            worklist.append(
                TaskSpec(
                    agent=msg.to_agent, kind=followup_agent.kind,
                    payload=dict(msg.payload),
                    conversation_id=self.conversation_id,
                    correlation_id=self.correlation_id,
                    depth=msg.relay_depth,
                    sensitive=followup_agent.sensitive_default,
                )
            )
            log.info(
                "relayed message to next agent",
                extra={"to": msg.to_agent, "relay_depth": msg.relay_depth},
            )


def _dispatch(app: FastAPI, agent: AgentSpec, task: TaskSpec) -> None:
    if agent.runtime == "cloudrun-job":
        app.state.runner.run_task(agent, task.id)
    elif agent.runtime == "local-agent":
        log.info("task queued for local agent", extra={"task_id": task.id})
    elif agent.runtime == "master-inline":
        _execute_inline(app, agent, task)
    else:
        raise ValueError(f"unknown agent runtime: {agent.runtime}")


def _get_cloudrun_admin(app: FastAPI):
    """Lazily build (and cache) the Cloud Run admin client. Uses ADC."""
    if app.state.cloudrun_admin is None:
        from agentmgr.cloudrun_admin import CloudRunAdmin

        app.state.cloudrun_admin = CloudRunAdmin(app.state.config)
    return app.state.cloudrun_admin


def _execute_inline(app: FastAPI, agent: AgentSpec, task: TaskSpec) -> None:
    store: StateStore = app.state.store
    store.update_task_status(task.id, TaskStatus.RUNNING)
    try:
        if app.state.cloudrun_admin is None:
            _get_cloudrun_admin(app)
        op = str(task.payload.get("op", "list_services"))
        if op in MANAGE_OPS:
            app.state.gate.request_approval(f"cloudrun:{op}", task.payload)
        output = app.state.cloudrun_admin.execute(op, task.payload)
        store.put_task_result(
            TaskResult(task_id=task.id, status=TaskStatus.COMPLETED,
                       output=output, worker=agent.name)
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("inline execution failed", extra={"task_id": task.id})
        store.put_task_result(
            TaskResult(task_id=task.id, status=TaskStatus.FAILED,
                       error=f"{type(exc).__name__}: {exc}", worker=agent.name)
        )


# Agents the operator talks WITH (vs. batch workers it dispatches): these
# speak in plain prose, not labeled status lines or raw data dumps.
_CONVERSATIONAL_AGENTS = frozenset({"assistant", "mac-shell"})


def _clean_error(error: str | None) -> str:
    """Trim a raw exception down to a short, readable cause — drop any trailing
    dict/JSON blob (e.g. the '... - {...}' tail of an API error)."""
    text = str(error or "").strip()
    if not text:
        return "no details were given"
    for sep in (" - {", ": {", " {'", " {\""):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    text = text.rstrip(".")
    return text if len(text) <= 300 else text[:300].rstrip() + "…"


def _humanize_result(task: TaskSpec, result: TaskResult) -> str:
    """Render a task result as a line a person actually wants to read."""
    out = result.output
    if result.status == TaskStatus.COMPLETED:
        if isinstance(out, dict) and "answer" in out:          # the assistant
            answer = str(out.get("answer") or "").strip()
            if out.get("hit_limit"):
                answer += (
                    "\n\nI stopped after reaching my step limit — tell me to "
                    "keep going if you'd like me to continue."
                )
            return answer or "Done."
        if isinstance(out, dict) and "exit_code" in out:       # a shell command
            stdout = str(out.get("stdout") or "").strip()
            stderr = str(out.get("stderr") or "").strip()
            if out.get("exit_code") == 0:
                return stdout or "Done — that ran with no output."
            tail = stderr or f"It exited with code {out.get('exit_code')}."
            return f"{stdout}\n{tail}".strip()
        if isinstance(out, dict) and "grade" in out:           # security-health
            c = out.get("counts", {})
            email = out.get("email") or {}
            if email.get("sent"):
                where = f"Report emailed to {email.get('to')}."
            elif email:
                where = f"Email failed: {email.get('error')}"
            else:
                where = "No email sent."
            return (f"Security health: grade {out['grade']} — "
                    f"{c.get('critical', 0)} critical, {c.get('high', 0)} high, "
                    f"{c.get('medium', 0)} medium. {where}")
        return f"[{task.agent}] completed: {out}"              # echo/transform
    # failure
    if task.agent in _CONVERSATIONAL_AGENTS:
        return f"Sorry, that didn't work — {_clean_error(result.error)}."
    return f"[{task.agent}] FAILED: {result.error}"


def _await_result(store: StateStore, cfg: Config, task: TaskSpec) -> str:
    try:
        result: TaskResult = poll_until(
            lambda: store.get_task_result(task.id),
            timeout_s=cfg.task_timeout_s,
            interval_s=cfg.poll_interval_s,
        )
    except TimeoutExceeded:
        log.warning("task timed out", extra={"task_id": task.id})
        if task.agent in _CONVERSATIONAL_AGENTS:
            return (
                "That timed out before it finished. Is the Mac agent running, "
                "or is there an approval still waiting?"
            )
        return (
            f"[{task.agent}] timed out after {cfg.task_timeout_s}s "
            f"(is the Mac agent running / a session or approval pending?)"
        )
    return _humanize_result(task, result)


# Served via uvicorn factory mode:  uvicorn master.main:build_app --factory
