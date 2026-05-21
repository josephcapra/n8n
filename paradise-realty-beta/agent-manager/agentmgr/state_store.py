"""Shared state store behind a swappable interface.

Holds four things, as required by the architecture:
  * conversation / command history
  * per-task state and results
  * the inter-agent **message bus** (hub model — workers write here, the
    Master reads and relays; no direct worker-to-worker calls)
  * pending approval requests

Two implementations ship now:
  * :class:`FirestoreStateStore` — production (Firestore native mode)
  * :class:`InMemoryStateStore` — local dev and tests

Swapping backends is a one-line config change (``AGENTMGR_STATE_BACKEND``);
nothing else in the codebase imports a concrete backend directly.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from .config import Config
from .schemas import (
    ApprovalRequest,
    ApprovalResponse,
    ApprovalStatus,
    ConversationTurn,
    Message,
    PasskeyCredential,
    SessionGrant,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from .util import gen_id


class StateStore(ABC):
    """Abstract state store. All persistence in the system goes through this."""

    # --- conversations ---------------------------------------------------
    @abstractmethod
    def create_conversation(self) -> str: ...

    @abstractmethod
    def append_turn(self, conversation_id: str, turn: ConversationTurn) -> None: ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> list[ConversationTurn]: ...

    # --- tasks -----------------------------------------------------------
    @abstractmethod
    def put_task(self, task: TaskSpec) -> None: ...

    @abstractmethod
    def get_task(self, task_id: str) -> TaskSpec | None: ...

    @abstractmethod
    def update_task_status(self, task_id: str, status: TaskStatus) -> None: ...

    @abstractmethod
    def put_task_result(self, result: TaskResult) -> None: ...

    @abstractmethod
    def get_task_result(self, task_id: str) -> TaskResult | None: ...

    # --- inter-agent message bus ----------------------------------------
    @abstractmethod
    def put_message(self, message: Message) -> None: ...

    @abstractmethod
    def get_messages(self, correlation_id: str) -> list[Message]: ...

    # --- approvals -------------------------------------------------------
    @abstractmethod
    def put_approval(self, approval: ApprovalRequest) -> None: ...

    @abstractmethod
    def get_approval(self, approval_id: str) -> ApprovalRequest | None: ...

    @abstractmethod
    def list_pending_approvals(self) -> list[ApprovalRequest]: ...

    @abstractmethod
    def submit_approval_response(
        self, approval_id: str, response: ApprovalResponse
    ) -> None: ...

    @abstractmethod
    def set_approval_status(self, approval_id: str, status: ApprovalStatus) -> None: ...

    # --- fan-out / loop guard counters ----------------------------------
    @abstractmethod
    def incr_job_count(self, correlation_id: str) -> int:
        """Atomically increment and return the job count for a command."""

    # --- session window (Phase 1.5) -------------------------------------
    @abstractmethod
    def put_session(self, grant: SessionGrant) -> None: ...

    @abstractmethod
    def get_session(self, session_id: str) -> SessionGrant | None: ...

    @abstractmethod
    def list_sessions(self) -> list[SessionGrant]: ...

    @abstractmethod
    def revoke_session(self, session_id: str) -> None: ...

    # --- task queue for local-agent runtimes ----------------------------
    @abstractmethod
    def get_pending_tasks(self, agent: str) -> list[TaskSpec]:
        """Tasks addressed to ``agent`` that are still PENDING (poll source
        for local agents, which are not triggered like Cloud Run Jobs)."""

    # --- passkeys + WebAuthn challenges (Phase 1.5 Increment 2) ----------
    @abstractmethod
    def put_passkey(self, credential: PasskeyCredential) -> None: ...

    @abstractmethod
    def get_passkey(self, credential_id: str) -> PasskeyCredential | None: ...

    @abstractmethod
    def list_passkeys(self) -> list[PasskeyCredential]: ...

    @abstractmethod
    def put_challenge(self, key: str, challenge: str) -> None: ...

    @abstractmethod
    def pop_challenge(self, key: str) -> str | None:
        """Return and consume a one-time WebAuthn challenge, or None."""

    # --- LLM cost accounting (Phase 3) ----------------------------------
    @abstractmethod
    def record_llm_cost(self, correlation_id: str, usd: float) -> float:
        """Add ``usd`` to a command's cumulative LLM cost; return the new total."""

    @abstractmethod
    def get_llm_cost(self, correlation_id: str) -> float: ...

    @abstractmethod
    def get_total_llm_cost(self) -> float: ...


