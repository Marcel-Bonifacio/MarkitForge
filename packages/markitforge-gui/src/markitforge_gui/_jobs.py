# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Background conversion jobs with progress and cancellation.

``POST /api/convert`` hands the work to a :class:`JobManager` and returns at
once; the page polls ``GET /api/jobs/{id}``. Each job owns a
``threading.Event`` that is passed to the converter as ``cancel_event``, and a
``progress`` callback that records the latest :class:`ProgressEvent`.
A finished job is forgotten once the page has fetched its outcome, or after
``ttl`` seconds. Workers are daemon threads, so a running conversion never
keeps the program alive after Ctrl+C.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from markitforge import ConversionCancelled, ProgressEvent

from ._store import new_id

__all__ = ["Job", "JobManager", "JobFailed", "TooManyJobs", "FINISHED"]

logger = logging.getLogger(__name__)

FINISHED = ("done", "error", "cancelled")


class TooManyJobs(Exception):
    """The queue is full."""


class JobFailed(Exception):
    """A job ended with a user-facing error (status code and JSON body)."""

    def __init__(self, status: int, body: Dict[str, Any]) -> None:
        super().__init__(body.get("error"))
        self.status = status
        self.body = body


@dataclass
class Job:
    id: str
    name: str
    status: str = "queued"
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    progress: Dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    http_status: Optional[int] = None
    cleanup: List[Callable[[], None]] = field(default_factory=list)

    def on_progress(self, event: ProgressEvent) -> None:
        data = event.to_dict()
        # Messages come from converters; keep them short.
        if isinstance(data.get("message"), str):
            data["message"] = data["message"][:200]
        self.progress = data

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "job_id": self.id,
            "name": self.name,
            "status": self.status,
            "progress": dict(self.progress) if self.progress else None,
            "cancel_requested": self.cancel_event.is_set(),
        }
        if self.status == "done":
            out["result"] = self.result
        elif self.status == "error":
            out["error"] = self.error
            out["http_status"] = self.http_status
        return out


class JobManager:
    """Runs jobs on daemon worker threads and remembers them for a while."""

    def __init__(
        self,
        workers: int = 2,
        *,
        ttl: float = 15 * 60,
        max_jobs: int = 64,
        error_mapper: Optional[
            Callable[[BaseException], Tuple[int, Dict[str, Any]]]
        ] = None,
    ) -> None:
        self.ttl = ttl
        self.max_jobs = max_jobs
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._error_mapper = error_mapper
        self._closed = False
        self._queue: "queue.SimpleQueue[Any]" = queue.SimpleQueue()
        self._threads = [
            threading.Thread(
                target=self._worker, name=f"markitforge-job_{i}", daemon=True
            )
            for i in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._run(*item)

    def submit(
        self,
        name: str,
        work: Callable[[Job], Dict[str, Any]],
        cleanup: Optional[Callable[[], None]] = None,
    ) -> Job:
        """Queue ``work(job)``; it returns the result payload."""
        self.prune()
        job = Job(id=new_id(), name=name[:500])
        if cleanup is not None:
            job.cleanup.append(cleanup)
        job.progress = {
            "stage": "queued",
            "current": 0,
            "total": None,
            "fraction": None,
            "message": None,
        }
        with self._lock:
            active = sum(1 for j in self._jobs.values() if j.status not in FINISHED)
            accepted = not self._closed and active < self.max_jobs
            if accepted:
                self._jobs[job.id] = job
                self._queue.put((job, work))
        if not accepted:
            self._run_cleanup(job)
            raise TooManyJobs()
        return job

    def _run_cleanup(self, job: Job) -> None:
        while job.cleanup:
            func = job.cleanup.pop()
            try:
                func()
            except Exception:  # noqa: BLE001 - cleanup is best effort
                logger.warning("Job cleanup failed", exc_info=True)

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.finished = time.time()

    def _run(self, job: Job, work: Callable[[Job], Dict[str, Any]]) -> None:
        if job.cancel_event.is_set():
            self._run_cleanup(job)
            self._finish(job, "cancelled")
            return
        job.status = "running"
        job.progress = {
            "stage": "start",
            "current": 0,
            "total": None,
            "fraction": None,
            "message": None,
        }
        try:
            payload = work(job)
        except ConversionCancelled:
            self._run_cleanup(job)
            self._finish(job, "cancelled")
            return
        except JobFailed as exc:
            self._run_cleanup(job)
            job.error, job.http_status = exc.body, exc.status
            self._finish(job, "error")
            return
        except BaseException as exc:  # noqa: BLE001 - reported to the page
            self._run_cleanup(job)
            if job.cancel_event.is_set():
                self._finish(job, "cancelled")
                return
            if self._error_mapper is not None:
                job.http_status, job.error = self._error_mapper(exc)
            else:
                job.http_status, job.error = 500, {"error": str(exc)}
            self._finish(job, "error")
            return
        self._run_cleanup(job)
        if job.cancel_event.is_set():
            # The converter finished anyway; the user asked to stop, so drop it.
            self._finish(job, "cancelled")
            return
        job.result = payload
        job.progress = {
            "stage": "done",
            "current": 1,
            "total": 1,
            "fraction": 1.0,
            "message": None,
        }
        self._finish(job, "done")

    def get(self, job_id: str) -> Optional[Job]:
        self.prune()
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in FINISHED:
            return job
        job.cancel_event.set()
        if job.status == "queued":
            # The worker will clean up the temp file when it reaches the job.
            self._finish(job, "cancelled")
        return job

    def forget(self, job_id: str) -> None:
        """Drop a finished job (and its result payload) right away."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.status in FINISHED:
                del self._jobs[job_id]

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            for job_id in [
                j.id
                for j in self._jobs.values()
                if j.status in FINISHED and j.finished and now - j.finished > self.ttl
            ]:
                del self._jobs[job_id]

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel_event.set()
        # Jobs that never started: clean up here. Running ones stop on their own.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._run_cleanup(item[0])
                self._finish(item[0], "cancelled")
        for _ in self._threads:
            self._queue.put(None)
