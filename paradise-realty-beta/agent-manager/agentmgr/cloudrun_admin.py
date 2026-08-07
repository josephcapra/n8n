"""Cloud Run administration — path B (Phase 1.5).

Lets the Master read and manage Cloud Run through its own scoped service
account, so it works even when the Mac is offline. Path A (running ``gcloud``
on the Mac) needs no special code — it is just a shell command for the Mac
agent.

Read ops (`list_*`) are benign. Mutating ops (`run_job`) are SENSITIVE — the
Master routes them through the approval flow before calling them here.

If the Python SDK fails (e.g. ADC permissions), falls back to gcloud CLI.
"""

from __future__ import annotations

import subprocess

from .config import Config
from .logging_utils import get_logger
from .util import retry

log = get_logger("agentmgr.cloudrun_admin")

READ_OPS = frozenset({"list_services", "list_jobs"})
MANAGE_OPS = frozenset({"run_job"})


class CloudRunAdmin:
    def __init__(self, config: Config) -> None:
        self._project = config.project_id
        self._region = config.region
        self._sdk_available = False
        self._run_v2 = None
        self._services = None
        self._jobs = None
        try:
            from google.cloud import run_v2
            self._run_v2 = run_v2
            self._services = run_v2.ServicesClient()
            self._jobs = run_v2.JobsClient()
            self._sdk_available = True
        except ImportError:
            log.info("google-cloud-run SDK not installed, using gcloud CLI")

    @property
    def _parent(self) -> str:
        return self._parent_for(self._region)

    def _parent_for(self, region: str | None) -> str:
        return f"projects/{self._project}/locations/{region or self._region}"

    def _gcloud_list_jobs(self, region: str | None = None) -> list[dict]:
        """Fallback: use gcloud CLI to list jobs."""
        r = region or self._region
        try:
            out = subprocess.run(
                ["gcloud", "run", "jobs", "list",
                 f"--project={self._project}", f"--region={r}",
                 "--format=value(name)"],
                capture_output=True, text=True, timeout=30
            )
            if out.returncode != 0:
                log.warning("gcloud list_jobs failed", extra={"stderr": out.stderr[:200]})
                return []
            names = [n.strip() for n in out.stdout.strip().split("\n") if n.strip()]
            return [{"name": n, "region": r} for n in names]
        except Exception as e:
            log.warning("gcloud list_jobs exception", extra={"error": str(e)})
            return []

    def _gcloud_run_job(self, job_name: str, region: str | None = None) -> str:
        """Fallback: use gcloud CLI to run a job."""
        r = region or self._region
        out = subprocess.run(
            ["gcloud", "run", "jobs", "execute", job_name,
             f"--project={self._project}", f"--region={r}",
             "--format=value(name)", "--async"],
            capture_output=True, text=True, timeout=30
        )
        if out.returncode != 0:
            raise RuntimeError(f"gcloud run job failed: {out.stderr[:200]}")
        log.info("cloud run job triggered via gcloud", extra={"job": job_name, "region": r})
        return out.stdout.strip() or "execution-started"

    def list_services(self) -> list[dict]:
        if not self._sdk_available:
            return []  # no CLI fallback for services yet
        items = retry(lambda: list(self._services.list_services(parent=self._parent)))
        return [{"name": s.name.split("/")[-1], "uri": s.uri} for s in items]

    def list_jobs(self, region: str | None = None) -> list[dict]:
        if not self._sdk_available:
            return self._gcloud_list_jobs(region)
        parent = self._parent_for(region)
        try:
            items = retry(lambda: list(self._jobs.list_jobs(parent=parent)))
            return [{"name": j.name.split("/")[-1], "region": region or self._region} for j in items]
        except Exception as e:
            log.warning("SDK list_jobs failed, falling back to gcloud", extra={"error": str(e)[:100]})
            return self._gcloud_list_jobs(region)

    def run_job(self, job_name: str, region: str | None = None) -> str:
        """Trigger a Cloud Run Job. SENSITIVE — caller must gate this first."""
        if not self._sdk_available:
            return self._gcloud_run_job(job_name, region)
        path = f"{self._parent_for(region)}/jobs/{job_name}"
        try:
            op = retry(
                lambda: self._jobs.run_job(
                    request=self._run_v2.RunJobRequest(name=path)
                )
            )
            log.info("cloud run job triggered", extra={"job": job_name, "region": region or self._region})
            return getattr(op.metadata, "name", "execution-started")
        except Exception as e:
            log.warning("SDK run_job failed, falling back to gcloud", extra={"error": str(e)[:100]})
            return self._gcloud_run_job(job_name, region)

    def execute(self, op: str, args: dict) -> dict:
        if op == "list_services":
            return {"services": self.list_services()}
        if op == "list_jobs":
            return {"jobs": self.list_jobs()}
        if op == "run_job":
            return {"execution": self.run_job(args["job_name"], args.get("region"))}
        raise ValueError(f"unknown cloud run op: {op!r}")