class InMemoryStateStore(StateStore):
    """Thread-safe in-process store for local dev and tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conversations: dict[str, list[ConversationTurn]] = {}
        self._tasks: dict[str, TaskSpec] = {}
        self._results: dict[str, TaskResult] = {}
        self._messages: list[Message] = []
        self._approvals: dict[str, ApprovalRequest] = {}
        self._job_counts: dict[str, int] = {}
        self._sessions: dict[str, SessionGrant] = {}
        self._passkeys: dict[str, PasskeyCredential] = {}
        self._challenges: dict[str, str] = {}
        self._llm_cost: dict[str, float] = {}

    def create_conversation(self) -> str:
        with self._lock:
            cid = gen_id("conv")
            self._conversations[cid] = []
            return cid

    def append_turn(self, conversation_id: str, turn: ConversationTurn) -> None:
        with self._lock:
            self._conversations.setdefault(conversation_id, []).append(turn)

    def get_conversation(self, conversation_id: str) -> list[ConversationTurn]:
        with self._lock:
            return list(self._conversations.get(conversation_id, []))

    def put_task(self, task: TaskSpec) -> None:
        with self._lock:
            self._tasks[task.id] = task.model_copy(deep=True)

    def get_task(self, task_id: str) -> TaskSpec | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return t.model_copy(deep=True) if t else None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].status = status

    def put_task_result(self, result: TaskResult) -> None:
        with self._lock:
            self._results[result.task_id] = result.model_copy(deep=True)
            if result.task_id in self._tasks:
                self._tasks[result.task_id].status = result.status

    def get_task_result(self, task_id: str) -> TaskResult | None:
        with self._lock:
            r = self._results.get(task_id)
            return r.model_copy(deep=True) if r else None

    def put_message(self, message: Message) -> None:
        with self._lock:
            self._messages.append(message.model_copy(deep=True))

    def get_messages(self, correlation_id: str) -> list[Message]:
        with self._lock:
            return [
                m.model_copy(deep=True)
                for m in self._messages
                if m.correlation_id == correlation_id
            ]

    def put_approval(self, approval: ApprovalRequest) -> None:
        with self._lock:
            self._approvals[approval.id] = approval.model_copy(deep=True)

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            a = self._approvals.get(approval_id)
            return a.model_copy(deep=True) if a else None

    def list_pending_approvals(self) -> list[ApprovalRequest]:
        with self._lock:
            return [
                a.model_copy(deep=True)
                for a in self._approvals.values()
                if a.status == ApprovalStatus.PENDING
            ]

    def submit_approval_response(
        self, approval_id: str, response: ApprovalResponse
    ) -> None:
        with self._lock:
            if approval_id not in self._approvals:
                raise KeyError(f"unknown approval {approval_id}")
            self._approvals[approval_id].response = response.model_copy(deep=True)

    def set_approval_status(self, approval_id: str, status: ApprovalStatus) -> None:
        with self._lock:
            if approval_id in self._approvals:
                self._approvals[approval_id].status = status

    def incr_job_count(self, correlation_id: str) -> int:
        with self._lock:
            self._job_counts[correlation_id] = (
                self._job_counts.get(correlation_id, 0) + 1
            )
            return self._job_counts[correlation_id]

    def put_session(self, grant: SessionGrant) -> None:
        with self._lock:
            self._sessions[grant.id] = grant.model_copy(deep=True)

    def get_session(self, session_id: str) -> SessionGrant | None:
        with self._lock:
            s = self._sessions.get(session_id)
            return s.model_copy(deep=True) if s else None

    def list_sessions(self) -> list[SessionGrant]:
        with self._lock:
            return [s.model_copy(deep=True) for s in self._sessions.values()]

    def revoke_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].revoked = True

    def get_pending_tasks(self, agent: str) -> list[TaskSpec]:
        with self._lock:
            return [
                t.model_copy(deep=True)
                for t in self._tasks.values()
                if t.agent == agent and t.status == TaskStatus.PENDING
            ]

    def put_passkey(self, credential: PasskeyCredential) -> None:
        with self._lock:
            self._passkeys[credential.credential_id] = credential.model_copy(deep=True)

    def get_passkey(self, credential_id: str) -> PasskeyCredential | None:
        with self._lock:
            c = self._passkeys.get(credential_id)
            return c.model_copy(deep=True) if c else None

    def list_passkeys(self) -> list[PasskeyCredential]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self._passkeys.values()]

    def put_challenge(self, key: str, challenge: str) -> None:
        with self._lock:
            self._challenges[key] = challenge

    def pop_challenge(self, key: str) -> str | None:
        with self._lock:
            return self._challenges.pop(key, None)

    def record_llm_cost(self, correlation_id: str, usd: float) -> float:
        with self._lock:
            self._llm_cost[correlation_id] = (
                self._llm_cost.get(correlation_id, 0.0) + usd
            )
            return self._llm_cost[correlation_id]

    def get_llm_cost(self, correlation_id: str) -> float:
        with self._lock:
            return self._llm_cost.get(correlation_id, 0.0)

    def get_total_llm_cost(self) -> float:
        with self._lock:
            return sum(self._llm_cost.values())


class FirestoreStateStore(StateStore):
    """Firestore-backed store for production.

    Collections are namespaced by ``config.collection_prefix`` so they never
    collide with other projects sharing the same Firestore database.
    """

    def __init__(self, config: Config) -> None:
        from google.cloud import firestore  # lazy: tests need not import GCP libs

        self._firestore = firestore
        self._db = firestore.Client(project=config.project_id)
        p = config.collection_prefix
        self._c_conv = f"{p}conversations"
        self._c_task = f"{p}tasks"
        self._c_result = f"{p}task_results"
        self._c_msg = f"{p}messages"
        self._c_appr = f"{p}approvals"
        self._c_counter = f"{p}counters"
        self._c_session = f"{p}sessions"
        self._c_passkey = f"{p}passkeys"
        self._c_challenge = f"{p}challenges"

    # conversations -------------------------------------------------------
    def create_conversation(self) -> str:
        cid = gen_id("conv")
        self._db.collection(self._c_conv).document(cid).set(
            {"turns": [], "created_at": cid}
        )
        return cid

    def append_turn(self, conversation_id: str, turn: ConversationTurn) -> None:
        self._db.collection(self._c_conv).document(conversation_id).set(
            {"turns": self._firestore.ArrayUnion([turn.model_dump()])},
            merge=True,
        )

    def get_conversation(self, conversation_id: str) -> list[ConversationTurn]:
        snap = self._db.collection(self._c_conv).document(conversation_id).get()
        if not snap.exists:
            return []
        return [ConversationTurn(**t) for t in snap.to_dict().get("turns", [])]

    # tasks ---------------------------------------------------------------
    def put_task(self, task: TaskSpec) -> None:
        self._db.collection(self._c_task).document(task.id).set(task.model_dump())

    def get_task(self, task_id: str) -> TaskSpec | None:
        snap = self._db.collection(self._c_task).document(task_id).get()
        return TaskSpec(**snap.to_dict()) if snap.exists else None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        self._db.collection(self._c_task).document(task_id).set(
            {"status": status.value}, merge=True
        )

    def put_task_result(self, result: TaskResult) -> None:
        self._db.collection(self._c_result).document(result.task_id).set(
            result.model_dump()
        )
        self.update_task_status(result.task_id, result.status)

    def get_task_result(self, task_id: str) -> TaskResult | None:
        snap = self._db.collection(self._c_result).document(task_id).get()
        return TaskResult(**snap.to_dict()) if snap.exists else None

    # message bus ---------------------------------------------------------
    def put_message(self, message: Message) -> None:
        self._db.collection(self._c_msg).document(message.id).set(message.model_dump())

    def get_messages(self, correlation_id: str) -> list[Message]:
        docs = (
            self._db.collection(self._c_msg)
            .where("correlation_id", "==", correlation_id)
            .stream()
        )
        msgs = [Message(**d.to_dict()) for d in docs]
        return sorted(msgs, key=lambda m: m.created_at)

    # approvals -----------------------------------------------------------
    def put_approval(self, approval: ApprovalRequest) -> None:
        self._db.collection(self._c_appr).document(approval.id).set(
            approval.model_dump()
        )

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        snap = self._db.collection(self._c_appr).document(approval_id).get()
        return ApprovalRequest(**snap.to_dict()) if snap.exists else None

    def list_pending_approvals(self) -> list[ApprovalRequest]:
        docs = (
            self._db.collection(self._c_appr)
            .where("status", "==", ApprovalStatus.PENDING.value)
            .stream()
        )
        return [ApprovalRequest(**d.to_dict()) for d in docs]

    def submit_approval_response(
        self, approval_id: str, response: ApprovalResponse
    ) -> None:
        ref = self._db.collection(self._c_appr).document(approval_id)
        if not ref.get().exists:
            raise KeyError(f"unknown approval {approval_id}")
        ref.set({"response": response.model_dump()}, merge=True)

    def set_approval_status(self, approval_id: str, status: ApprovalStatus) -> None:
        self._db.collection(self._c_appr).document(approval_id).set(
            {"status": status.value}, merge=True
        )

    # counters ------------------------------------------------------------
    def incr_job_count(self, correlation_id: str) -> int:
        ref = self._db.collection(self._c_counter).document(correlation_id)

        @self._firestore.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            current = snap.to_dict().get("job_count", 0) if snap.exists else 0
            txn.set(ref, {"job_count": current + 1}, merge=True)
            return current + 1

        return _txn(self._db.transaction())

    # sessions ------------------------------------------------------------
    def put_session(self, grant: SessionGrant) -> None:
        self._db.collection(self._c_session).document(grant.id).set(
            grant.model_dump()
        )

    def get_session(self, session_id: str) -> SessionGrant | None:
        snap = self._db.collection(self._c_session).document(session_id).get()
        return SessionGrant(**snap.to_dict()) if snap.exists else None

    def list_sessions(self) -> list[SessionGrant]:
        return [
            SessionGrant(**d.to_dict())
            for d in self._db.collection(self._c_session).stream()
        ]

    def revoke_session(self, session_id: str) -> None:
        self._db.collection(self._c_session).document(session_id).set(
            {"revoked": True}, merge=True
        )

    def get_pending_tasks(self, agent: str) -> list[TaskSpec]:
        docs = (
            self._db.collection(self._c_task)
            .where("agent", "==", agent)
            .where("status", "==", TaskStatus.PENDING.value)
            .stream()
        )
        return [TaskSpec(**d.to_dict()) for d in docs]

    # passkeys + challenges ----------------------------------------------
    def put_passkey(self, credential: PasskeyCredential) -> None:
        self._db.collection(self._c_passkey).document(
            credential.credential_id
        ).set(credential.model_dump())

    def get_passkey(self, credential_id: str) -> PasskeyCredential | None:
        snap = self._db.collection(self._c_passkey).document(credential_id).get()
        return PasskeyCredential(**snap.to_dict()) if snap.exists else None

    def list_passkeys(self) -> list[PasskeyCredential]:
        return [
            PasskeyCredential(**d.to_dict())
            for d in self._db.collection(self._c_passkey).stream()
        ]

    def put_challenge(self, key: str, challenge: str) -> None:
        self._db.collection(self._c_challenge).document(key).set(
            {"challenge": challenge}
        )

    def pop_challenge(self, key: str) -> str | None:
        ref = self._db.collection(self._c_challenge).document(key)
        snap = ref.get()
        if not snap.exists:
            return None
        ref.delete()
        return snap.to_dict().get("challenge")

    # LLM cost accounting -------------------------------------------------
    def record_llm_cost(self, correlation_id: str, usd: float) -> float:
        cmd_ref = self._db.collection(self._c_counter).document(
            f"llmcost_{correlation_id}"
        )
        global_ref = self._db.collection(self._c_counter).document("llmcost__global")

        @self._firestore.transactional
        def _txn(txn):
            snap = cmd_ref.get(transaction=txn)
            current = snap.to_dict().get("llm_cost_usd", 0.0) if snap.exists else 0.0
            new_total = current + usd
            txn.set(cmd_ref, {"llm_cost_usd": new_total}, merge=True)
            txn.set(
                global_ref,
                {"llm_cost_usd": self._firestore.Increment(usd)},
                merge=True,
            )
            return new_total

        return _txn(self._db.transaction())

    def get_llm_cost(self, correlation_id: str) -> float:
        snap = self._db.collection(self._c_counter).document(
            f"llmcost_{correlation_id}"
        ).get()
        return snap.to_dict().get("llm_cost_usd", 0.0) if snap.exists else 0.0

    def get_total_llm_cost(self) -> float:
        snap = self._db.collection(self._c_counter).document("llmcost__global").get()
        return snap.to_dict().get("llm_cost_usd", 0.0) if snap.exists else 0.0


def make_state_store(config: Config) -> StateStore:
    """Factory — the single place a concrete backend is chosen."""
    if config.state_backend == "memory":
        return InMemoryStateStore()
    if config.state_backend == "firestore":
        return FirestoreStateStore(config)
    raise ValueError(f"unknown state backend: {config.state_backend}")
