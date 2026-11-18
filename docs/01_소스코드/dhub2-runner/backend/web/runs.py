"""Running pipelines for the web app.

``Executor`` is where a run happens. ``ThreadExecutor`` runs it in this
process, which is the local setup; a Kubernetes executor that starts a Job per
run can take its place without the API changing.

The scheduler fires pipelines on their ``options.schedule`` through the same
executor, so scheduled and manual runs share its limits. A paused pipeline's
schedule is skipped; it can still be run by hand.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Protocol

from croniter import croniter
from pipeline.runner import PipelineError

from .workspace import Workspace

log = logging.getLogger(__name__)


class Executor(Protocol):
    def submit(self, collection: str, pipeline: str) -> str:
        """Queue a run and return its id; refuse a pipeline that cannot run."""
        ...

    def cancel(self, collection: str, run_id: str) -> bool:
        """Ask a queued or running run to stop; True when it was found."""
        ...

    def active(self, collection: str) -> dict[str, str]:
        """``{pipeline: run id}`` of the runs queued or running in a collection."""
        ...


class ThreadExecutor:
    """Runs in a thread pool of ``max_runs`` threads. One run per pipeline at a
    time: a second request while one is queued or running gets the same run id.
    A cancelled run stops before its next step."""

    def __init__(self, workspace: Workspace, max_runs: int = 4):
        self.workspace = workspace
        self.pool = ThreadPoolExecutor(max_workers=max_runs, thread_name_prefix="run")
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], str] = {}
        self._cancelled: set[str] = set()
        # Called with (collection, pipeline) when a run ends, however it ended.
        self.listeners: list[Callable[[str, str], None]] = []

    def submit(self, collection: str, pipeline: str) -> str:
        opened = self.workspace.open(collection)
        opened.runner.load(pipeline)  # raises PipelineError for a pipeline that cannot run
        with self._lock:
            if (collection, pipeline) in self._active:
                return self._active[(collection, pipeline)]
            run_id = uuid.uuid4().hex[:12]
            self._active[(collection, pipeline)] = run_id
        opened.state.queue_run(run_id, pipeline)
        self.pool.submit(self._run, collection, pipeline, run_id)
        return run_id

    def _run(self, collection: str, pipeline: str, run_id: str) -> None:
        try:
            opened = self.workspace.open(collection)
            opened.runner.run(pipeline, run_id=run_id, cancelled=lambda: run_id in self._cancelled)
        except Exception:  # recorded on the run by the runner; logged here as well
            log.exception("collection %s pipeline %s run %s failed", collection, pipeline, run_id)
        finally:
            # Before the run stops being active, so what waits for it sees the result.
            for listener in self.listeners:
                try:
                    listener(collection, pipeline)
                except Exception:  # a listener must not break the executor
                    log.exception("run listener failed")
            with self._lock:
                self._active.pop((collection, pipeline), None)
                self._cancelled.discard(run_id)

    def cancel(self, collection: str, run_id: str) -> bool:
        with self._lock:
            if run_id not in {r for (b, _), r in self._active.items() if b == collection}:
                return False
            self._cancelled.add(run_id)
        return True

    def active(self, collection: str) -> dict[str, str]:
        with self._lock:
            return {p: r for (b, p), r in self._active.items() if b == collection}


class Scheduler:
    """Fires every collection's scheduled pipelines; looks for new or changed
    schedules on each tick."""

    def __init__(self, workspace: Workspace, executor: Executor, tick: float = 30):
        self.workspace = workspace
        self.executor = executor
        self.tick = tick
        self._next: dict[tuple[str, str, str], datetime] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            try:
                self.fire_due(datetime.now(UTC))
            except Exception:  # a broken collection must not stop the others' schedules
                log.exception("scheduler tick failed")

    def fire_due(self, now: datetime) -> list[tuple[str, str]]:
        fired = []
        seen = set()
        for collection in self.workspace.names():
            try:
                opened = self.workspace.open(collection)
                docs = opened.collection.list("pipeline")
                paused = opened.state.stopped("pipeline")
            except Exception:  # reported when the collection is opened from the UI
                continue
            for doc in docs:
                cron = (doc.get("options") or {}).get("schedule")
                if not cron or doc["name"] in paused:
                    continue
                key = (collection, doc["name"], cron)
                seen.add(key)
                if key not in self._next:
                    self._next[key] = croniter(cron, now).get_next(datetime)
                    continue
                if now < self._next[key]:
                    continue
                self._next[key] = croniter(cron, now).get_next(datetime)
                try:
                    self.executor.submit(collection, doc["name"])
                    fired.append((collection, doc["name"]))
                except PipelineError as exc:
                    log.warning(
                        "scheduled pipeline %s/%s cannot run: %s", collection, doc["name"], exc
                    )
        for key in set(self._next) - seen:
            del self._next[key]
        return fired


def wait_for(executor: Executor, collection: str, run_id: str, timeout: float = 60) -> None:
    """Block until a run is no longer active (for tests and scripts)."""
    deadline = time.monotonic() + timeout
    while run_id in executor.active(collection).values():
        if time.monotonic() > deadline:
            raise TimeoutError(run_id)
        time.sleep(0.05)
