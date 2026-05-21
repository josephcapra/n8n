"""Cloud Run administration — path B (Phase 1.5).

Lets the Master read and manage Cloud Run through its own scoped service
account, so it works even when the Mac is offline. Path A (running ``gcloud``
on the Mac) needs no special code — it is just a shell command for the Mac
agent.

Read ops (`list_*`) are benign. Mutating ops (`run_job`) are SENSITIVE — the
Master routes them through the approval flow before calling them here.
"""

from __future__ import annotations

from .config import Config
from .logging_utils import get_logger
from .util import retry

log = get_logger("agentmgr.cloudrun_admin")

READ_OPS = frozenset({"list_services", "list_jobs"})
MANAGE_OPS = frozenset({"run_job"})


class CloudRunAdmin:
    def __init__(self, config: Config) -> None:
        from google.cloud import run_v2  # lazy import

        self._run_v2 = run_v2
        self._services = run_v2.ServicesClient()
        self._jobs = run_v2.JobsClient()
        self._project = config.project_id
        self._region = config.region

    @property
    def _parent(self) -> str:
        return f"projects/{self._project}/locations/{self._region}"

    def list_services(self) -> list[dict]:
        items = retry(lambda: list(self._services.list_services(parent=self._parent)))
        return [{"name": s.name.split("/")[-1], "uri": s.uri} for s in items]

    def list_jobs(self) -> list[dict]:
        items = retry(lambda: list(self._jobs.list_jobs(parent=self._parent)))
        return [{"name": j.name.split("/")[-1]} for j in items]

    def run_job(self, job_name: str) -> str:
        """Trigger a Cloud Run Job. SENSITIVE — caller must gate this first."""
        path = f"{self._parent}/jobs/{job_name}"
        op = retry(
            lambda: self._jobs.run_job(
                request=self._run_v2.RunJobRequest(name=path)
            )
        )
        log.info("cloud run job triggered", extra={"job": job_name})
        return getattr(op.metadata, "name", "execution-started")

    def execute(self, op: str, args: dict) -> dict:
        if op == "list_services":
            return {"services": self.list_services()}
        if op == "list_jobs":
            return {"jobs": self.list_jobs()}
        if op == "run_job":
            return {"execution": self.run_job(args["job_name"])}
        raise ValueError(f"unknown cloud run op: {op!r}")
