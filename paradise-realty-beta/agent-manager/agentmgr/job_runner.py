"""Job runner behind a swappable interface.

Worker agents are Cloud Run **Jobs** (run-to-completion). The Master triggers
them through this interface and never blocks on them — the worker writes its
typed result to the state store and exits; the Master polls the store.

  * :class:`CloudRunJobRunner` — production; triggers via the Cloud Run Admin
    API, passing the task id as an env override.
  * :class:`LocalJobRunner` — local dev / tests; runs the worker in-process so
    an end-to-end run needs no GCP access.

Every trigger is wrapped in a bounded retry (cross-cutting requirement).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from .config import Config
from .logging_utils import get_logger
from .registry import AgentSpec
from .state_store import StateStore
from .util import retry

log = get_logger("agentmgr.job_runner")


class JobRunner(ABC):
    @abstractmethod
    def run_task(self, agent: AgentSpec, task_id: str) -> None:
        """Trigger ``agent``'s Cloud Run Job for ``task_id``. Fire-and-forget."""


class CloudRunJobRunner(JobRunner):
    """Triggers real Cloud Run Jobs via the Cloud Run Admin API."""

    def __init__(self, config: Config) -> None:
        from google.cloud import run_v2  # lazy import

        self._run_v2 = run_v2
        self._client = run_v2.JobsClient()
        self._project = config.project_id
        self._attempts = config.trigger_retry_attempts

    def run_task(self, agent: AgentSpec, task_id: str) -> None:
        run_v2 = self._run_v2
        job_path = (
            f"projects/{self._project}/locations/{agent.region}"
            f"/jobs/{agent.job_name}"
        )
        # Pass the task id as a per-execution env override. The worker reads
        # AGENTMGR_TASK_ID, fetches the typed TaskSpec from the store, runs it.
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name="AGENTMGR_TASK_ID", value=task_id)]
                )
            ]
        )
        request = run_v2.RunJobRequest(name=job_path, overrides=overrides)
        retry(
            lambda: self._client.run_job(request=request),
            attempts=self._attempts,
        )
        log.info(
            "triggered cloud run job",
            extra={"job": agent.job_name, "task_id": task_id},
        )


class LocalJobRunner(JobRunner):
    """Runs the worker in-process against the shared store (local dev / tests).

    Mimics a Cloud Run Job: the call returns immediately and the worker runs
    on a background thread, writing its result to the same store the Master
    polls.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def run_task(self, agent: AgentSpec, task_id: str) -> None:
        import importlib  # lazy: avoid import cycle

        module = importlib.import_module(agent.worker_module or "worker.run")

        def _run() -> None:
            try:
                module.run_task(task_id, self._store)
            except Exception:  # noqa: BLE001 - worker records its own failure
                log.exception("local worker crashed", extra={"task_id": task_id})

        thread = threading.Thread(
            target=_run, name=f"local-worker-{task_id}", daemon=True
        )
        thread.start()
        log.info(
            "triggered local worker", extra={"agent": agent.name, "task_id": task_id}
        )


def make_job_runner(config: Config, store: StateStore) -> JobRunner:
    """Factory — the single place a concrete job runner is chosen."""
    if config.job_runner == "local":
        return LocalJobRunner(store)
    if config.job_runner == "cloudrun":
        return CloudRunJobRunner(config)
    raise ValueError(f"unknown job runner: {config.job_runner}")
